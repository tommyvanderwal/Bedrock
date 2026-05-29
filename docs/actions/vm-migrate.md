# Live-migrate a VM

Moves a running pet/ViPet VM from its current host to another node in the
cluster. QEMU memory migration is overlapped with DRBD replication so the
VM never pauses more than ~1 second.

**Triggered by:**

- Dashboard: VM detail page → `Live Migrate` button (greyed for cattle)
- VM table on `/vms` → Migrate button
- HTTP: `POST /api/vms/{name}/migrate` with optional `{"target_node":"<name>"}`

Migrate runs through the VM-lifecycle saga executor on the master.

**Source:** `mgmt/app.py:api_vm_migrate` → `_run_vm_saga("vm_migrate", …)`,
`bedrock_d/vm/migrate.py` (the `VmMigrate` saga — see
[`docs/sagas/vm_migrate.md`](../sagas/vm_migrate.md)).

## Preconditions

- VM is **running**. QEMU live migration copies RAM state, so a stopped
  domain has nothing to migrate. The saga does not gate on VM state; it
  relies on `virsh migrate` to fail cleanly if the domain isn't active.
- VM has a DRBD resource. Cattle has none, so `validate_request` rejects
  it (surfaced as a 500 — see failure modes; the dashboard greys the
  button, so this is an API-only path).
- `target_node` is in the resource's peer set (`drbd_resources.peers`)
  and reachable; `target_node != source`.
- Passwordless `ssh root@<target-loopback>` works from the source node
  (SSH mesh established at join time).
- Target node has the VM **defined** in libvirt — the create path handles
  this; manual XML edits may leave it undefined.

## Sequence

```
  T=0    POST /api/vms/NAME/migrate  {"target_node":"<dst>" | null}
         │  api_vm_migrate: null target → the VM's backup_node
         │  (400 if it has none). Then _run_vm_saga("vm_migrate", …)
         │  on the master.
         │
  step 1 validate_request
         │   → confirm VM has replicated record (cattle rejected),
         │     target in the resource peer set, target != source
         │   → resources = EVERY DRBD resource backing the VM
         │     (SELECT name FROM drbd_resources WHERE name LIKE
         │      'vm-NAME-disk%' — multi-disk VMs cycle all of them)
         │   → source     = vms.host (where the VM currently runs)
         │   → source_host/target_host = nodes.host (LAN IPs, used for
         │                 the per-resource drbdadm SSH commands)
         │   → target_lo  = dst nodes.loopback_ip (the migrate URI +
         │                 qemu+ssh host — pins the QEMU/libvirt transfer
         │                 to the 100.X.Y.N mesh /32)
         │
  step 2 enable_dual_primary
         │   for r in resources, on src AND dst:
         │     drbdadm net-options --allow-two-primaries=yes {r}
  step 3 drbd_primary_on_target
         │   for r in resources, on dst:  drbdadm primary {r}
         │
         │  (both nodes are now DRBD Primary — safe because QEMU will
         │  atomically hand off ownership on migrate pivot)
         │
  step 4 virsh_migrate_live  (on src):
         │   virsh migrate --live --verbose --unsafe
         │     --migrateuri  tcp://{target_lo}
         │     NAME
         │     qemu+ssh://root@{target_lo}/system
         │
         │   NB: NO --undefinesource — the source keeps the domain
         │   defined so it stays a failover target.
         │
         │   This opens two channels:
         │     libvirt control over SSH (qemu+ssh://...)
         │     QEMU memory migration traffic over tcp://{target_lo}:49152+
         │
         │   QEMU iteratively copies RAM pages, tracking dirty pages,
         │   until throttling and a final sub-second pause completes the
         │   handoff. On completion:
         │     - VM stops on src
         │     - VM resumes on dst, owning /dev/drbd{minor}
         │
  step 5 record_uuids_after_migrate
         │   for r in resources: read the post-promote DRBD current-UUID
         │   off dst's debugfs (/sys/kernel/debug/drbd/.../data_gen_id)
         │   over SSH, write it to rqlite (drbd_resource_uuid_set).
         │   The step-3 promote bumped the UUID; without recording it a
         │   later host-death failover is REFUSED by the INV-5 exact-
         │   equality gate (VM-02). The saga runs on the master, which
         │   has rqlite access.
         │
  step 6 drbd_secondary_on_source
         │   for r in resources, on src:  drbdadm secondary {r}
  step 7 disable_dual_primary
         │   for r in resources, on src AND dst:
         │     drbdadm net-options --allow-two-primaries=no {r}
  step 8 update_vms_host
         │   UPDATE vms SET host=<dst>, updated_at=… WHERE vm_name=NAME
         │   → the new home is now in rqlite; nodes re-reading the vms
         │     table see the VM has moved
         │
         │ _run_vm_saga returns 200 {
         │   "op_id": <N>, "state": "completed", "last_step": "update_vms_host"
         │ }
         │ (a saga failure raises 500 with the failing step + error)
         │
  (async) the dashboard reflects the new host on its next read of the
          vms table.
```

## Log lines

There is no single "migrated" event line; progress is per step. The saga
executor logs one line per step, plus the migrate saga's own UUID line.
Watch the daemon journal on the master:

```
journalctl -u bedrock-d -f
  saga[vm_migrate] op=<N> run step=enable_dual_primary
  saga[vm_migrate] op=<N> run step=virsh_migrate_live
  vm_migrate: recorded UUID for vm-NAME-disk0 = <12-hex>
  saga[vm_migrate] op=<N> run step=update_vms_host
  saga[vm_migrate] op=<N> COMPLETED
```

**Failure**: the executor logs `saga[vm_migrate] op=<N> step=<step> FAILED:
<error>` and stores the message in the operation's `error`. HTTP response:
`500` with `detail: "vm_migrate saga failed at step '<step>': step
<step>: <error>"` (the failing `drbdadm`/`virsh` stderr is in `<error>`).

## Why this exact order

1. **`allow-two-primaries=yes` on both ends before QEMU migrate**: DRBD
   by default refuses two Primaries simultaneously. During the QEMU
   handoff there is a moment where both nodes need the DRBD device
   writable. Forbidding two-primaries causes the migrate to fail with
   cryptic "Failed to start block copy job".
2. **`drbdadm primary` on destination before migrate**: libvirt on the
   destination expects the block device to already be accessible. DRBD
   Secondary is a read-only shadow; QEMU on dst would fail to open its
   disk at migrate resume.
3. **`--unsafe`**: acknowledges we're intentionally migrating between
   DRBD Primaries (a config libvirt flags as risky by default).
4. **`--migrateuri tcp://<target_loopback>`**: forces the QEMU memory
   copy over the mesh (the target's `100.X.Y.N/32`) instead of the LAN.
   Saves LAN bandwidth and, on the physical lab with USB4 / 2.5 G direct
   ethernet, rides the fast link.
5. **Record post-promote UUIDs before demoting**: `drbdadm primary` on
   the target bumps each resource's DRBD current-UUID. The HA failover
   path gates on an exact-equality UUID match (INV-5), so the saga writes
   the new UUID into rqlite immediately after the migrate, before any
   host-death failover could reference it (VM-02).
6. **Secondary-demote + disallow-two-primaries only after migrate
   returns**: reverting earlier would yank the device out from under QEMU
   during the handoff.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `500 vm_migrate saga failed at step 'validate_request': no replicated record for vm '<name>' (cattle VMs cannot migrate)` | Cattle has no DRBD resource; the saga's validate step rejects it. The UI greys the button, so this is an API-only path | Convert to pet first (see [`vm-convert.md`](vm-convert.md)). |
| `Host key verification failed. Connection reset by peer` | known_hosts cold for the target's loopback | `ssh-keyscan -H <target_loopback> >> /root/.ssh/known_hosts` on src. The join saga's `prescan_peer_hostkeys` scans peer LAN IPs, not loopbacks, so the loopback entry can still be cold. |
| `Requested operation is not valid: domain is already active` | Stale VM on target | `virsh undefine <vm>` on target; it will be re-created on successful migrate. |
| Migration aborts mid-way, VM resumes on src | QEMU detected dirty-page thrash / link saturation | Harmless — VM is still healthy on src. Retry with less load. |
| Migrate succeeded, dashboard still shows the old host briefly | the dashboard hasn't re-read the `vms` table yet | Wait for the next refresh; the tile updates once it re-reads the new `vms.host`. |
| Migrate succeeded, DRBD split-brain after | Both sides accepted writes for an extended overlap | See [`scenarios/split-brain.md`](../scenarios/split-brain.md). |

## Operator perspective

- **Typical duration** (testbed, nested KVM, 1 GB RAM VM): 1.0–1.2 s.
- **Physical lab** (USB4 10 Gbps ring, 25-run validation): mean 3.4 s,
  median 3.2 s, 0.5 s guest pause at handoff, zero failures.
- VM clock is preserved (KVM + qemu-guest-agent). TCP connections are
  held open by memory-state continuity; clients typically don't notice.
- The VM detail tile updates its host once the saga's `update_vms_host`
  step commits and the dashboard re-reads the `vms` table.
