# Scenario: secondary node power loss

A cluster node that is currently a **DRBD Secondary** for one or more VMs
loses power or crashes hard. No VMs are running on that node (it's a
secondary); the primary keeps serving all I/O uninterrupted.

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

The kernel on node2 is gone — no graceful shutdown, no `drbdadm
secondary`, no libvirt shutdown.

1. **node1** (primary): DRBD detects TCP timeout on the node2 connection
   (~6 s, configurable via `ping-timeout` / `ping-int`). It marks peer
   `node2` as `Connecting` in `drbdadm status`, then progresses to
   `StandAlone` or `Connecting` (depending on wire state). The local
   disk remains `UpToDate`. **Writes to the VM continue uninterrupted**
   — protocol C only waits for peer ACK from reachable peers.
2. **node3** (secondary): same timeout, same state transition. Its disk
   stays `UpToDate`. For a 3-way ViPet VM, replication continues
   primary↔node3 so there are still 2 good copies.
3. **VictoriaMetrics** (on mgmt node, which is node1 in our default
   layout): the scrape of `node2:9100` and `node2:9177` fails → `up=0`
   for those targets. The dashboard tile goes red (Offline).
4. **State push loop** on mgmt: `get_node_info("node2", ...)` raises on
   SSH connect (paramiko EOFError or timeout). The exception path in
   `build_cluster_state` marks the node `online: false` with the error.
5. **Witness** (if configured): sees node2 heartbeat drop. Used by the
   failover orchestrator for primary-loss scenarios — not triggered
   here because node2 was Secondary.

## What the operator sees

| Where | What |
|---|---|
| Dashboard sidebar | node2 status dot goes red |
| `/hosts` page | node2 row: Offline, host IP greyed, memory/load `-` |
| Recent Logs | nothing from node2 (it's dead); no push_log from mgmt either — the state push loop doesn't emit events for node going offline (future improvement) |
| VM detail of any VM with node2 as peer | DRBD table: peer_disk shows the last-known value (typically UpToDate) from cached state; actual state returned by `drbdadm status` on node1 shows node2 as `Connecting` |
| `journalctl -f` on node1 | `drbd …: peer disconnected` kernel messages |

## What Bedrock does automatically

Nothing. A secondary outage is not a service-impacting event. The primary
keeps serving. Replication to other peers (if ViPet) continues.

## Recovery — clean rejoin

When node2 is powered back on:

1. Boot completes. systemd brings up `bedrock-failover` (if deployed),
   `libvirtd`, `bedrock-drbd` NM connection, `node-exporter`,
   `vm-exporter`. This is fully automatic.
2. DRBD: `drbdadm up` runs on each resource (from `/etc/drbd.d/*.res`).
   Resources re-establish TCP connections to peers. DRBD detects the
   bitmap of dirty-on-primary blocks and initiates a **partial resync**
   (not a full resync — the bitmap tracks exactly which extents changed
   during the outage).
3. Once resync completes, `drbdadm status` on both ends shows
   `UpToDate/UpToDate`, and the dashboard flips the node dot green
   again.
4. VictoriaMetrics scrapes start succeeding; metrics tiles repopulate.

The orchestrator does **not** need to run anything. DRBD9's bitmap-based
resync is self-starting.

### Timing expectations

- Resync rate is gated by the DRBD ring bandwidth (10 Gbps on physical,
  whatever the libvirt isolated network gives in testbed) and the
  amount written during the outage.
- Typical: 10–30 s of downtime → 1–5 s of resync. Hours of downtime on
  a write-busy VM → resync proportional to the delta.
- During resync the VM keeps running; the tile shows `SyncTarget` with
  a percentage.

## Recovery — if DRBD is stuck in StandAlone after rejoin

Sometimes a peer fails to leave `StandAlone` on its own (usually after a
network partition that `drbd.conf:after-sb-0pri` couldn't resolve
automatically). Manual kick:

```bash
# on the reconnected secondary
drbdadm disconnect <resource>
drbdadm connect <resource>
# or nuclear:
drbdadm adjust <resource>
```

For a split-brain specifically, see
[`split-brain.md`](split-brain.md).

## Impact to the data plane

- **Cattle VMs on node2**: they're down. Cattle has no replica; their
  disk is on node2's local LV and inaccessible until node2 returns.
  Dashboard shows the VM as `shut off` (actually: unreachable) because
  the state push can't query node2.
- **Pet VMs with node2 as the primary**: not this scenario — see
  [`power-loss-primary.md`](power-loss-primary.md).
- **Pet VMs with node2 as Secondary**: running normally on their
  Primary. This is the happy path.
- **ViPet VMs with node2 as Secondary**: still 2 UpToDate copies
  (Primary + other Secondary). Equivalent to the pet case but with one
  Secondary healthy.

## Related

- [`power-loss-primary.md`](power-loss-primary.md) — harder case.
- [`node-rejoin.md`](node-rejoin.md) — what happens as the node comes back.
- [`split-brain.md`](split-brain.md) — when DRBD can't auto-heal.
