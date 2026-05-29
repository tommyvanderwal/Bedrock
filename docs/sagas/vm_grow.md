# Saga: `vm_grow`

**Code:** `bedrock_d/vm/grow.py` (class `VmGrow`, `@saga("vm_grow")`)
**Sizing helpers:** `bedrock_d/vm/lvm.py`

## Summary

Online-grow one VM disk. DRBD extends in place — no detach, no remount, no
VM restart — by extending the data LV (and the meta LV first, when the larger
bitmap needs the room) on every peer, telling DRBD to re-read the device, and
recording the new size in rqlite. The guest still grows its own partition/FS
(`growpart` + `resize2fs`); that is a separate operator action this saga does
not perform.

Shrink is out of scope: DRBD does not shrink online. Recovery from an
over-grow is destroy + recreate at the desired size + restore from backup.

- **Trigger:** `POST /api/operations` with `kind="vm_grow"` and params
  `{"vm_name": ..., "new_gb": N[, "disk_index": 0]}`. The request body becomes
  the saga `ctx`. (The dashboard's VM-settings page drives a separate inline
  live-grow via `POST /api/vms/{vm_name}/compute` with `{"disk_gb": N}`; that
  path is not this saga.)
- **Where:** the saga executor inside `bedrock-d`. Step bodies shell out per
  peer via `lvm._run_on` — local when the host is empty/localhost/this node,
  SSH `root@<host>` otherwise.
- **End state:** every peer's data LV (and meta LV if needed) is at `new_gb`,
  DRBD reports the larger device, and `drbd_resources.data_size_bytes` /
  `meta_size_bytes` reflect the new baseline.

**ctx in:** `vm_name` (str); `new_gb` (int, new *total* GiB, must be `>`
current); `disk_index` (int, default 0 = boot disk).
**ctx filled:** `resource` = `vm-<vm_name>-disk<idx>`; `old_gb` (current
`data_size_bytes // GiB`); `peers` (node-name list).

| # | Step | What it does |
|---|------|--------------|
| 1 | [`load_current_size`](#load_current_size) | Read the `drbd_resources` row for `data_size_bytes` and the `peers` JSON column |
| 2 | [`validate_new_size`](#validate_new_size) | Refuse unless `new_gb > old_gb` |
| 3 | [`lvextend_meta_on_peers`](#lvextend_meta_on_peers) | `lvextend` the meta LV to `meta_size_mb_for(new_gb)` on every peer |
| 4 | [`lvextend_data_on_peers`](#lvextend_data_on_peers) | `lvextend -L <new_gb>G --no-resize-fs` the data LV on every peer |
| 5 | [`drbd_resize`](#drbd_resize) | `drbdadm resize <resource>` on every peer |
| 6 | [`update_drbd_resources_row`](#update_drbd_resources_row) | `UPDATE` `data_size_bytes` + `meta_size_bytes` in rqlite |

## Detail

Each step is idempotent. The executor records a `done` row per step in
`operation_steps`; a crash resumes from the first step without one. The LVM
grows run `lvextend ... || true` (`check=False`) and `drbd_resize` uses
`check=False`, so "already at size" and benign re-resize results never abort a
re-run.

### `load_current_size`

`SELECT data_size_bytes, peers FROM drbd_resources WHERE name = ?` for
`resource = vm-<vm_name>-disk<disk_index>`. Sets `ctx["resource"]`,
`ctx["old_gb"]` (`data_size_bytes // 1 GiB`), `ctx["peers"]` (the `peers` JSON
column, decoded). Raises `RuntimeError` if no row.

- **Revert:** none (read-only).
- **Idempotent:** pure read.

### `validate_new_size`

Raises `ValueError` if `new_gb <= old_gb`. Equal is rejected explicitly so the
operator doesn't mistake a no-op for a grow; smaller is a shrink, which is
unsupported. No reachability or peer checks here — an unreachable peer surfaces
as an SSH failure in a later step.

- **Revert:** none.
- **Idempotent:** pure check.

### `lvextend_meta_on_peers`

For each peer: `lvm.lvextend_meta(host, resource, new_gb)` →
`lvextend -L <meta>M <vg>/bedrock-meta-<resource>` where `<meta>` =
`meta_size_mb_for(new_gb)`. Meta grows before data because a larger data
device needs a larger DRBD per-peer bitmap; the bitmap must be able to describe
the new size before `drbd_resize` reads it. Small grows often fit the existing
meta LV, in which case LVM no-ops.

- **Revert:** none (`lvreduce` on live DRBD meta is unsafe).
- **Idempotent:** `lvextend` no-ops if target `<=` current.

### `lvextend_data_on_peers`

For each peer: `lvm.lvextend_data(host, resource, new_gb)` →
`lvextend -L <new_gb>G --no-resize-fs <vg>/bedrock-data-<resource>`.
`--no-resize-fs` because DRBD (not LVM) publishes the new size in the next
step, and the guest owns its own in-VM filesystem resize.

- **Revert:** none.
- **Idempotent:** `lvextend` no-ops if target `<=` current.

### `drbd_resize`

For each peer: `drbdadm resize <resource>` (`check=False`). DRBD re-reads the
now-larger data device, recalculates bitmap accounting, and publishes the new
device size to the kernel. Online — no detach, no remount.

- **Revert:** none.
- **Idempotent:** no-op once DRBD already sees the full LV size.

### `update_drbd_resources_row`

`UPDATE drbd_resources SET data_size_bytes = ?, meta_size_bytes = ?,
updated_at = ? WHERE name = ?`, with `meta_size_bytes` recomputed from
`meta_size_mb_for(new_gb)` and `updated_at` = epoch seconds. Deliberately last:
until it commits, a re-run still reads the old `old_gb` and stays consistent;
once it commits, the next grow uses the new size as its floor.

- **Revert:** none.
- **Idempotent:** `UPDATE` is a no-op when the values are unchanged.
