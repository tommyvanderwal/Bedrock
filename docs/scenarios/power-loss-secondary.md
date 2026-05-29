# Scenario: secondary node power loss

A cluster node that is a **DRBD Secondary** for one or more VMs loses power
or crashes hard. No VMs run on that node (it is a secondary); every primary
keeps serving I/O uninterrupted.

## State before

```
   node1 (P)                 node2 (S)                 node3 (S)
  ┌─────────┐               ┌─────────┐               ┌─────────┐
  │ VM foo  │               │         │               │         │
  │ DRBD    │═══════════════│ DRBD    │═══════════════│ DRBD    │
  │ UpToDate│◀─── ring ─────│ UpToDate│◀─── ring ─────│ UpToDate│
  └─────────┘               └──POWER──┘               └─────────┘
```

## What happens

The kernel on node2 is gone — no graceful shutdown, no `drbdadm secondary`,
no libvirt shutdown.

1. **node1** (primary): DRBD detects the dead TCP connection to node2 (DRBD's
   own `ping-timeout`/`connect-int`, kernel defaults — Bedrock does not tune
   them; the `.res` `net {}` block carries only `protocol C`, `allow-two-primaries
   no`, and the `after-sb-*` policies). `drbdadm status` on node1 shows peer
   `node2` going `Connecting` (then `StandAlone` or staying `Connecting`
   depending on wire state). The local disk stays `UpToDate`. **Writes to the
   VM continue uninterrupted** — protocol C only waits for ACK from reachable
   peers.
2. **node3** (secondary): same disconnect, same transition; its disk stays
   `UpToDate`. For a 3-way vipet VM, replication keeps running primary↔node3,
   so two good copies remain.
3. **VictoriaMetrics** (scraping from the mgmt-master, node1 in the default
   layout): scrapes of `node2:9100` (node-exporter) and `node2:9177`
   (vm-exporter) fail → `up=0`. The dashboard tile for node2 goes red.
4. **State push loop** (`mgmt/app.py` `state_push_loop` → `build_cluster_state`,
   every 3 s): the parallel SSH fan-out's `get_node_info("node2", …)` raises
   on connect (paramiko EOFError / timeout). `get_node_info` catches it and
   returns `{"online": false, "error": …}`, so node2 renders Offline.
5. **Election / witness** (bedrock-d netd + BedRock Echo): node2's mesh
   heartbeat stops, so peers drop its vote weight. No failover fires — node2
   held no VM primary and was not mgmt-master, so quorum and `.254` are
   unaffected. Witness slots matter only on master/primary loss.

## What the operator sees

| Where | What |
|---|---|
| Dashboard sidebar | node2 status dot red |
| `/hosts` page | node2 row Offline; host IP greyed, memory/load `-` |
| Recent Logs | nothing from node2 (it is dead); the state push loop emits no log event for a node going offline |
| VM detail of any VM with node2 as peer | DRBD table shows node2's last-cached `peer_disk` (typically UpToDate); fresh `drbdadm status` on node1 shows node2 as `Connecting` |
| `journalctl -f` on node1 | `drbd …: peer disconnected` kernel messages |

## What Bedrock does automatically

Nothing. A secondary outage is not service-impacting: the primary keeps
serving and replication to any other peer (vipet) continues.

## Recovery — clean rejoin

When node2 is powered back on:

1. Boot reaches `multi-user.target`; systemd starts `bedrock-d`,
   `bedrock-rqlited`, `bedrock-mdns`, `bedrock-redirect`, plus
   `node-exporter` and `vm-exporter`. bedrock-net (netd thread) rejoins the
   mesh and re-creates each mesh NIC's `169.254/16` link-local address, so the
   DRBD replication paths come back.
2. bedrock-d's `boot_orchestrator` waits for a quorum role, then
   `_start_local_services` runs `drbdadm up` on each DRBD resource backing a
   VM this node should host (per-VM units are not boot-enabled). DRBD
   re-establishes TCP to each peer and, from the activity-log / bitmap of
   dirty extents, starts a **partial resync** — only the blocks written
   during the outage, not a full copy.
3. When resync finishes, `drbdadm status` shows `UpToDate/UpToDate` at both
   ends and the dashboard flips node2's dot green.
4. VictoriaMetrics scrapes succeed again; metrics tiles repopulate.

No orchestrator action drives the resync — DRBD9's bitmap resync is
self-starting. `_start_local_services` does not promote per-VM resources on
node2; primary/secondary stays with whoever holds the VM, so the rejoin can
never cause dual-primary.

### Timing expectations

- Resync rate is gated by ring bandwidth (DRBD tuning: `resync-rate 100M`,
  `c-min-rate 0`, `c-plan-ahead 0`) and by how much was written during the
  outage.
- Typical: 10–30 s of downtime → 1–5 s of resync. Hours of downtime on a
  write-busy VM → resync proportional to the delta.
- The VM keeps running throughout; the tile shows `SyncTarget` with a percent.

## Recovery — if DRBD stays StandAlone after rejoin

A peer can fail to leave `StandAlone` on its own (after a partition that
`after-sb-0pri discard-zero-changes` could not auto-resolve). Manual kick on
the reconnected secondary:

```bash
drbdadm disconnect <resource>
drbdadm connect <resource>
# or:
drbdadm adjust <resource>
```

For an actual split-brain, see [`split-brain.md`](split-brain.md).

## Impact to the data plane

- **Cattle VMs on node2**: down. Cattle has no replica — its disk is one local
  thin LV on node2, inaccessible until node2 returns. The dashboard shows the
  VM as `shut off` (really: unreachable) because the SSH probe to node2 fails.
- **Pet VMs with node2 as primary**: not this scenario — see
  [`power-loss-primary.md`](power-loss-primary.md).
- **Pet VMs with node2 as Secondary**: running normally on their primary. The
  happy path.
- **Vipet VMs with node2 as Secondary**: still two UpToDate copies (primary +
  the other secondary). Same as pet, with one healthy secondary remaining.

## Related

- [`power-loss-primary.md`](power-loss-primary.md) — the harder case.
- [`node-rejoin.md`](node-rejoin.md) — the node coming back, in full.
- [`split-brain.md`](split-brain.md) — when DRBD cannot auto-heal.
