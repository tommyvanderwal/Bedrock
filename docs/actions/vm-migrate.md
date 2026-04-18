# Live-migrate a VM

Moves a running pet/ViPet VM from its current host to another node in the
cluster. QEMU memory migration is overlapped with DRBD replication so the
VM never pauses more than ~1 second.

**Triggered by:**

- Dashboard: VM detail page → `Live Migrate` button (greyed for cattle)
- VM table on `/vms` → Migrate button
- HTTP: `POST /api/vms/{name}/migrate` with optional `{"target_node":"<name>"}`

**Source:** `mgmt/app.py:_vm_migrate`.

## Preconditions

- VM is **running** (`state=="running"`).
- VM has a DRBD resource (cattle is rejected with 400).
- Target node is defined in `cluster.json` and reachable.
- Passwordless `ssh root@<target-drbd-ip>` works from the source node
  (mesh established at join time).
- Target node has the VM **defined** in libvirt — the convert and create
  paths handle this; manual XML edits may leave it undefined.

## Sequence

```
  T=0    POST /api/vms/NAME/migrate  {"target_node":"<dst>" | null}
         │
         │ build_cluster_state()
         │   → vm["running_on"] = src_name
         │   → vm["backup_node"] = dst_name (auto-pick if no target given)
         │   → resource = "vm-NAME-disk0"  (from virsh dumpxml)
         │
         │ dst_migrate_ip = dst["tb_ip"] OR dst["drbd_ip"] OR
         │                  dst["eno_ip"] OR dst["host"]
         │
  T+0.1  ssh src:  drbdadm net-options --allow-two-primaries=yes {resource}
         │ ssh dst:  drbdadm net-options --allow-two-primaries=yes {resource}
         │ ssh dst:  drbdadm primary {resource}
         │
         │  (both nodes are now DRBD Primary — safe because QEMU will
         │  atomically hand off ownership on migrate pivot)
         │
  T+0.3  ssh src (timeout=120):
         │   virsh migrate --live --verbose --unsafe
         │     --migrateuri  tcp://{dst_migrate_ip}
         │     NAME
         │     qemu+ssh://root@{dst_migrate_ip}/system
         │
         │   This opens two channels:
         │     libvirt control over SSH (qemu+ssh://...)
         │     QEMU memory migration traffic over tcp://{dst_ip}:49152+
         │
         │   QEMU iteratively copies RAM pages, tracking dirty pages,
         │   until throttling and a final sub-second pause completes the
         │   handoff. On completion:
         │     - VM stops on src
         │     - VM resumes on dst, owning /dev/drbd{minor}
         │
  T+~1s  ssh src:  drbdadm secondary {resource}
         │ ssh src:  drbdadm net-options --allow-two-primaries=no {resource}
         │ ssh dst:  drbdadm net-options --allow-two-primaries=no {resource}
         │
  T+~1s  push_log "VM NAME migrated from <src> to <dst> in <dur>s"
         │   → WS 'event' channel (instant in browser)
         │   → VictoriaLogs
         │
         │ return 200 { "status":"migrated", "from":<src>, "to":<dst>,
         │              "duration_s":<dur> }
         │
  (async) next state_push_loop tick (≤ 3s later) broadcasts the new
          running_on; VM tile in the dashboard updates.
```

## Log lines

**Success:**

```
VM NAME migrated from <src> to <dst> in <dur>s
  level=info  hostname=<dst>  app=bedrock-mgmt
```

**Failure** (migrate command exit code != 0):

```
VM NAME migration FAILED from <src> to <dst>: <stderr_first_line>
  level=error  hostname=<src>  app=bedrock-mgmt
```

HTTP response: 500 with `detail: "Migration failed: ..."`.

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
4. **`--migrateuri tcp://<drbd_ip>`**: forces the QEMU memory copy over
   the DRBD ring (10.99.0.x) instead of the LAN. Saves LAN bandwidth
   and, on the physical lab with USB4 / 2.5 G direct ethernet, uses the
   fast link.
5. **Secondary-demote + disallow-two-primaries only after migrate
   returns**: reverting earlier would yank the device out from under QEMU
   during the handoff.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 VM X has no DRBD resource (cattle VM — cannot migrate)` | Caller didn't check; UI now greys the button | Convert to pet first (see [`vm-convert.md`](vm-convert.md)). |
| `Host key verification failed. Connection reset by peer` | Known_hosts cold for target's drbd_ip | `ssh-keyscan -H <drbd_ip> >> /root/.ssh/known_hosts` on src. Bootstrap sets `accept-new` for 192.168.*/10.* so this shouldn't happen on fresh installs. |
| `Requested operation is not valid: domain is already active` | Stale VM on target | `virsh undefine <vm>` on target; it will be re-created on successful migrate. |
| Migration aborts mid-way, VM resumes on src | QEMU detected dirty-page thrash / link saturation | Harmless — VM is still healthy on src. Retry with less load. |
| Migrate succeeded, dashboard still shows old `running_on` for ~3 s | State push loop hasn't run yet | Wait up to 3 s. The `event` log line arrives instantly; the tile updates next tick. |
| Migrate succeeded, DRBD split-brain after | Both sides accepted writes for an extended overlap | See [`scenarios/split-brain.md`](../scenarios/split-brain.md). |

## Operator perspective

- **Typical duration** (testbed, nested KVM, 1 GB RAM VM): 1.0–1.2 s.
- **Physical lab** (USB4 10 Gbps ring, 25-run validation): mean 3.4 s,
  median 3.2 s, 0.5 s guest pause at handoff, zero failures.
- VM clock is preserved (KVM + qemu-guest-agent). TCP connections are
  held open by memory-state continuity; clients typically don't notice.
- The Recent Logs panel shows the migration entry **before** the VM
  detail tile updates running_on (log ~instant vs tile ~ next 3s tick).
