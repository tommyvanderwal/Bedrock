# Saga: `vm_migrate`

**Module:** `bedrock_d/vm/migrate.py`  
**Class:** `VmMigrate`

## Purpose

Live-migrate a VM from one peer to another. DRBD synchronous
replication makes this cheap: only RAM has to copy across, the
disk is already mirrored. The dance is the standard "DRBD dual-
primary live migrate" pattern from the LINBIT manual.

## Trigger

`POST /api/vms/{vm_name}/migrate` with `{"target_node": "<name>"}`
from the dashboard / CLI, or `POST /api/operations` with
`kind="vm_migrate"`.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `vm_name` | str | VM to migrate |
| `target_node` | str | Destination node (must be in the VM's peer set) |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `source_node` | `validate_request` | Current `host` from the `vms` row |
| `resource` | `validate_request` | `vm-<vm_name>-disk0` |
| `minor` | `validate_request` | DRBD minor, from `drbd_resources` |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_request`](#validate_request) | Confirm VM running on source, target in peer set, source != target |
| 2 | [`enable_dual_primary`](#enable_dual_primary) | `drbdadm net-options --allow-two-primaries=yes` on both peers |
| 3 | [`drbd_primary_on_target`](#drbd_primary_on_target) | `drbdadm primary` on the target |
| 4 | [`virsh_migrate_live`](#virsh_migrate_live) | `virsh migrate --live --persistent` from source to target |
| 5 | [`drbd_secondary_on_source`](#drbd_secondary_on_source) | `drbdadm secondary` on the source |
| 6 | [`disable_dual_primary`](#disable_dual_primary) | `drbdadm net-options --allow-two-primaries=no` |
| 7 | [`update_vms_host`](#update_vms_host) | `UPDATE vms SET host = ? WHERE vm_name = ?` |

## Revert

To migrate back: submit `vm_migrate` again with the original source
as `target_node`. No special "revert" saga exists because the
forward saga is itself symmetric — any peer in the resource's peer
set can become the new home.

If the saga **fails partway**, the cluster is in a recoverable
state but not always pretty:
- Failed before `virsh_migrate_live`: source still hosts, dual-
  primary still on. Re-run cleans it up.
- Failed during `virsh_migrate_live`: rare and bad — partial RAM
  transfer. Manual recovery via `virsh destroy` + `virsh start`
  on whichever side has DRBD Primary.
- Failed after `virsh_migrate_live`: VM is on target, DRBD may
  still be dual-primary. Re-run completes the steps (idempotent).

## Idempotency / resume

- `enable_dual_primary` / `disable_dual_primary`: idempotent
  (`drbdadm net-options` with the same value is a no-op)
- `drbd_primary_on_target` / `drbd_secondary_on_source`: idempotent
  (no-op if already in the requested role)
- `virsh_migrate_live`: NOT idempotent in the strict sense, but
  re-running detects "VM is already on target" via `virsh list`
  before issuing the migrate and short-circuits to the post-
  migrate cleanup steps.
- `update_vms_host`: `UPDATE` (no-op if value unchanged)

## Step details

### `validate_request`

Refuses if:
- `vm_name` not in `vms`
- VM's current state is not `running` (can't live-migrate a
  stopped VM — use `vm_destroy` + `vm_create` on the new host
  instead, or just start it on the new host)
- `target_node` not in the resource's peer set (DRBD would have
  to first add the peer — out of scope here)
- `target_node == source_node`

Populates `ctx["source_node"]`, `ctx["resource"]`, `ctx["minor"]`.

### `enable_dual_primary`

`drbdadm net-options --allow-two-primaries=yes <resource>` on both
the source and target nodes. This is the critical step that lets
both sides briefly hold DRBD Primary at once — without it,
`virsh migrate` would fail because the target can't open the
device for write.

### `drbd_primary_on_target`

`drbdadm primary <resource>` on the target. Both source AND
target are now DRBD Primary (allowed because of the previous
step).

### `virsh_migrate_live`

`virsh migrate --live --persistent --undefinesource
<source_qemu_uri> <target_qemu_uri> <vm_name>` from the source.
QEMU's live migration copies dirty RAM pages in iterations until
the working set is small enough to pause-copy-resume in <100 ms.
Disk I/O continues during migration — both sides read+write the
same DRBD bytes locally because of dual-primary.

`--persistent` ensures the domain is defined on the target;
`--undefinesource` removes it from the source after a successful
migrate.

### `drbd_secondary_on_source`

`drbdadm secondary <resource>` on the source. The source returns
to read-only DRBD; only the target writes from now on.

### `disable_dual_primary`

`drbdadm net-options --allow-two-primaries=no <resource>` on
both peers. Returns DRBD to the safe single-primary regime.

### `update_vms_host`

`UPDATE vms SET host = ?, updated_at = ? WHERE vm_name = ?`.
Bumps `bedrock_meta.revision` so every node's subscriber sees the
VM has moved.
