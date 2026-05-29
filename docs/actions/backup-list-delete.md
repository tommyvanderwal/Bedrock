# List & delete backups (per-VM and cluster-wide)

Each backup is a row in the rqlite `vm_backups` table. `cluster_state.load_cluster()`
projects those rows into each VM's `backups` list (newest-first, capped at 200 per
VM). Listing is a pure read of that projection — no SSH, no kopia call.

Deleting drops the kopia snapshot manifest on the node serving the request, then
`DELETE`s the `vm_backups` row and bumps `bedrock_meta.revision` so the projection
no longer shows it. The underlying content chunks are reclaimed later by
`kopia maintenance run` on the mgmt master (see `docs/snapshots-and-backup.md`
§9c-bis).

**Triggered by:**

- Dashboard: VM detail page → Backups card (per-VM list, 20 s poll)
- Dashboard: `/backups` page → "Snapshots — cluster-wide" card (every VM's
  backups, newest-first, 15 s poll), with per-row Restore / Delete buttons
- HTTP (8443 HTTPS, operator-authed; or 127.0.0.1:8001 for the CLI):
  - `GET /api/vms/{name}/backups`        (per-VM)
  - `GET /api/backups`                   (cluster-wide)
  - `DELETE /api/vms/{name}/backups/{kopia_snapshot_id}`
    body `{"target_id":"main", "reason":"<freeform>"}`

**Source:** `mgmt/app.py:api_vm_backups_list`, `api_backups_list_all`,
`api_vm_backup_delete`; `mgmt/backup.py:delete_backup`;
`installer/lib/bedrock_state.py:backup_deleted` (the `vm_backups` DELETE);
`installer/lib/view_builder.py:build_snapshot` (projects `vm_backups` rows into
`vm["backups"]`, newest-first, capped at 200); `installer/lib/cluster_state.py:load_cluster`.

## List response (per-VM)

```json
{
  "vm": "backup-demo",
  "backups": [
    {
      "kopia_snapshot_id": "57528e497be645f2379e02126d9db8dc",
      "disks": [
        {"target_dev": "vda", "lv_path": "/dev/bedrock/...",
         "kopia_snapshot_id": "57528e...", "bytes_added": 0}
      ],
      "target_id": "main",
      "source_node": "bedrock-sim-1.bedrock.local",
      "bytes_added": 0,
      "duration_s": 3.14,
      "label": "alpine-pristine",
      "fs_freeze_used": true,
      "ts_index": 17
    }
  ],
  "last_backup_error": null,
  "last_restore": { "ts_index": 19, "kopia_snapshot_id": "...",
                    "target_id": "main", "dest_node": "..." },
  "last_restore_error": null
}
```

`kopia_snapshot_id` is the first disk's snapshot id (`primary_kopia_id`); `disks`
carries the full per-disk array. `bytes_added` is the sum across disks. `ts_index`
is the `bedrock_meta.revision` value at write time — a cluster-wide monotonic
counter, so newest-first sort by `ts_index` is a global ordering.

The projection caps each VM at its 200 most recent backups (the underlying SQL
reads the 1000 newest `vm_backups` rows globally). Beyond that, rows stay in the
kopia repo and the `vm_backups` table but are not surfaced.

## Sequence — per-VM list

```
  GET /api/vms/NAME/backups
       │
       │ load_cluster() → c["vms"][NAME]
       │   missing → 404
       │
       │ 200 {
       │   "vm": NAME,
       │   "backups": vm.get("backups", []),
       │   "last_backup_error", "last_restore", "last_restore_error"
       │ }
```

Pure projection read against the local rqlite replica (level `none`, sub-ms). No
SSH, no kopia call, no write. The dashboard refreshes it every 20 s on the VM
detail page; live updates during an in-flight backup arrive on the `task` WS
channel.

## Sequence — cluster-wide list

```
  GET /api/backups
       │
       │ load_cluster()
       │ for vm_name, vm in c["vms"]:
       │   for b in vm.get("backups", []):
       │     out.append({**b, "vm": vm_name, "vm_present": True})
       │
       │ out.sort(key=ts_index, reverse=True)
       │ 200 { "backups": [...] }
```

Same projection-read characteristics — flattens every VM's history into one
timeline. `vm_present` is hard-set `True` on every emitted row. Surfacing orphan
snapshots (a deleted VM's rows, walked straight from the kopia repo) is a stub
left in `api_backups_list_all`, not yet wired. Deleting a VM does NOT cascade to
`vm_backups` (no FK; `vm_destroyed` only `DELETE`s from `vms`), so its rows linger
and the projection re-materialises a stub VM record from them — they still list
with `vm_present=True` until their snapshots are deleted or maintenance prunes
them.

## Sequence — delete

```
  T=0    DELETE /api/vms/NAME/backups/SNAPID  {target_id, reason}
         │
         │ target_id not in cluster backup_targets → 400
         │
         │ backup.delete_backup(target_id, SNAPID, NAME, reason):
         │   ssh <this node>:
         │     { [ -f /etc/bedrock/backup-credentials/<id>.env ] \
         │         && set -a && . <that file> && set +a; true; } \
         │       && export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)" \
         │       && kopia --config-file=/etc/bedrock/kopia/<id>.config \
         │            snapshot delete SNAPID
         │
         │     ( credentials env = _credentials_env(); config-file =
         │       _kopia_global_flags(). The .env source is skipped for
         │       kopia-fs targets that have no creds file. )
         │     - removes the snapshot manifest from the repo
         │     - content chunks persist until `kopia maintenance run` GCs them
         │
  T+δ    bedrock_state.backup_deleted(NAME, target_id, SNAPID, reason):
         │     DELETE FROM vm_backups WHERE vm_name=NAME
         │       AND primary_kopia_id=SNAPID AND target_id=target_id
         │     → bumps bedrock_meta.revision
         │
         │ The projection then no longer lists that snapshot.
         │ 200 { "status":"ok", "kopia_snapshot_id":"SNAPID" }
```

The kopia command runs over SSH to the node serving the request (its own
`host`, or `127.0.0.1`), since every node connects to the same shared repo and
can delete any snapshot in it.

## Why this order

1. **Drop manifest first, delete row second.** A failed kopia delete must not
   leave the `vm_backups` row gone while the snapshot still exists — the UI
   would hide a backup that is still consuming S3.
2. **Manifest delete is reversible-ish.** kopia keeps tombstone manifests until
   maintenance compaction, so an accidental delete can be recovered with kopia
   recovery commands before the next `maintenance run`. Bedrock does not expose
   that path.
3. **GC belongs to the maintenance owner.** Only the mgmt master runs
   `kopia maintenance run`, so deletes on any node only drop the manifest; chunk
   reclamation is deferred to the master's schedule, avoiding concurrent-
   maintenance races (`docs/snapshots-and-backup.md` §9c-bis).

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 backup target … not configured` | `target_id` not in the cluster's backup targets | `GET /api/backup/targets` for valid ids. |
| `500 delete failed: kopia: … snapshot manifest <id> not found` | Snapshot already deleted (or never existed) | Refresh the list; it is already gone from the projection. |
| Snapshot gone from `/api/vms/.../backups` but still in `kopia snapshot list` | Chunks not yet GC'd | Expected. Maintenance runs on the master (weekly). To force: `kopia maintenance run --full` on the master. |
| List returns 0 backups for a VM that was backed up | Local rqlite replica lag — a fresh write hasn't replicated to this node's replica yet (reads use level `none`) | Wait 1–2 s and refresh. If still empty, check `journalctl -u bedrock-d`. |

## Operator perspective

- **List is free** — a flat read of the local rqlite replica. Hit it on every
  refresh.
- **Delete is fast** (sub-second) — kopia just drops a manifest. Storage savings
  appear after maintenance GC on the master.
- **200-backup cap per VM in the projection** — older backups remain in the
  kopia repo and the `vm_backups` table; they are not shown in the UI.
- `last_backup_error` is sticky: a later successful backup does not clear it.
  The UI shows it as a banner with the timestamp so the most recent failure stays
  visible.
