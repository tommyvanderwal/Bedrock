# Saga: `vm_destroy`

**Module:** `bedrock_d/vm/destroy.py`  
**Class:** `VmDestroy`

## Purpose

Tear down a VM's domain, DRBD resource, LV pair, and rqlite rows in
the safe order. The inverse of [`vm_create`](vm_create.md) — what
vm_create allocated, vm_destroy releases.

## Trigger

`DELETE /api/vms/{vm_name}` from the dashboard / CLI, or
`POST /api/operations` with `kind="vm_destroy"`.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `vm_name` | str | The VM to remove |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `minor` | `load_resource_metadata` | DRBD minor, looked up from `drbd_resources` |
| `peers` | `load_resource_metadata` | Peer list, recovered from the .res config or rqlite |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`load_resource_metadata`](#load_resource_metadata) | Read `drbd_resources` row → `minor`, recover `peers` |
| 2 | [`virsh_destroy_running`](#virsh_destroy_running) | `virsh destroy` on every peer (force-stop if running) |
| 3 | [`virsh_undefine`](#virsh_undefine) | `virsh undefine` on every peer |
| 4 | [`drbd_down`](#drbd_down) | `drbdadm down` on every peer |
| 5 | [`drbd_wipe_md`](#drbd_wipe_md) | `drbdadm wipe-md` on every peer (frees the meta LV) |
| 6 | [`remove_drbd_res_file`](#remove_drbd_res_file) | Remove `/etc/drbd.d/vm-<name>-disk0.res` on every peer |
| 7 | [`lvremove_pair`](#lvremove_pair) | `lvremove` data + meta LV on every peer |
| 8 | [`delete_rqlite_rows`](#delete_rqlite_rows) | Delete the `vms` and `drbd_resources` rows |

## Revert

No inverse — `vm_destroy` is terminal. A new VM with the same name
can be created later via [`vm_create`](vm_create.md); it will pick
a fresh DRBD minor.

## Idempotency / resume

Every step uses an "absence is success" check:
- `virsh destroy`: tolerates "domain not running" (rc != 0 with
  that specific stderr is treated as success)
- `virsh undefine`: tolerates "domain not found"
- `drbdadm down` / `wipe-md`: tolerates "no such resource"
- `lvremove`: tolerates "LV not found"
- `delete_rqlite_rows`: `DELETE … WHERE name = ?` is a no-op on
  missing rows

A vm_destroy that crashed mid-saga (e.g. between virsh undefine
and drbd down) re-runs cleanly because every later step's
"already gone" branch fires.

## Step details

### `load_resource_metadata`

Reads `drbd_resources WHERE name = "vm-<vm_name>-disk0"` for the
`minor`. If the row is gone (vm_create never completed past
`register_drbd_resource`), the step still tries to clean up by
falling back to:
- `vms WHERE vm_name = ?` for the host list
- the `.res` file at `/etc/drbd.d/vm-<vm_name>-disk0.res` for peer
  parsing

If even those are gone, the saga short-circuits to
`delete_rqlite_rows` — there's nothing to clean up.

### `virsh_destroy_running`

`virsh destroy <vm_name>` on every peer. "Domain not running" /
"not found" is treated as success.

This step force-stops the VM (equivalent to power-off) — for a
graceful shutdown, call `virsh shutdown` via the
`/api/vms/<name>/stop` endpoint first.

### `virsh_undefine`

`virsh undefine <vm_name>` on every peer. Removes the persistent
domain definition.

### `drbd_down`

`drbdadm down vm-<vm_name>-disk0` on every peer. Disconnects the
resource from the kernel; LVs underneath stay intact (next step
wipes their DRBD metadata, the one after removes them).

### `drbd_wipe_md`

`drbdadm wipe-md --force vm-<vm_name>-disk0` on every peer.
Cleans the external metadata from the meta LV so a future
`vm_create` with the same name doesn't see stale state.

### `remove_drbd_res_file`

`rm /etc/drbd.d/vm-<vm_name>-disk0.res` on every peer. With the
file gone, `drbdadm dump` no longer sees the resource.

### `lvremove_pair`

`lvm.lvremove_pair(host, resource)` on every peer. Removes both
`bedrock-data-vm-<vm_name>-disk0` and `bedrock-meta-vm-<vm_name>-disk0`.

### `delete_rqlite_rows`

`DELETE FROM vms WHERE vm_name = ?` and `DELETE FROM drbd_resources
WHERE name = ?`. Bumps `bedrock_meta.revision` so every node's
subscriber drops the VM from its projection.
