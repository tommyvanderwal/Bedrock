# List & delete backups

Read-only history per VM is folded into `cluster.json` on every
`BACKUP_DONE` log entry; deletion drops a kopia snapshot manifest
plus appends a `BACKUP_DELETED` entry so the dashboard reflects the
removal. Underlying chunk GC happens later, during the master's
scheduled `kopia maintenance run`.

**Triggered by:**

- Dashboard: VM detail page → Backups card → row table (list);
  per-row `Delete` button (delete)
- HTTP:
  - `GET /api/vms/{name}/backups`
  - `DELETE /api/vms/{name}/backups/{kopia_snapshot_id}`
    body `{"target_id":"main", "reason":"<freeform>"}`

**Source:** `mgmt/app.py:api_vm_backups_list`,
`mgmt/app.py:api_vm_backup_delete`,
`mgmt/backup.py:list_backups_for_vm`,
`mgmt/backup.py:delete_backup`,
`installer/lib/view_builder.py` BACKUP_* fold rules.

## List response

```json
{
  "vm": "backup-demo",
  "backups": [
    {
      "kopia_snapshot_id": "57528e497be645f2379e02126d9db8dc",
      "target_id": "main",
      "source_node": "bedrock-sim-1.bedrock.local",
      "bytes_added": 0,
      "duration_s": 3.14,
      "label": "alpine-pristine",
      "ts_index": 17
    }
  ],
  "last_backup_error": null,
  "last_restore": { "ts_index": 19, "kopia_snapshot_id": "...",
                    "target_id": "main", "dest_node": "..." },
  "last_restore_error": null
}
```

The list is **kept in cluster.json**, capped at the most recent 200
entries per VM. Older entries are still in the kopia repo and the
cluster log; the cap exists to keep cluster.json size bounded.

## Sequence — list

```
  GET /api/vms/NAME/backups
       │
       │ load_cluster() → c["vms"][NAME]
       │   missing → 404
       │
       │ Return 200 {
       │   "vm": NAME,
       │   "backups": vm.get("backups", []),
       │   "last_backup_error": vm.get("last_backup_error"),
       │   "last_restore": vm.get("last_restore"),
       │   "last_restore_error": vm.get("last_restore_error")
       │ }
```

Pure projection read. No SSH, no kopia call, no log append. Sub-
millisecond response time. The dashboard refreshes this on a 20 s
interval as part of the VM detail page; live updates flow through
the `task` WS channel during in-flight backups.

## Sequence — delete

```
  T=0    DELETE /api/vms/NAME/backups/SNAPID  {body}
         │
         │ target_id not configured → 400
         │
         │ ssh self_node:
         │   . /etc/bedrock/backup-credentials/<id>.env
         │   export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"
         │   kopia --config-file=/etc/bedrock/kopia/<id>.config \
         │     snapshot delete <SNAPID>
         │
         │   - kopia removes the snapshot manifest from the repo
         │   - the underlying content blobs persist until
         │     `kopia maintenance run` GCs them on the master
         │
  T+0.5  bedrock-rust IPC: append BACKUP_DELETED
         │   {vm, target_id, kopia_snapshot_id, reason}
         │
         │ Fold rule filters that snapshot out of vm["backups"].
         │
         │ Return 200 { "status":"ok", "kopia_snapshot_id":"<id>" }
```

## Why this order

1. **Drop manifest first, log second.** If the kopia delete failed,
   we don't want a `BACKUP_DELETED` in the cluster log claiming a
   snapshot is gone when it isn't. The fold rule would then hide it
   from the UI but the data would still be billed for in S3.
2. **Manifest delete is fast and reversible-ish** — kopia stores
   "tombstone" manifests until maintenance compaction. If the
   operator regrets the delete *before* the next `maintenance run`,
   advanced kopia recovery commands can resurrect the snapshot.
   Bedrock doesn't expose that in v1.
3. **GC delegated to maintenance owner**. The mgmt master is the
   single node that runs `kopia maintenance run` (per
   `snapshots-and-backup.md` §9c-bis). Other nodes' `delete`
   appends still record their intent but actual chunk reclamation
   is deferred to the master's schedule. Avoids concurrent-
   maintenance races.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 backup target …  not configured` | Caller sent a target_id that doesn't exist | Use `GET /api/backup/targets` to find valid ids. |
| `500 delete failed: kopia: error: snapshot manifest <id> not found` | Snapshot already deleted (or never existed) | Refresh the list — it should already be gone from cluster.json. |
| Snapshot disappears from `/api/vms/.../backups` but kopia repo still shows it under `kopia snapshot list` | maintenance hasn't run yet | Expected. Kopia maintenance runs on a configurable schedule (default weekly on the master). To force: `ssh master kopia maintenance run --full`. |
| List endpoint returns 0 backups for a VM that was definitely backed up | cluster.json projection lag (mgmt restart in flight, or rust IPC catch-up) | Wait 1–2 seconds; the subscriber will fold the BACKUP_DONE entry. If still empty, check `journalctl -u bedrock-mgmt` for subscriber errors. |

## Operator perspective

- **List is free.** Hit it on every dashboard refresh; it's a flat
  dict read.
- **Delete is fast** (sub-second usually) — kopia just drops a
  manifest. Storage savings show up after maintenance GC.
- **Cap of 200 entries per VM in cluster.json**: older backups stay
  in the kopia repo and are findable via `kopia snapshot list
  <override-source>`; bedrock just doesn't display them in the UI.
  v1.x will add a "load older" pager.
- The `last_backup_error` field is sticky — a successful subsequent
  backup does NOT clear it. The UI shows it as a yellow banner with
  the timestamp so operators know the most recent failure even if
  the next attempt succeeded.
