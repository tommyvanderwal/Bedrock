# Back up a VM

Captures a block-fidelity, content-addressed snapshot of every disk
of a VM into the cluster's kopia repository, as one consistent
point-in-time. Each disk's LV snapshot is streamed through kopia
stdin — no intermediate temp file. Identical 4 MiB chunks across
snapshots are deduplicated by kopia, so unchanged sectors never
re-upload.

**Triggered by:**

- Dashboard: VM detail page → yellow `Backup` button (greyed when no
  target is configured)
- HTTP: `POST /api/vms/{name}/backup` with `{"target_id":"main",
  "label":"<freeform>"}`

**Source:** `mgmt/app.py:api_vm_backup`,
`mgmt/backup.py:run_backup`.

## Preconditions

- A backup target is configured (`/api/backup/targets` non-empty;
  rqlite table `backup_targets`).
- The kopia repo is connected on the VM's home node. Each node's own
  mgmt reactor watches `backup_targets` and runs
  `configure_target_locally` (→ `kopia repository connect`, creating
  the repo if it's the first node to land) whenever a target row
  appears or changes, so every node can run a local backup/restore.
- VM may be running OR shut down. When running with `qemu-guest-agent`
  reachable, bedrock calls `virsh domfsfreeze` around the LV-snapshot
  step so the resulting backup is **filesystem-consistent** (DBs flush,
  journals settle). When the agent is absent, bedrock falls through to
  a crash-consistent snapshot — safe for ext4/xfs which replay their
  own journals on first boot. The `fs_freeze_used` field on the backup
  row records which path actually ran.
- The VM's home node has free space in the LV thin pool for the COW
  of the snapshot; only changed sectors take space until lvremove.

## Multi-disk VMs

VMs with multiple disks are backed up as **one consistent point-in-
time**: bedrock takes ALL disk LV snapshots inside one bash invocation
on the home node, with `virsh domfsfreeze` before the first lvcreate
and `virsh domfsthaw` immediately after the last. The fs-freeze
window is bounded by `lvcreate × N` (typically tens of milliseconds
per disk) so the guest's IO pause is sub-second even on 4-disk VMs.

Each disk lands in the kopia repo as its own snapshot under a
per-disk source line (`<prefix>:<vm>:<target_dev>`, e.g.
`<uuid>:vms:web1:vda`, `<uuid>:vms:web1:vdb`). Dedup still works
per-disk because chunks are content-addressed independently. The
`vm_backups` rqlite row carries the `disks[]` list so restore puts
every disk back at the same LV path it came from.

## Sequence

```
  T=0    POST /api/vms/NAME/backup  {"target_id":"main", "label":"v1"}
         │
         │ load_cluster() — find vm + target metadata
         │   → vm not present           → 404
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
         │ probe disks via ssh + virsh dumpxml (ALL device='disk'):
         │   for each disk: <source dev='…'/> + <target dev='…'/>
         │     e.g. /dev/almalinux/vm-NAME-disk0 paired with "vda"
         │   vg, lv = parse out "almalinux", "vm-NAME-disk0"
         │
         │ snap_label  = label OR strftime("%Y%m%dT%H%M%S")
         │ snap_lv     = "<lv>-bk-<label>"   e.g. vm-NAME-disk0-bk-v1
         │
  T+0.2  ssh home_node, ONE bash script (all disks under one freeze):
         │   if running + guest-agent: virsh domfsfreeze <vm>
         │   for each disk: lvcreate --snapshot --name <snap_lv> <vg>/<lv>
         │   virsh domfsthaw <vm>   (ASAP, also in an EXIT trap)
         │   for each disk: lvchange -ay -K /dev/<vg>/<snap_lv>
         │   echo FS_FREEZE_USED=<0|1>
         │
         │   (-K overrides skip-activation: thin snapshots are k-flagged
         │    by default and /dev/<vg>/<snap_lv> isn't usable until
         │    explicitly activated.)
         │
  T+0.3  ssh home_node, one piped shell command PER DISK:
         │   set -o pipefail
         │   . /etc/bedrock/backup-credentials/<target_id>.env
         │   export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"
         │   dd if=/dev/<vg>/<snap_lv> bs=4M status=none
         │     | kopia --config-file=/etc/bedrock/kopia/<id>.config
         │             snapshot create  /bedrock/vms/<vm>/<target_dev>
         │               --stdin-file=disk0.img
         │               --override-source=<prefix>:<vm>:<target_dev>
         │               --description=<label>
         │               --json
         │
         │   <prefix> = target's override_source_prefix, else
         │             <cluster_uuid>:vms.
         │
         │   - kopia content-defined chunks the stream at ≈4 MiB
         │   - each chunk is BLAKE2B-256 hashed
         │   - identical chunks across snapshots resolve to the same
         │     content blob, no re-upload
         │   - `--stdin-file=disk0.img` makes the snapshot a single-
         │     file directory (one entry "disk0.img"), so on restore
         │     we can address `<snap-id>/disk0.img` directly.
         │   - `--override-source` makes each disk's identity stable
         │     across nodes / live migrations: same VM+disk ⇒ same
         │     source line in `kopia snapshot list`.
         │
         │   Final JSON line of each disk's stdout = manifest summary;
         │   we parse `id` (kopia snapshot id) + `stats.uploadedBytes`
         │   (falling back to `stats.totalSize`).
         │
  T+...  ssh home_node: lvremove -f /dev/<vg>/<snap_lv>
         │   (always — ran in `finally`, also on failure)
         │
  T+done bedrock_state.backup_done(vm, target_id, disks=[...],
         │   source_node, duration_s, label, fs_freeze_used)
         │   → INSERT one vm_backups row (ts_index = the bumped
         │     revision; disks[] stored as a JSON column,
         │     primary_kopia_id = disk0's id, bytes_added = sum)
         │
         │ task.succeed(); WS broadcast on 'task' channel.
         │
         │ view_builder queries vm_backups newest-first (ts_index DESC,
         │ ≤200 per VM) into vm["backups"]; `/api/vms/<vm>/backups`
         │ reflects it on the next request, dashboard tile updates on
         │ the next revision tick or operator refresh.
```

On failure: `bedrock_state.backup_failed(vm, target_id, reason,
source_node, label)` writes the error into rqlite; the projection
surfaces it as `vm["last_backup_error"]`. lvremove still runs.

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

**Success (rqlite `vm_backups` row):**

```
vm_name=<name> target_id=main primary_kopia_id=<kopia-id>
  source_node=<node-name> bytes_added=<N> duration_s=<f>
  label=<label> fs_freeze_used=<0|1> ts_index=<rev> disks=[...]
```

**Failure (rqlite `vms.last_backup_error`, a JSON blob):**

```
{ "ts_index": <rev>, "target_id": "main", "reason": "<short>" }
```

**Daemon journal (`journalctl -u bedrock-d`):**

```
backup[<vm>]: <n> disk(s) to back up: …
backup[<vm>]: snapshot phase (freeze + lvcreate × <n>)
backup[<vm>]: kopia stream disk <dev> ← /dev/<vg>/<snap>
backup[<vm>] done: <n> disk(s), <N> bytes added total, <f>s, …
```

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Task ends `failed` with `can't resolve SSH host for VM <name>` | VM record missing `host` field, or master can't SSH to peer | Check `bedrock status` / `/api/cluster` shows the VM under the right node; ensure SSH mesh is healthy. |
| Task ends `failed` with `Volume group "<vg>" has insufficient free space` | Thin pool exhausted; snapshot couldn't COW | Free up space (delete unused snapshots / VMs) and retry. Snapshots only need COW space for the *changed* sectors during the backup window, so larger pools rarely hit this. |
| Task ends `failed` with `unsupported source: …` | kopia is filesystem-level and rejects a raw block-device path; happens if `--stdin-file` is missing from the command | Check `mgmt/backup.py`; see [`docs/snapshots-and-backup.md`](../snapshots-and-backup.md). |
| Task `succeeded` but `bytes_added=0` even on a fresh disk | Repo already has all those chunks (e.g. backing up the same Alpine image twice — dedup wins) | Expected. Look at `kopia content stats` on the master to see total repo size. |
| Snapshot LV left behind after a backup | mgmt crashed between kopia and lvremove | Manual cleanup: `ssh <home> lvremove -f /dev/<vg>/<lv>-bk-*`. |

## Operator perspective

- **Typical duration**: depends on (a) total disk size for the first
  backup and (b) changed bytes for subsequent. Testbed (1 GB Alpine,
  freshly written): ~3 s wall-clock. Steady-state incremental
  backup of a 100 GB VM with ~5 GB changes: ~30 s.
- **Storage cost on the kopia side** = unique data + metadata.
  Backing up the same disk a second time (no changes) typically
  uploads 0 new bytes; only the new manifest takes ~1 KB.
- A backup of a running VM captures the LV state at the
  `lvcreate --snapshot` instant (see Preconditions for the
  freeze/crash-consistent split; the chosen path is recorded in
  `fs_freeze_used`).
