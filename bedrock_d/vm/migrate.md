# bedrock_d/vm/migrate.py

The `vm_migrate` saga — the single code path behind `bedrock vm migrate`. It
live-migrates a running replicated VM from its current host to a target node:
RAM state copies over the network while disk bytes are already mirrored by DRBD,
so only memory moves. It runs on the master under the saga executor
(`bedrock_d/orchestrator/sagas`), drives `drbdadm` / `virsh` on both nodes via
`lvm._run_on` (local-or-SSH), and finishes by writing `vms.host` in rqlite. Only
pet/vipet VMs migrate — cattle VMs have no DRBD replica and are rejected at
validation.

## Functions / Classes

### `class VmMigrate` — `@saga("vm_migrate")`
Ordered, crash-resumable saga of seven idempotent steps. The executor runs the
steps top-to-bottom and resumes a half-finished migrate from the first step that
hasn't recorded completion; each step body tolerates re-execution (`check=False`
on the device commands), so a resumed run converges to "VM on target, source
Secondary, single primary, `vms.host` = target".

**ctx in:** `vm_name` (str), `target` (node_name — must be a DRBD peer of every
resource backing the VM and reachable).
**ctx filled by step_validate:** `resources` (list of DRBD resource names),
`source` (current `vms.host`), `source_host` / `target_host` (LAN IPs),
`target_lo` (target loopback `/32`, used as the migrate URI).

Steps (each `@step(...)`, executed in this order):

### `step_validate(ctx)` — `validate_request`
Confirms the VM is replicated and the move is legal; populates ctx routing info.
- **In:** `ctx['vm_name']`, `ctx['target']`.
- **Out:** rqlite reads — `drbd_resources` rows `LIKE vm-<name>-disk%` (name +
  peers), the VM's `vms.host`, and `nodes` (host + loopback_ip, read level
  `none`) for source and target. Sets `ctx['resources']`, `ctx['source']`,
  `ctx['source_host']`, `ctx['target_host']`, `ctx['target_lo']` (falls back to
  `target_host` if no loopback). Raises `RuntimeError` if there is no replicated
  record (cattle can't migrate) or the nodes table lacks a host; raises
  `ValueError` if the target is not a peer, or the VM is already on the target.

### `step_enable_dual(ctx)` — `enable_dual_primary`
Opens the bounded dual-primary window.
- **Out:** runs `drbdadm net-options --allow-two-primaries=yes <r>` on both
  source and target for every resource (subprocess via `_run_on`, `check=False`).

### `step_promote_target(ctx)` — `drbd_primary_on_target`
Promotes the target's replicas so libvirt can take the domain.
- **Out:** `drbdadm primary <r>` on `target_host` for every resource
  (`check=False`). Both nodes are now Primary.

### `step_migrate(ctx)` — `virsh_migrate_live`
The live migration itself.
- **Out:** on `source_host`, runs
  `virsh migrate --live --verbose --unsafe --migrateuri tcp://<target_lo> <vm_name> qemu+ssh://root@<target_lo>/system`
  (`check=False`, timeout 600 s). No `--undefinesource`, so the domain stays
  defined on the source. Raises `RuntimeError` (with truncated stderr) on
  non-zero return.

### `step_record_uuids(ctx)` — `record_uuids_after_migrate`
Records each resource's post-promote DRBD current-UUID on the new primary.
- **Out:** for every resource, reads `head -1
  /sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id` on `target_host`
  (`check=False`, timeout 10 s) and, when it parses (`0x…`), strips `0x` and
  calls `bedrock_state.drbd_resource_uuid_set(r, uuid)` (writes rqlite). A failed
  or malformed read logs a warning and is skipped (no raise).

### `step_demote_source(ctx)` — `drbd_secondary_on_source`
- **Out:** `drbdadm secondary <r>` on `source_host` for every resource
  (`check=False`).

### `step_disable_dual(ctx)` — `disable_dual_primary`
Closes the dual-primary window.
- **Out:** `drbdadm net-options --allow-two-primaries=no <r>` on both nodes for
  every resource (`check=False`).

### `step_update_host(ctx)` — `update_vms_host`
The commit point.
- **Out:** `UPDATE vms SET host = ?, updated_at = ? WHERE vm_name = ?` in rqlite
  (target, current unix time, vm_name). Once committed, the dashboard and
  downstream consumers see the VM on its new home.

## How it works

The saga walks the canonical Bedrock migrate shape. Disk bytes never move — DRBD
already mirrors them — so the only network transfer is RAM, pinned to the
target's loopback `/32` (mesh-routed over whatever NIC bedrock-net picked).

```
validate ── reads rqlite: resources, source, source/target host + target_lo
   │         (rejects cattle / wrong peer / already-on-target)
   ▼
enable_dual ─ allow-two-primaries=yes  on SOURCE + TARGET, every resource
   ▼
promote_target ─ drbdadm primary on TARGET        ┐ dual-primary window
   ▼                                              │ (both nodes Primary)
virsh_migrate_live ─ RAM copy SOURCE→TARGET       │
   ▼                  (--migrateuri tcp://target_lo)
record_uuids ─ read data_gen_id on TARGET → rqlite│
   ▼                                              │
demote_source ─ drbdadm secondary on SOURCE       ┘
   ▼
disable_dual ─ allow-two-primaries=no  on SOURCE + TARGET, every resource
   ▼
update_vms_host ─ UPDATE vms.host = target   (commit; dashboard sees the move)
```

Multi-disk VMs are handled by enumerating every `vm-<name>-disk%` resource at
validation and looping that list in each device step.

The dual-primary window is the load-bearing guard. `drbdadm primary` on the
target only succeeds while both peers carry `allow-two-primaries=yes`; the window
is opened in `enable_dual`, used across the promote and the live transfer, and
tightened back to single-primary in `disable_dual` so DRBD's default split-brain
protection is restored. All device commands run with `check=False`, so re-running
a partially-applied step (e.g. a resource already Primary) is a no-op rather than
a failure, which is what makes the saga resumable.

`record_uuids` exists because `step_promote_target`'s promote bumps each
resource's DRBD current-UUID. A later host-death failover (`vm.failover`) gates
on an exact UUID-equality check; if the new value isn't persisted to rqlite here,
that gate refuses to bring the VM up — every migrate would silently break HA. It
reads the UUID off the target's debugfs over SSH (quote-safe `head -1`) and
writes it from the master, mirroring `vm.failover.read_local_drbd_uuid`.

`update_vms_host` is deliberately last: rqlite only reflects the new home after
the device-level move has fully succeeded, so a crash mid-migrate never advertises
a VM as relocated before it actually is.

## Why
The domain is intentionally left defined on the source (no `--undefinesource`):
the source keeps a Secondary DRBD replica, so it stays a valid failover target
for the now-migrated VM. The migrate URI targets the target's loopback `/32`
rather than a NIC address, so the RAM transfer rides the mesh path the kernel
chooses and is unaffected by a single NIC change.
