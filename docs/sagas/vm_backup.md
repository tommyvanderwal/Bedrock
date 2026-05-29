# Saga: `vm_backup`

**Module:** `bedrock_d/vm/backup.py` · **Class:** `VmBackup`

## Summary

Back up every disk of a VM to its kopia target, plus a portable metadata
snapshot, running on the VM's **home node** so the snapshot and kopia stream
are local (no SSH from the master).

- **What:** freeze the guest, LVM-snapshot each backing disk, stream each to the
  kopia repo (content-addressed, incremental), and snapshot a portable metadata
  JSON; record the result in rqlite.
- **Triggers:** `POST /api/vms/{vm_name}/backup` `{"target_id": "...", "label": "..."}`
  (dashboard / scheduler). The mgmt master submits a `vm_backup` operation with
  `target_node = vms.host`; that node's `operations_drain` (mgmt/orchestrator.py)
  runs it.
- **Where:** the VM's home node. rqlite's `operations` table is the channel —
  the master never SSHes the home node; the home node executes locally.
- **End state:** a new `vm_backups` row (per-disk kopia ids + bytes added,
  `fs_freeze_used`), a `<prefix>:<vm>:metadata` kopia snapshot describing the
  VM (type, vCPUs, RAM, disks, libvirt XML), and the LV snapshots removed.
- **Forward-only:** kopia is idempotent and incremental, so re-running just
  re-snapshots (cheap, deduped). A failed run records `vms.last_backup_error`.

### Inputs / outputs (`ctx`)

| key | direction | meaning |
|-----|-----------|---------|
| `target_id` | in (param) | backup target (`backup_targets` row) |
| `vm_name` | in (param) | VM to back up |
| `label` | in (param) | optional snapshot label (defaults to a timestamp) |
| `result` | out | `{kopia_snapshot_id, metadata_kopia_id, bytes_added, duration_s, fs_freeze_used}` |

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1 | `backup` | Calls `mgmt.backup.run_backup` locally: freeze → LVM-snapshot each backing LV → `kopia snapshot create` per disk → portable metadata snapshot → drop LV snapshots → `vm_backups` row. |

## Detail

### 1 · `backup`

Runs the full freeze→snapshot→stream→metadata→cleanup→record cycle via
`mgmt.backup.run_backup(target_id, vm_name, label)`. Because the saga executes
on the home node, `run_backup` resolves the disk-owning host to *this* node and
runs `virsh domfsfreeze`, `lvcreate --snapshot` (against the DRBD backing LV,
resolved with `drbdadm sh-ll-dev`), `dd | kopia snapshot create`, and the
metadata `kopia snapshot create` all as local subprocesses. All disks are
snapshotted under one freeze window for a consistent point-in-time; the guest
is thawed before the (slower) kopia streaming. LV snapshots are always removed,
even on kopia failure.
**Revert:** none — kopia snapshots are immutable and additive; deleting one is a
separate `DELETE /api/vms/{vm}/backups/{id}`. **Idempotent:** kopia dedups by
content, so a re-run uploads only changed chunks and writes a fresh `vm_backups`
row for that point-in-time.
