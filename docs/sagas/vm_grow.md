# Saga: `vm_grow`

**Module:** `bedrock_d/vm/grow.py`  
**Class:** `VmGrow`

## Purpose

Online-grow a VM's disk. DRBD does this in-place — no detach, no
VM restart — by extending the underlying LV on every peer, telling
DRBD to refresh its bitmap accounting, and updating the
`drbd_resources` row.

Shrinking is **not** supported by this saga (DRBD doesn't shrink
online; the operator would need to manually destroy + recreate the
resource).

## Trigger

`POST /api/vms/{vm_name}/grow` with `{"new_disk_gb": N}` from the
dashboard / CLI, or `POST /api/operations` with
`kind="vm_grow"`.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `vm_name` | str | VM to grow |
| `new_disk_gb` | int | New data LV size (must be ≥ current) |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `current_disk_gb` | `load_current_size` | Old size, recovered from rqlite |
| `peers` | `load_current_size` | Peer list from `drbd_resources` |
| `resource` | `load_current_size` | `vm-<vm_name>-disk0` |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`load_current_size`](#load_current_size) | Read `drbd_resources` row for `data_size_bytes`, derive `peers` |
| 2 | [`validate_new_size`](#validate_new_size) | Refuse if shrink, refuse if equal, refuse if peer is unreachable |
| 3 | [`lvextend_meta_on_peers`](#lvextend_meta_on_peers) | Grow meta LV if `meta_size_mb_for(new_disk_gb)` > current |
| 4 | [`lvextend_data_on_peers`](#lvextend_data_on_peers) | `lvextend -L <new>G` on every peer's data LV |
| 5 | [`drbd_resize`](#drbd_resize) | `drbdadm resize <resource>` on home node (one place; DRBD broadcasts the new size) |
| 6 | [`update_drbd_resources_row`](#update_drbd_resources_row) | Update `data_size_bytes` in rqlite |

## Revert

No automated shrink. To revert (if a grow somehow needs to be
undone before the new space is used): `lvremove` is destructive and
unsafe. The supported recovery is:
1. `vm_destroy` the VM
2. Re-create with the desired (smaller) `disk_gb`
3. Restore from backup

## Idempotency / resume

- `lvextend` is naturally idempotent (no-op if target ≤ current)
- `drbdadm resize` is idempotent (no-op if DRBD already sees the
  full LV size)
- `update_drbd_resources_row` is `UPDATE` (no-op if value unchanged)

A grow that crashes mid-saga can be re-run safely. The only
"unrolled" state is the partial peer extend — peers that already
got `lvextend` no-op next time; peers that didn't get the new size.

## Step details

### `load_current_size`

Reads `drbd_resources WHERE name = "vm-<vm_name>-disk0"`:
- `data_size_bytes` → `ctx["current_disk_gb"]`
- `data_lv`, `meta_lv` (re-derive via `lvm.lv_names_for`)
- Peer list — recovered by reading the .res file's `on <peer>`
  blocks (the row doesn't store peers directly)

### `validate_new_size`

Refuses if:
- `new_disk_gb < current_disk_gb` (shrink — not supported)
- `new_disk_gb == current_disk_gb` (no-op — refuse to spend
  cycles on no work)
- Any peer is unreachable via SSH (the grow MUST happen on all
  peers atomically or DRBD ends up with inconsistent sizes)

### `lvextend_meta_on_peers`

For each peer: compute `lvm.meta_size_mb_for(new_disk_gb)` and
`lvextend -L <new_mb>M` if it's larger than the current meta LV
size. Idempotent — `lvextend` no-ops if target ≤ current.

The meta LV's required size scales with `max_peers × data_gb`, so
large grows do need a matching meta-LV grow; small grows
typically don't.

### `lvextend_data_on_peers`

For each peer: `lvextend -L <new_disk_gb>G --no-resize-fs
bedrock/<data_lv>`. `--no-resize-fs` because DRBD will publish the
new size to the kernel via `drbdadm resize` in the next step; the
filesystem inside the VM owns its own resize ceremony.

### `drbd_resize`

Run on the **home node only**. `drbdadm resize vm-<vm_name>-disk0`.
DRBD reads the new LV size, recalculates bitmap accounting, and
publishes the new device size. The peers' DRBD instances see the
size change via the replication channel; no SSH ceremony needed for
this step.

### `update_drbd_resources_row`

`UPDATE drbd_resources SET data_size_bytes = ?, meta_size_bytes = ?,
updated_at = ? WHERE name = ?`. Bumps `bedrock_meta.revision`.
