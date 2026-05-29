# bedrock_d/vm/destroy.py

The `VmDestroy` saga — the body of `bedrock vm delete`. It tears a VM down
end-to-end in safe reverse order: kill the running domain, undefine it from
libvirt, drop DRBD, wipe meta, remove the resource files and LVs, then delete the
rqlite rows. It runs in the saga executor inside `bedrock-d`, driven by the
orchestrator/reactor; the operator never touches the peers directly. It handles
multi-disk VMs and both disk shapes — replicated (DRBD data+meta pair) and cattle
(a single local LV) — and every step is idempotent, so re-running over a
half-destroyed VM converges.

## Functions / Classes

### `class VmDestroy` — `@saga("vm_destroy")`
The teardown saga. Each method below is a `@step`; the executor runs them in
declaration order and can resume mid-sequence after a crash. Shared context dict
(`ctx`):

- **In (ctx):** `vm_name: str`.
- **Fills as it runs:** `resources: list[str]` (the `vm-<name>-diskN` resource
  names), `peers: list[str]` (node names, or the home host for cattle),
  `already_gone: bool` (set when nothing is left to remove).

#### `step_load(ctx)` — step `load_resource_metadata`
Reads rqlite to learn the peer list and every disk resource.
- **In:** `ctx["vm_name"]`.
- **Out:** queries `drbd_resources` (`name`, `peers`) for `vm-<name>-disk%` and
  `vms` (`host`) for the VM, via `bedrock_d.state.RqliteClient`. If both come back
  empty, sets `ctx["already_gone"] = True` and returns. If DRBD rows exist, fills
  `resources` from their names and `peers` from the first row's JSON `peers`.
  Otherwise (cattle, no DRBD rows) sets `peers = [home]` and discovers the disk
  resources by running `lvs` on the home host, grepping the canonical
  `bedrock-data-vm-<name>-diskN` LV names; falls back to a single
  `vm-<name>-disk0` if none match.

#### `step_destroy_domain(ctx)` — step `virsh_destroy_running`
Stops the running domain on every peer.
- **In:** `ctx["peers"]`, `ctx["vm_name"]`.
- **Out:** runs `virsh destroy <vm_name>` per peer host; errors ignored (a
  defined-but-not-running domain returns non-zero). No-op if `already_gone`.

#### `step_undefine(ctx)` — step `virsh_undefine`
Removes the domain definition from libvirt on every peer.
- **In:** `ctx["peers"]`, `ctx["vm_name"]`.
- **Out:** runs `virsh undefine --nvram <vm_name>` per peer host; errors ignored.
  No-op if `already_gone`.

#### `step_drbd_down(ctx)` — step `drbd_down`
Brings each DRBD resource down on every peer.
- **In:** `ctx["peers"]`, `ctx["resources"]`.
- **Out:** runs `drbdadm down <resource>` per peer per disk; a resource that
  isn't up returns non-zero, which is fine. No-op if `already_gone`.

#### `step_wipe_md(ctx)` — step `drbd_wipe_md`
Zeroes each external meta LV so a future re-create starts clean.
- **In:** `ctx["peers"]`, `ctx["resources"]`.
- **Out:** runs `drbdadm wipe-md --force <resource>` per peer per disk (clears the
  meta superblock + activity log + bitmap); errors ignored. No-op if
  `already_gone`.

#### `step_remove_res(ctx)` — step `remove_drbd_res_file`
Deletes the per-resource DRBD config files.
- **In:** `ctx["peers"]`, `ctx["resources"]`.
- **Out:** `rm -f /etc/drbd.d/<resource>.res` (via `drbd_config.res_file_path`)
  per peer per disk. No-op if `already_gone`.

#### `step_lvremove(ctx)` — step `lvremove_pair`
Removes the backing LVs.
- **In:** `ctx["peers"]`, `ctx["resources"]`.
- **Out:** calls `lvm.lvremove_pair(host, resource)` per peer per disk, which
  removes the `data` + `meta` LVs (each only if present). The cattle disk LV
  shares the replicated `data_lv` name, so the same call covers both shapes. No-op
  if `already_gone`.

#### `step_delete_rows(ctx)` — step `delete_rqlite_rows`
Final cleanup of cluster-wide state.
- **In:** `ctx["vm_name"]`.
- **Out:** `DELETE FROM drbd_resources WHERE name LIKE 'vm-<name>-disk%'` and
  `DELETE FROM vms WHERE vm_name = <name>` via `bedrock_d.state.RqliteClient`.
  This runs even when `already_gone` (the guard is not checked here), so a stray
  row is cleaned regardless. Makes the VM operator-visibly gone.

### `_peer_hosts(peer_names) -> list[str]`
Maps node names to reachable hosts. (Private; used by every step.)
- **In:** `peer_names` — node names (or, on the cattle path, a node-name placed in
  `peers`).
- **Out:** queries `nodes` (`node_name`, `host`) via `lib.rqlite_client`
  (read level `none`). Returns each entry's `host`, or the entry itself
  unchanged when not found — so an unknown value is treated as a literal host and
  cattle teardown still reaches the box. Empty input returns `[]`.

## How it works

The saga undoes `VmCreate` in strict reverse order so nothing references something
already removed: a domain must stop before its DRBD goes down, DRBD must go down
before its LVs disappear, and the LVs go before the rqlite rows that named them.

```
load_resource_metadata   -- learn peers + every disk resource (or already_gone)
        |
virsh_destroy_running     -- stop the live domain        +
virsh_undefine            -- forget the domain XML        | libvirt
        |                                                 +
drbd_down                 -- stop replication            +
drbd_wipe_md              -- zero each meta superblock    | DRBD
remove_drbd_res_file      -- rm /etc/drbd.d/<r>.res       +
        |
lvremove_pair             -- drop data + meta (or cattle) LVs   LVM
        |
delete_rqlite_rows        -- drop drbd_resources + vms rows     state
```

Two disk shapes flow through the same steps. The replicated shape has
`drbd_resources` rows, so `peers` and `resources` come straight from rqlite. The
cattle shape has no DRBD rows; `step_load` reads the VM's `host` from `vms`,
treats it as the only peer, and enumerates disks by listing
`bedrock-data-vm-<name>-diskN` LVs on that host so multi-disk cattle clean up
fully. The DRBD-specific steps (`drbd_down`, `wipe_md`, `remove_res`) still run on
the cattle path but harmlessly no-op, since those resources never existed.

Every shell-touching step swallows errors (`|| true`, `check=False`, or
`lvremove_pair`'s presence check), which is what makes a re-run over a partially
destroyed VM safe — a missing domain, an already-down resource, or an absent LV is
treated as success. The `already_gone` guard short-circuits all the
host-touching steps when `step_load` found nothing; `delete_rqlite_rows` runs
unconditionally to sweep any leftover row.

`_peer_hosts` is the seam that turns the abstract peer list into concrete hosts.
Its lookup falls through to using the entry verbatim when a name isn't in `nodes`,
which keeps the cattle path (where the home node-name is stuffed into `peers`)
working even if the node row is missing.

## Why

Reverse-order teardown plus per-step error tolerance is what gives idempotency:
the only "real" failure that can stop the saga is a fundamentally broken peer
connection, not the expected "already removed" state at each layer. Wiping DRBD
meta (not just bringing it down) guarantees a later re-create of the same VM name
sees clean metadata rather than a stale, conflicting superblock.
