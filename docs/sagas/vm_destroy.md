# Saga: `vm_destroy`

Tears a VM down end-to-end, inverting [`vm_create`](vm_create.md): kill the
running domain, undefine it from libvirt, drop DRBD, wipe meta, remove the
`.res` files and LVs, then delete the rqlite rows. Handles multi-disk VMs and
both disk shapes — replicated (DRBD data+meta pair) and cattle (a single local
LV).

## Summary

| field | value |
|-------|-------|
| **What** | Release everything `vm_create` allocated for one VM. |
| **Trigger** | `DELETE /api/vms/{vm_name}` (dashboard, or `bedrock vm delete <name>`). |
| **Where** | The mgmt master. `api_vm_delete` runs it fire-and-forget in a task that calls `_run_vm_saga("vm_destroy", {"vm_name": ...})`; the executor submits an `operations` row and runs the steps synchronously on this node. |
| **Code** | `bedrock_d/vm/destroy.py` (`VmDestroy`), helpers `bedrock_d/vm/lvm.py`, `bedrock_d/vm/drbd_config.py`. |
| **End state** | No domain, DRBD resource, `.res` file, or LV for the VM on any peer; no `vms` / `drbd_resources` rows. Operator-visibly gone. |

`ctx` in: `vm_name: str`.
`ctx` filled: `resources: list[str]` (`vm-<name>-disk0`, `disk1`, …), `peers:
list[str]` (node names, or the home host for cattle), `already_gone: bool`.

| # | Step | What it does |
|---|------|--------------|
| 1 | `load_resource_metadata` | Read rqlite → `resources` + `peers`; set `already_gone` if nothing is left. |
| 2 | `virsh_destroy_running` | `virsh destroy <name>` on every peer (force-stop). |
| 3 | `virsh_undefine` | `virsh undefine --nvram <name>` on every peer. |
| 4 | `drbd_down` | `drbdadm down <r>` on every peer, per disk. |
| 5 | `drbd_wipe_md` | `drbdadm wipe-md --force <r>` on every peer, per disk. |
| 6 | `remove_drbd_res_file` | `rm -f /etc/drbd.d/<r>.res` on every peer, per disk. |
| 7 | `lvremove_pair` | `lvremove` data + meta LV on every peer, per disk. |
| 8 | `delete_rqlite_rows` | Delete the `drbd_resources` + `vms` rows. |

Order is the strict inverse of create: a domain stops before its DRBD goes
down; DRBD goes down before its LVs disappear; LVs go before the rows that
named them.

## Detail

Steps 2–7 short-circuit when `already_gone` is set; step 8 always runs to sweep
any stray row. Every host-touching step swallows errors (`|| true`,
`check=False`, or `lvremove_pair`'s presence check), so a missing domain,
down resource, or absent LV reads as success — that "absence is success" rule
is what makes a re-run over a half-destroyed VM converge.

### 1. `load_resource_metadata`

Queries `drbd_resources WHERE name LIKE 'vm-<name>-disk%'` (for `resources` +
the `peers` JSON of the first row) and `vms WHERE vm_name = ?` (for `host`), via
`bedrock_d.state.RqliteClient`.

- **Both empty** → set `already_gone = True` and return (nothing to clean up).
- **DRBD rows present** (pet/vipet) → `resources` from their names, `peers` from
  the first row's JSON peer list.
- **No DRBD rows but a `vms` row** (cattle) → `peers = [home]`; enumerate disks
  by running `lvs` on the home host and grepping the canonical
  `bedrock-data-vm-<name>-diskN` LV names, so multi-disk cattle clean up fully.
  Falls back to a single `vm-<name>-disk0` if none match.

**Revert:** none (read-only). **Idempotent:** pure read.

### 2. `virsh_destroy_running`

`virsh destroy <name> 2>/dev/null || true` on every peer host. Force-stop
(power-off). For a graceful shutdown, call `POST /api/vms/<name>/stop` first.

**Revert:** none. **Idempotent:** a defined-but-not-running or absent domain
returns non-zero, ignored.

### 3. `virsh_undefine`

`virsh undefine --nvram <name>` on every peer host. Removes the persistent
domain XML (and the UEFI nvram file).

**Revert:** none. **Idempotent:** "domain not found" is ignored.

### 4. `drbd_down`

`drbdadm down <r>` on every peer, per disk. Disconnects the resource from the
kernel; the LVs underneath stay intact.

**Revert:** none. **Idempotent:** a resource that isn't up returns non-zero,
ignored.

### 5. `drbd_wipe_md`

`drbdadm wipe-md --force <r>` on every peer, per disk. Zeroes the external
meta superblock + activity log + bitmap on the meta LV, so a future `vm_create`
of the same name sees clean metadata rather than a stale superblock.

**Revert:** none. **Idempotent:** wiping already-wiped meta is a no-op.

### 6. `remove_drbd_res_file`

`rm -f /etc/drbd.d/<r>.res` (path from `drbd_config.res_file_path`) on every
peer, per disk. With the file gone, `drbdadm dump` no longer sees the resource.

**Revert:** none. **Idempotent:** `rm -f`.

### 7. `lvremove_pair`

`lvm.lvremove_pair(host, resource)` on every peer, per disk — removes
`bedrock-data-<r>` and `bedrock-meta-<r>`, each only if `lvs` reports it
present. The cattle disk LV shares the replicated `data_lv` name, so the same
call covers both shapes.

**Revert:** none. **Idempotent:** a missing LV is skipped.

### 8. `delete_rqlite_rows`

`DELETE FROM drbd_resources WHERE name LIKE 'vm-<name>-disk%'` and `DELETE FROM
vms WHERE vm_name = ?`, via `bedrock_d.state.RqliteClient`. Runs even when
`already_gone`, so a leftover row is swept regardless.

**Revert:** none. **Idempotent:** `DELETE … WHERE` is a no-op on missing rows.

`_peer_hosts` maps each peer node name to its `host` via `nodes` (read level
`none`), falling through to the entry verbatim when a name isn't in `nodes` —
so the cattle path (home node-name stuffed into `peers`) still reaches the box
even if the node row is gone. `_run_on` runs the command locally when `host` is
this node, otherwise over SSH.

## Crash resume

The executor records each step's success in `operation_steps` and resumes from
the first step lacking a `done` row. Because every step is idempotent, a saga
that crashed mid-sequence (e.g. between `virsh_undefine` and `drbd_down`)
re-runs cleanly — each later step's "already gone" branch fires.

## Revert

None — `vm_destroy` is terminal. A new VM with the same name is created later
via [`vm_create`](vm_create.md); it picks a fresh DRBD minor.
