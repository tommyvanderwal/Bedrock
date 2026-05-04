# Back up a VM

Captures a block-fidelity, content-addressed snapshot of a VM's
primary disk into the cluster's kopia repository. Streams the LV
through kopia stdin — no intermediate temp file. Identical 4 MiB
chunks across snapshots are deduplicated by kopia, so unchanged
sectors never re-upload.

**Triggered by:**

- Dashboard: VM detail page → yellow `Backup` button (greyed when no
  target is configured)
- HTTP: `POST /api/vms/{name}/backup` with `{"target_id":"main",
  "label":"<freeform>"}`

**Source:** `mgmt/app.py:api_vm_backup`,
`mgmt/backup.py:run_backup`.

## Preconditions

- A backup target is configured (`/api/backup/targets` non-empty).
- The kopia repo is connected on the VM's home node (mgmt master's
  reactor or boot-reconcile runs `kopia repository connect` on every
  node when a target_set entry lands).
- The VM is **shut down** (any state ≠ running). Live backup of a
  running VM works in principle (LV thin snapshot is a point-in-time
  COW), but the inner filesystem may be in a crash-inconsistent
  state — the operator is responsible for quiescing first if they
  care about FS consistency. Cattle VMs without a guest agent should
  be stopped before backup.
- The VM's home node has free space in the LV thin pool for the COW
  of the snapshot; only changed sectors take space until lvremove.

## Sequence

```
  T=0    POST /api/vms/NAME/backup  {"target_id":"main", "label":"v1"}
         │
         │ load_cluster() — find vm + target metadata
         │   → vm not in cluster.json   → 404
         │   → target_id not configured → 400
         │
         │ task = task_registry().create("vm.backup", …)
         │ asyncio.create_task(_run())   ← fire-and-forget
         │
         │ Return 202 { "status":"accepted", "task_id":"…" }
         │ ────────────────────────────────────────────────────
         │
         │  (background)
         │
  T+0.1  resolve VM home node:
         │   home_node_name = vm["host"]   (the node-NAME)
         │   ssh_host       = nodes[home_node_name]["host"]   (the IP)
         │
         │ probe disks via ssh + virsh dumpxml:
         │   primary = first <source dev='…'/>      e.g. /dev/almalinux/vm-NAME-disk0
         │   vg, lv  = parse out "almalinux", "vm-NAME-disk0"
         │
         │ snap_label  = label OR strftime("%Y%m%dT%H%M%S")
         │ snap_lv     = "<lv>-bk-<label>"   e.g. vm-NAME-disk0-bk-v1
         │
  T+0.2  ssh home_node:
         │   lvcreate --snapshot --name <snap_lv>  <vg>/<lv>
         │   lvchange -ay -K  /dev/<vg>/<snap_lv>
         │
         │   (-K overrides skip-activation: thin snapshots are k-flagged
         │    by default and /dev/<vg>/<snap_lv> isn't usable until
         │    explicitly activated.)
         │
  T+0.3  ssh home_node, single piped shell command:
         │   set -o pipefail
         │   . /etc/bedrock/backup-credentials/<target_id>.env
         │   export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"
         │   dd if=/dev/<vg>/<snap_lv> bs=4M status=none
         │     | kopia --config-file=/etc/bedrock/kopia/<id>.config
         │             snapshot create  /bedrock/vms/<vm>
         │               --stdin-file=disk0.img
         │               --override-source=<cluster_uuid>:vms:<vm>
         │               --description=<label>
         │               --json
         │
         │   - kopia content-defined chunks the stream at ≈4 MiB
         │   - each chunk is BLAKE2B-256 hashed
         │   - identical chunks across snapshots resolve to the same
         │     content blob, no re-upload
         │   - `--stdin-file=disk0.img` makes the snapshot a single-
         │     file directory (one entry "disk0.img"), so on restore
         │     we can address `<snap-id>/disk0.img` directly.
         │   - `--override-source` makes the snapshot's identity stable
         │     across nodes / live migrations: same VM ⇒ same source
         │     line in `kopia snapshot list`.
         │
         │   Final JSON line of stdout = manifest summary; we parse
         │   `id` (kopia snapshot id) + `stats.uploadedBytes`.
         │
  T+...  ssh home_node: lvremove -f /dev/<vg>/<snap_lv>
         │   (always — ran in `finally`, also on failure)
         │
  T+done bedrock-rust IPC: append BACKUP_DONE
         │   {vm, target_id, kopia_snapshot_id, source_node,
         │    bytes_added, duration_s, label, ts_index=N}
         │
         │ task.succeed(); WS broadcast on 'task' channel.
         │
         │ The fold rule prepends this entry to vm["backups"] in
         │ cluster.json; `/api/vms/<vm>/backups` reflects it on the
         │ next request, dashboard tile updates via the next state
         │ tick or operator refresh.
```

On failure: bedrock-rust IPC append `BACKUP_FAILED` with `{vm,
target_id, reason, source_node, label}`; fold writes
`vm["last_backup_error"]`. lvremove still runs.

## Why this exact order

1. **lvcreate + lvchange -ay -K**: thin snapshots default to skip-
   activation. Without `-K` the LV exists in metadata but
   `/dev/<vg>/<snap>` is not a usable block device, and kopia's
   subsequent stat on the snapshot path fails with ENOENT.
2. **dd | kopia --stdin-file**: kopia 0.21 errors on raw-device
   sources (`unsupported source: <path>`); it is filesystem-level.
   Streaming via stdin captures exact block contents — partition
   table, bootloader, all inner filesystems unchanged — and gets
   the dedup property anyway via content-defined chunking.
3. **`set -o pipefail`**: without this, the rc of the pipe is
   kopia's. If `dd` errors mid-read (e.g. snapshot ran out of thin
   pool space), the pipe would still complete and kopia would
   record a partial backup. With pipefail the failure surfaces.
4. **lvremove in `finally`**: long-lived LV snapshots eat thin pool
   space on every write to the origin. Backup snapshots exist only
   for the duration of the kopia read; cleanup is mandatory even
   when kopia errors.

## Log lines

**Success (cluster log, structural):**

```
BACKUP_DONE
  vm=<name> target_id=main kopia_snapshot_id=<32-hex>
  source_node=<node-name> bytes_added=<N> duration_s=<f>
  label=<label> ts_index=<idx>
```

**Failure:**

```
BACKUP_FAILED
  vm=<name> target_id=main reason=<short>
  source_node=<node-name> label=<label>
```

**push_log (VictoriaLogs side, also visible in dashboard logs feed):**

```
backup[<vm>]: lvcreate snapshot /dev/<vg>/<snap>
backup[<vm>]: dd | kopia snapshot create (stdin-file)
backup[<vm>] done: kopia=<id>, <N> bytes added, <f>s
```

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Task ends `failed` with `can't resolve SSH host for VM <name>` | VM record missing `host` field, or master can't SSH to peer | Check `cluster.json` shows the VM under the right node; ensure SSH mesh is healthy. |
| Task ends `failed` with `Volume group "<vg>" has insufficient free space` | Thin pool exhausted; snapshot couldn't COW | Free up space (delete unused snapshots / VMs) and retry. Snapshots only need COW space for the *changed* sectors during the backup window, so larger pools rarely hit this. |
| Task ends `failed` with `unsupported source: …` | kopia 0.21 quirk if `--stdin-file` got dropped from the cmd | Check `mgmt/backup.py` — see [`lesson_kopia_e2e_setup.md`](../lessons-log/2026-05-04-kopia-e2e.md). |
| Task `succeeded` but `bytes_added=0` even on a fresh disk | Repo already has all those chunks (e.g. backing up the same Alpine image twice — dedup wins) | Expected. Look at `kopia content stats` on the master to see total repo size. |
| Snapshot LV left behind after a backup | mgmt crashed between kopia and lvremove | Manual cleanup: `ssh <home> lvremove -f /dev/<vg>/<lv>-bk-*`. v1.x will add an orphan-snapshot reaper. |

## Operator perspective

- **Typical duration**: depends on (a) total disk size for the first
  backup and (b) changed bytes for subsequent. Testbed (1 GB Alpine,
  freshly written): ~3 s wall-clock. Steady-state incremental
  backup of a 100 GB VM with ~5 GB changes: ~30 s.
- **Storage cost on the kopia side** = unique data + metadata.
  Backing up the same disk a second time (no changes) typically
  uploads 0 new bytes; only the new manifest takes ~1 KB.
- A backup of a running VM captures the LV state at the
  `lvcreate --snapshot` instant. The inner filesystem can be in a
  journal-replay-needed state; on restore + boot, the guest kernel
  replays the journal and recovers cleanly for typical Linux FSes
  (ext4, xfs). Quiescing the guest first (e.g. via qemu-guest-agent
  fs-freeze) makes this fully clean — v1.x optional addition.
