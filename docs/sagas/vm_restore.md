# Saga: `vm_restore`

**Module:** `bedrock_d/vm/backup.py` · **Class:** `VmRestore`

## Summary

Restore a VM from a kopia backup and bring it back up — HA on DRBD for
pet/vipet — running on the VM's **home node** so all commands are local.

- **What:** power the VM off, restore each disk of the chosen backup, start it
  again. Disks are restored by writing through the DRBD-primary device, so DRBD
  replicates the restored bytes to the peers and the VM returns fully HA without
  a manual re-sync.
- **Triggers:** `POST /api/vms/{vm_name}/restore` `{"target_id": "...",
  "kopia_snapshot_id": "..."}` (omit the id to restore the newest backup). The
  mgmt master submits a `vm_restore` operation with `target_node = vms.host`;
  that node's `operations_drain` runs it.
- **Where:** the VM's home node. rqlite's `operations` table is the channel —
  no SSH from the master.
- **End state:** the VM running with the restored disk contents, DRBD UpToDate
  on its peers; `vms.last_restore` recorded.
- **Forward-only:** re-running restores again (idempotent overwrite); a failure
  records `vms.last_restore_err`.

### Inputs / outputs (`ctx`)

| key | direction | meaning |
|-----|-----------|---------|
| `target_id` | in (param) | backup target holding the snapshot |
| `vm_name` | in (param) | VM to restore (must exist in this cluster) |
| `kopia_snapshot_id` | in (param) | which backup; empty = newest recorded |
| `result` | out | `{started, disks, home_node}` |

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1 | `restore` | Calls `mgmt.backup.run_restore_to_ha` locally: power off the VM → `kopia mount` + `dd` each disk back through the DRBD primary (replicates to peers) → `virsh start`. |

## Detail

### 1 · `restore`

Runs `mgmt.backup.run_restore_to_ha(target_id, vm_name, kopia_snapshot_id)` on
the home node. It selects the backup row (the newest if no id is given), powers
the VM off (`run_restore` refuses on a running VM, since qemu holds the device),
restores every disk in that backup — `kopia mount` the snapshot and `dd` its
`disk0.img` onto the VM's DRBD device, so the write replicates to the secondaries
— then `virsh start`s the VM. The VM comes back with the restored data, HA on
DRBD.
**Revert:** none — restore is an overwrite; restore an earlier backup to roll
further back. **Idempotent:** restoring the same snapshot again reproduces the
same disk contents.

> Restoring a VM that no longer exists in the cluster (or onto a *different*
> cluster) first re-provisions the VM shell from the portable metadata snapshot
> (`<prefix>:<vm>:metadata`, written by `vm_backup`), then restores its disks.
