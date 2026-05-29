# Restore a VM from a backup

Streams a kopia snapshot directly back onto the VM's disk LV(s) —
byte-identical to what `vm-backup` captured, no intermediate temp
file. The VM must be shut down; on success its disk is rolled back to
the backup state. A backup row with multiple disks is restored as one
consistent unit.

**Triggered by:**

- Dashboard: VM detail → Backups list → `Restore` next to a snapshot row.
- HTTP: `POST /api/vms/{name}/restore` with `{"target_id":"main",
  "kopia_snapshot_id":"<32-hex>"}`.

**Source:** `mgmt/app.py:api_vm_restore`,
`mgmt/backup.py:run_restore`.

## Request body

```json
{
  "target_id": "main",
  "kopia_snapshot_id": "57528e497be645f2379e02126d9db8dc",
  "dest_node": "<optional, defaults to VM's home node>",
  "target_lv_path": "<optional, defaults to the VM's disk LV(s)>"
}
```

- `target_id` — backup **target** (kopia repo), default `"main"`.
- `kopia_snapshot_id` — any disk's snapshot id from a backup row;
  restore finds the owning row and rolls back every disk in it.
- `dest_node` + `target_lv_path` omitted → restore lands on the same
  LV(s) the backup was taken from (the "undo my last change" case).
  `target_lv_path` set → single-LV override: that one snapshot is
  written to that one LV (e.g. restore to a fresh, pre-created LV).

## Preconditions

- `target_id` exists in `cluster["backup_targets"]` (else 400).
- VM is **shut down**. mgmt enforces this: `run_restore` runs
  `virsh domstate` on the dest node and raises if it's `running`
  (qemu holds `/dev/<lv>` O_RDWR; a dd write would race in-flight VM
  state and corrupt both). mgmt does not auto-shutdown — the operator
  powers the VM off first.
- The dest node has the kopia repo connected (boot reconcile or
  reactor; verify via `kopia repository status`).
- `fusermount` is available (`/usr/bin/fusermount`, stock AlmaLinux 9).

## Sequence

```
  T=0    POST /api/vms/NAME/restore  {body}
         │
         │ load_cluster(); backup_targets[target_id] missing → 400
         │
         │ task = task_registry().create("vm.restore", …)
         │ asyncio.create_task(_run())   ← fire-and-forget
         │
         │ Return 202 { "status":"accepted", "task_id":"…" }
         │ ────────────────────────────────────────────────────
         │  (background, on the executor thread)
         │
         │ run_restore(target_id, kopia_snapshot_id, vm_name,
         │             target_lv_path=…, dest_node_name=…)
         │
  T+0.1  resolve dest:
         │   dest_node_name = dest_node OR vm["host"] OR self
         │   ssh_host       = nodes[dest_node_name]["host"]
         │   refuse if virsh domstate == running
         │
         │ build per-disk plan:
         │   target_lv_path set → [{custom, snapshot_id, that LV}]
         │   else  → match the backup row owning kopia_snapshot_id,
         │           one entry per disk; LV = current VM's matching
         │           target_dev (survives rename/recreate), else the
         │           lv_path frozen in the backup record
         │
  T+0.2  per disk: ssh dest, one shell script, `set -o pipefail`:
         │
         │   { [ -f /etc/bedrock/backup-credentials/<target_id>.env ] &&
         │       set -a && . …/<target_id>.env && set +a; true; } &&
         │   export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"
         │
         │   mnt=/run/bedrock-restore-<vm>-<target_dev>-<ms>
         │   mkdir -p $mnt
         │   kopia --config-file=/etc/bedrock/kopia/<target_id>.config \
         │     mount <kopia_snapshot_id> $mnt \
         │     >/tmp/kopia-restore.log 2>&1 &     ← backgrounded
         │   MOUNT_PID=$!
         │
         │   # poll up to 20s for FUSE to surface the file
         │   for i in $(seq 1 20); do
         │     [ -f $mnt/disk0.img ] && break; sleep 1
         │   done
         │   if [ ! -f $mnt/disk0.img ]; then
         │     echo 'kopia mount did not surface disk0.img within 20s'
         │     cat /tmp/kopia-restore.log
         │     fusermount -u $mnt; rmdir $mnt; exit 1
         │   fi
         │
         │   dd if=$mnt/disk0.img of=<target_lv_path> \
         │      bs=4M conv=sparse status=none
         │   DDRC=$?
         │   fusermount -u $mnt || kopia … mount unmount $mnt
         │   wait $MOUNT_PID; rmdir $mnt; exit $DDRC
         │
         │   ssh timeout 14400s (4h). Each snapshot's single file is
         │   named disk0.img regardless of which disk it backed up.
         │   kopia mount exposes it read-only over FUSE, pulling chunks
         │   from the repo on demand; no local temp file, only the
         │   cache dir grows. dd writes 4 MiB blocks with conv=sparse
         │   so holes stay sparse on the thin pool.
         │
  T+done bedrock_state.restore_done(vm, target_id, kopia_snapshot_id,
         │   dest_node, duration_s)  → rqlite vms.last_restore, bumps revision
         │
         │ task.succeed(); WS broadcast.
```

On failure: `bedrock_state.restore_failed(...)` writes rqlite
`vms.last_restore_err`, then re-raises so the task fails.

## Why this exact order

1. **FUSE mount + dd, not `kopia snapshot restore`**. kopia 0.21's
   `snapshot restore <id>/disk0.img <target>` calls `truncate(2)` on
   the destination, which `EINVAL`s on `/dev/*` block devices. Mount +
   dd avoids that path.
2. **No stream-to-stdout in 0.21**. kopia treats `-` as a literal
   directory name (`./-/`) and fails; FUSE is the supported escape.
3. **Background mount + poll**. `kopia mount` has no `--background`;
   shell `&` plus a 20 s poll for `disk0.img` lets dd start only once
   the file is live (typically <1 s).
4. **`set -o pipefail`** so a mid-pipeline failure (mkdir, kopia mount,
   dd, fusermount) propagates a non-zero rc instead of being swallowed.
5. **`fusermount -u` then `kopia mount unmount` fallback** for hosts
   where fusermount is restricted; `rmdir` last since the mountpoint
   must be empty first.

## State / log lines

`vms.last_restore` (success) and `vms.last_restore_err` (failure) are
rqlite columns; view_builder surfaces them on the VM record as
`last_restore` / `last_restore_error`, also returned by
`GET /api/vms/<vm>/backups`.

```
last_restore      = {ts_index, kopia_snapshot_id, target_id, dest_node}
last_restore_err  = {ts_index, kopia_snapshot_id, target_id, reason}
```

**Daemon journal (`journalctl -u bedrock-d`):**

```
restore[<vm>]: <n> disk(s) to restore: <dev>→<lv>, …
restore[<vm>]: kopia mount <id> → dd <target_lv>
restore[<vm>] done: <n> disk(s) in <f>s
restore[<vm>] failed: <type>: <msg>          # on error
```

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Task `failed` with `kopia mount did not surface disk0.img within 20s` | FUSE didn't initialise — config-file mismatch, missing fusermount, or wrong snapshot id | Check `/tmp/kopia-restore.log` on the dest node (the script cats it into the error); verify ids with `GET /api/vms/<vm>/backups`. |
| Task `failed` with `dd: writing to '/dev/…': No space left on device` | LV's thin pool exhausted | Free space; for cattle this is the host's local thin pool. |
| Task `failed`, `refusing to restore VM … it is currently running` | VM was up when restore was issued | Stop it (`POST /api/vms/<vm>/stop`, or `/force-stop`), then retry. |
| Task `succeeded` but VM still shows old data on next boot | host page cache kept stale data after dd | Rare — `dd conv=sparse` to a block device flushes through. If repeated, add `oflag=direct` to dd in `backup.py`. |
| Task `succeeded` but VM doesn't boot | the backup itself wasn't bootable | `dd if=<lv> bs=1 skip=510 count=2 \| od -An -tx1` should print `55 aa`; if not, the backup was of a non-OS disk. |
| Task hangs | orphaned kopia mount | `ps -ef \| grep kopia` on the dest node, `kill -9` the mount process. |

## Operator perspective

- **Typical duration**: 1–2 s for a 1 GB disk on the testbed
  (~570 MB/s through FUSE); scales ~linearly (100 GB ≈ 3 min on a
  10 Gbps backup ring).
- **In-place by default** — the "undo" case. To restore to a fresh LV,
  pre-create it and pass `target_lv_path`, e.g.
  `/dev/almalinux/vm-NAME-restored-disk0`.
- Intermediate dd states are visible to anyone reading the LV mid-write,
  but qemu is shut down so nothing reads it.
- A snapshot stays in the repo until `DELETE /api/vms/<vm>/backups/<id>`;
  a later `kopia maintenance run` from the master GCs its chunks.
