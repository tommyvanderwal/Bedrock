# Restore a VM from a backup

Streams a kopia snapshot directly back onto the VM's primary disk
LV — byte-identical to what `vm-backup` captured. No intermediate
temp file. The VM must be off; on success its disk is rolled back
to the backup state.

**Triggered by:**

- Dashboard: VM detail → Backups list → `Restore` button next to a
  snapshot row (planned UI; current API is operator-callable directly)
- HTTP: `POST /api/vms/{name}/restore` with `{"target_id":"main",
  "kopia_snapshot_id":"<32-hex>"}`

**Source:** `mgmt/app.py:api_vm_restore`,
`mgmt/backup.py:run_restore`.

## Request body

```json
{
  "target_id": "main",
  "kopia_snapshot_id": "57528e497be645f2379e02126d9db8dc",
  "dest_node": "<optional, defaults to VM's home node>",
  "target_lv_path": "<optional, defaults to VM's primary disk LV>"
}
```

When `dest_node` and `target_lv_path` are omitted, restore lands on
the same LV the backup was taken from — the "undo my last change to
this VM" case. Other combinations enable v1.x flows (restore to a
sibling disk, cross-node disaster restore).

## Preconditions

- The target backup exists in the kopia repo (`kopia_snapshot_id`
  matches an entry in `vm["backups"]`).
- VM is **shut down**. If it's running, qemu holds the LV with
  exclusive O_RDWR and the dd write would corrupt running state
  AND the in-flight changes would race with the dd. (mgmt does NOT
  auto-shutdown — the operator must do it explicitly.)
- The destination node has the kopia repo connected (boot reconcile
  or reactor; verify via `kopia repository status`).
- `fusermount` is available (`/usr/bin/fusermount`, in stock
  AlmaLinux 9 — no extra package).

## Sequence

```
  T=0    POST /api/vms/NAME/restore  {body}
         │
         │ load_cluster() — find target metadata
         │   → target_id not configured → 400
         │
         │ task = task_registry().create("vm.restore", …)
         │ asyncio.create_task(_run())   ← fire-and-forget
         │
         │ Return 202 { "status":"accepted", "task_id":"…" }
         │ ────────────────────────────────────────────────────
         │
         │  (background)
         │
  T+0.1  resolve dest:
         │   dest_node_name = body.dest_node OR vm["host"]
         │   ssh_host       = nodes[dest_node_name]["host"]
         │
         │ resolve target LV:
         │   target_lv_path = body.target_lv_path OR
         │                    first <source dev='…'/> from virsh dumpxml
         │
  T+0.2  ssh dest (ssh_host), single shell script with `set -o pipefail`:
         │
         │   . /etc/bedrock/backup-credentials/<id>.env
         │   export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"
         │
         │   (per disk in the snapshot's disks[] plan:)
         │   mnt=/run/bedrock-restore-<vm>-<target_dev>-<ms>
         │   mkdir -p $mnt
         │
         │   kopia --config-file=/etc/bedrock/kopia/<id>.config \
         │     mount <kopia_snapshot_id> $mnt &
         │   MOUNT_PID=$!
         │
         │   # poll up to 20s for FUSE to surface the file
         │   for i in $(seq 1 20); do
         │     [ -f $mnt/disk0.img ] && break
         │     sleep 1
         │   done
         │
         │   dd if=$mnt/disk0.img \
         │      of=<target_lv_path> \
         │      bs=4M conv=sparse status=none
         │   DDRC=$?
         │
         │   fusermount -u $mnt   ||
         │     kopia mount unmount $mnt
         │   wait $MOUNT_PID 2>/dev/null
         │   rmdir $mnt
         │   exit $DDRC
         │
         │   - kopia mount exposes the snapshot read-only as a FUSE
         │     filesystem; the single file is /<mnt>/disk0.img.
         │   - Reading from FUSE pulls chunks from the kopia repo
         │     on demand and re-assembles them in memory. No local
         │     temp file; only the cache directory accumulates.
         │   - dd writes 4 MiB blocks to the LV with conv=sparse —
         │     keeps holes sparse on the LV thin pool side.
         │
  T+done bedrock_state.restore_done(vm, target_id, kopia_snapshot_id,
         │   dest_node, duration_s)  → rqlite, bumps revision
         │
         │ task.succeed(); WS broadcast.
         │ vm["last_restore"] populated by the projection.
```

On failure: `bedrock_state.restore_failed(...)` writes to rqlite and
the projection surfaces `vm["last_restore_error"]`.

## Why this exact order

1. **FUSE mount, not snapshot restore-to-target**. kopia 0.21's
   `kopia snapshot restore <id>/disk0.img <target>` calls
   `truncate(2)` on the destination, which `EINVAL`s on `/dev/*`
   block devices. Mount + dd avoids that path entirely.
2. **`-` is not stdout in kopia restore**: kopia treats `-` as a
   literal directory name, creates `./-/`, fails. There is no
   stream-to-stdout option in 0.21; FUSE is the supported escape.
3. **Background mount + poll for the file**: `kopia mount` doesn't
   have `--background`. We background it via shell `&`, then poll
   for `disk0.img` to appear (typically within 1 s) before dd starts.
4. **`set -o pipefail`**: pipe `dd | …` already has the kopia exit
   as final, but we want the failure of any step (mkdir, kopia mount,
   dd, fusermount) to propagate as a non-zero rc. Without pipefail
   shell scripts swallow mid-pipeline errors.
5. **`fusermount -u` + `kopia mount unmount` fallback**: fusermount
   is the standard unmount path; kopia's own `mount unmount`
   subcommand is a fallback for systems where fusermount is
   restricted.
6. **`rmdir $mnt` last**: the mountpoint must be empty to remove,
   so we only do it after fusermount succeeds.

## Log lines

**Success (rqlite, surfaced as `vm["last_restore"]`):**

```
vm=<name> target_id=main kopia_snapshot_id=<id>
  dest_node=<node-name> duration_s=<f>
```

**Failure (rqlite, surfaced as `vm["last_restore_error"]`):**

```
vm=<name> target_id=main kopia_snapshot_id=<id>
  reason=<short> dest_node=<node-name>
```

**Daemon journal (`journalctl -u bedrock-d`):**

```
restore[<vm>]: <n> disk(s) to restore: …
restore[<vm>]: kopia mount <id> → dd <target_lv>
restore[<vm>] done: <n> disk(s) in <f>s
```

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| Task `failed` with `kopia mount did not surface disk0.img within 20s` | FUSE didn't initialise — typically a kopia mount config-file mismatch, missing fusermount, or the snapshot id is wrong | Check `/tmp/kopia-restore.log` on the home node for the kopia mount stderr; verify snapshot id with `GET /api/vms/<vm>/backups`. |
| Task `failed` with `dd: writing to '/dev/…': No space left on device` | LV's underlying thin pool exhausted | Free space; for cattle this is the host's local thin pool. |
| Task `succeeded` but VM still shows old data on next boot | Page cache on the host kept stale data after the dd write | Should be rare — `dd conv=sparse` of a block device flushes through. If repeated, add `oflag=direct` to dd in `backup.py`. |
| Task `succeeded` but VM doesn't boot | The backup itself wasn't bootable (LV had a non-bootable filesystem at backup time) | Verify with `dd if=<lv> bs=1 skip=510 count=2 \| od -An -tx1` — should print `55 aa`. If not, the backup was of a non-OS disk. |
| `fusermount: failed to unmount: Device or resource busy` | dd's read fd hadn't closed before unmount tried | Add a sleep after dd; in practice the script sequencing fixes this since dd has exited before unmount runs. |
| Task hangs forever | kopia mount is foreground but bash waited for `wait $MOUNT_PID` after fusermount succeeded — should exit cleanly. If it hangs check `ps -ef | grep kopia` on the home node; kill -9 the orphaned mount process. |

## Operator perspective

- **Typical duration**: 1–2 s for a 1 GB disk on the testbed (570
  MB/s through FUSE). Real disks scale linearly; 100 GB ≈ 3 minutes
  on a 10 Gbps backup ring.
- **Atomic from the LV's perspective.** dd writes the new bytes;
  intermediate states ARE visible to anyone reading the LV during
  the dd, but qemu is shut down so nothing's reading.
- **Restore overwrites in place by default.** This is the "undo"
  case. To restore to a fresh LV, pass `target_lv_path` to a
  different LV that you've created beforehand — e.g.
  `/dev/almalinux/vm-NAME-restored-disk0`.
- The kopia repo retains the snapshot until explicitly deleted via
  `DELETE /api/vms/<vm>/backups/<id>` and a subsequent `kopia
  maintenance run` from the master GCs the chunks.
