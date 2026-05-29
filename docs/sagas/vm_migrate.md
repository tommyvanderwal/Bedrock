# Saga: `vm_migrate`

**Module:** `bedrock_d/vm/migrate.py` · **Class:** `VmMigrate`

## Summary

Live-migrate a running pet/vipet VM from its current host to another peer.
DRBD keeps the disk synchronously mirrored on every peer, so only RAM copies
across — the standard DRBD dual-primary live-migrate dance.

- **What:** move a running VM to a target peer, RAM-only, zero downtime.
- **Triggers:**
  - `POST /api/vms/{vm_name}/migrate` `{"target_node": "<name>"}` (dashboard / CLI
    `bedrock vm migrate <name> --to <node>`). Omitting the target picks the VM's
    `backup_node`.
  - `POST /api/operations` `{"kind":"vm_migrate","params":{"vm_name":...,"target":...}}`.
- **Where:** runs on the mgmt master (it holds DRBD/arbiter authority and rqlite
  access); the CLI is a thin HTTP client to 127.0.0.1:8001. Per-resource
  `drbdadm`/`virsh` commands fan out to source and target over SSH via `lvm._run_on`.
- **End state:** VM running on `target`; every backing DRBD resource is Primary on
  `target`, Secondary on `source`; dual-primary disabled; post-promote DRBD UUIDs
  recorded on `target`; `vms.host = target`. The domain stays **defined** on the
  source (no `--undefinesource`), so the source remains a ready failover target.
- **Forward-only:** the executor has no compensation. Failed sagas stay `failed`
  until an explicit `retry`, which re-runs from the first not-`done` step. Every
  step is idempotent, so re-running converges.

### Inputs / outputs (`ctx`)

| key | direction | meaning |
|-----|-----------|---------|
| `vm_name` | in (param) | VM to migrate |
| `target` | in (param) | destination node; must be a DRBD peer of every resource |
| `resources` | filled by step 1 | every `vm-<vm_name>-disk*` resource backing the VM |
| `source` | filled by step 1 | current `vms.host` |
| `source_host` / `target_host` | filled by step 1 | LAN IPs of source/target (`nodes.host`) |
| `target_lo` | filled by step 1 | target loopback `/32` (the migrate URI; mesh-routed) |

> The HTTP/CLI field is `target_node`; the saga param is `target`.

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1 | `validate_request` | VM has a replicated record; `target` is a peer; `target != host`. Fills `resources`, `source`, hosts, `target_lo`. |
| 2 | `enable_dual_primary` | `drbdadm net-options --allow-two-primaries=yes <r>` on source + target, each resource. |
| 3 | `drbd_primary_on_target` | `drbdadm primary <r>` on target — both nodes now Primary. |
| 4 | `virsh_migrate_live` | `virsh migrate --live` on source; RAM copies to target's libvirt over the loopback `/32`. |
| 5 | `record_uuids_after_migrate` | Read each resource's post-promote DRBD UUID on target; write it to rqlite. |
| 6 | `drbd_secondary_on_source` | `drbdadm secondary <r>` on source. |
| 7 | `disable_dual_primary` | `drbdadm net-options --allow-two-primaries=no <r>` on both, each resource. |
| 8 | `update_vms_host` | `UPDATE vms SET host=target, updated_at WHERE vm_name`. |

## Detail

### 1 · `validate_request`

Queries `drbd_resources` for `name LIKE 'vm-<vm_name>-disk%'` and `vms.host` for
the VM. Refuses (no migrate) if:
- no `drbd_resources` row and no `vms` row — i.e. a cattle VM (one local thin LV,
  no DRBD): cattle cannot migrate.
- `target` is not in the first resource's `peers` set (adding a peer is out of scope).
- `target == vms.host` (already there).
- source/target missing a `host` in the `nodes` table.

Fills `resources` (all disks — multi-disk VMs are handled), `source`, `source_host`,
`target_host`, and `target_lo` (`nodes.loopback_ip`, falling back to the LAN host).
**Revert:** none (read-only). **Idempotent:** pure reads.

### 2 · `enable_dual_primary`

`drbdadm net-options --allow-two-primaries=yes <r>` on both source and target for
every resource (`check=False`). Without it, `drbdadm primary` on the target is
refused (the resource permits one primary by default).
**Revert:** step 7 sets it back to `=no`. **Idempotent:** same value is a no-op.

### 3 · `drbd_primary_on_target`

`drbdadm primary <r>` on the target for every resource. Both nodes are now Primary —
the bounded dual-primary window that lets `virsh` open the device for write on the
target. This promote bumps the resource's current-UUID (recorded in step 5).
**Revert:** step 6 demotes the source; failback (a reverse migrate) demotes the
target. **Idempotent:** no-op if already Primary.

### 4 · `virsh_migrate_live`

On the source:
```
virsh migrate --live --verbose --unsafe \
  --migrateuri tcp://<target_lo> \
  <vm_name> qemu+ssh://root@<target_lo>/system
```
QEMU iterates dirty RAM pages until the working set is small enough for a sub-100 ms
pause-copy-resume. Disk I/O continues throughout — both sides read+write the same
DRBD bytes locally (dual-primary). `--migrateuri` pins the transfer to the target's
loopback `/32`, so it rides whatever physical path bedrock-net picked. There is no
`--undefinesource`: the domain stays defined on the source for failback. 600 s
timeout; a non-zero rc raises and fails the saga.
**Revert:** none direct; failback is a reverse `vm_migrate`. **Idempotent:** if the
VM is already on the target, re-running this command is a no-op error that the later
idempotent steps recover from; a clean re-run after success short-circuits because
the domain already lives on the target.

### 5 · `record_uuids_after_migrate`

For each resource, reads `data_gen_id` from the target's
`/sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id` over SSH and writes the
hex UUID to rqlite via `bedrock_state.drbd_resource_uuid_set`. Necessary because the
promote in step 3 bumped the UUID, and a later host-death failover gates on an exact
UUID-equality check (INV-5) — without recording it here, the move would silently
break HA. A failed read logs a warning and skips that resource (no hard failure).
**Revert:** none. **Idempotent:** overwrites the same UUID.

### 6 · `drbd_secondary_on_source`

`drbdadm secondary <r>` on the source for every resource. The source's DRBD returns
to passing through replicated writes only; the target is the sole writer.
**Revert:** failback promotes the source again. **Idempotent:** no-op if already Secondary.

### 7 · `disable_dual_primary`

`drbdadm net-options --allow-two-primaries=no <r>` on both nodes, every resource.
Closes the dual-primary window so DRBD's default split-brain protection is back in force.
**Revert:** a future migrate re-opens it (step 2). **Idempotent:** same value is a no-op.

### 8 · `update_vms_host`

`UPDATE vms SET host = <target>, updated_at = <now> WHERE vm_name = <vm_name>`. The
last step; once it commits, every node's reactor sees the host change on its next
cluster-state diff: the target `virsh start`s (no-op — already running from the
migrate) and the source `virsh destroy`s its now-defunct local copy.
**Revert:** failback rewrites `host`. **Idempotent:** `UPDATE` to the same value is a no-op.
