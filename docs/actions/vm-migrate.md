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

- VM is **running** (`state=="running"`).
- VM has a DRBD resource. Cattle has none, so the saga's
  `validate_request` step rejects it (surfaced as a 500 — see failure
  modes; the dashboard greys the button so this is an API-only path).
- `target_node` is in the resource's peer set (a node in rqlite `nodes`)
  and reachable; `target_node != source_node`.
- Passwordless `ssh root@<target-loopback>` works from the source node
  (SSH mesh established at join time).
- Target node has the VM **defined** in libvirt — the convert and create
  paths handle this; manual XML edits may leave it undefined.

## Sequence

```
  T=0    POST /api/vms/NAME/migrate  {"target_node":"<dst>" | null}
         │  api_vm_migrate → _run_vm_saga("vm_migrate", …) on the master
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
         │   → bumps bedrock_meta.revision so every node's subscriber
         │     sees the VM has moved
         │
         │ _run_vm_saga returns 200 {
         │   "op_id": <N>, "state": "completed", "last_step": "update_vms_host"
         │ }
         │ (a saga failure raises 500 with the failing step + error)
         │
  (async) next rqlite revision tick broadcasts the new host; the VM
          tile in the dashboard updates.
```

## Log lines

The saga doesn't emit a single "migrated" event line; progress is per
step. Watch the daemon journal on the master:

```
journalctl -u bedrock-d -f
  vm_migrate: step enable_dual_primary on <src>/<dst>
  vm_migrate: step virsh_migrate_live …
  vm_migrate: recorded UUID for vm-NAME-disk0 = <12-hex>
  vm_migrate: step update_vms_host → host=<dst>
```

**Failure**: the failing step's `drbdadm`/`virsh` stderr is captured in
the saga's `error` field. HTTP response: `500` with
`detail: "vm_migrate saga failed at step '<step>': <error>"`.

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
| `Host key verification failed. Connection reset by peer` | known_hosts cold for the target's loopback | `ssh-keyscan -H <target_loopback> >> /root/.ssh/known_hosts` on src. The join saga's `prescan_peer_hostkeys` step covers this on fresh installs. |
| `Requested operation is not valid: domain is already active` | Stale VM on target | `virsh undefine <vm>` on target; it will be re-created on successful migrate. |
| Migration aborts mid-way, VM resumes on src | QEMU detected dirty-page thrash / link saturation | Harmless — VM is still healthy on src. Retry with less load. |
| Migrate succeeded, dashboard still shows the old host briefly | rqlite revision tick hasn't propagated yet | Wait a tick. The `event` log line arrives instantly; the tile updates once the subscriber projects the new `vms.host`. |
| Migrate succeeded, DRBD split-brain after | Both sides accepted writes for an extended overlap | See [`scenarios/split-brain.md`](../scenarios/split-brain.md). |

## Operator perspective

- **Typical duration** (testbed, nested KVM, 1 GB RAM VM): 1.0–1.2 s.
- **Physical lab** (USB4 10 Gbps ring, 25-run validation): mean 3.4 s,
  median 3.2 s, 0.5 s guest pause at handoff, zero failures.
- VM clock is preserved (KVM + qemu-guest-agent). TCP connections are
  held open by memory-state continuity; clients typically don't notice.
- The VM detail tile updates its host once the saga's `update_vms_host`
  step lands and the next rqlite revision tick projects it on each node.
