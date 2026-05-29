# Mesh network — open limits and untested assumptions

The mesh layer lives in bedrock-d's netd thread (`installer/lib/netd.py`):
signed-multicast discovery, log-driven path table, kernel-route emission,
panic-neighbour catch-all. This is the honest list of limits and assumptions
that production hardware can still bite, in rough order of risk. It is a
caveats list, not a backlog — each item states what is true now.

## 1. Only the mgmt master writes the `paths` table

A follower's netd keeps a full in-memory neighbour table for its own routing
but does not write LINK_UP/DOWN/QUALITY to rqlite — only the mgmt master does
(`persist_link_event` short-circuits to a follower no-op, `i_am_mgmt_master`
gate). This holds the single-writer rule: one appender, no hash-chain
divergence.

Consequence: the rqlite-backed `paths` table reflects only paths the master
observes (master <-> each peer). Inter-peer paths (sim-2 <-> sim-3) are not in
it, so the dashboard topology view is master-centric — each peer shows
"reachable to master", the master shows "reachable to everyone". Full-mesh
visibility would need followers to POST observations to the master for it to
write; routing itself is unaffected (decisions are local, off the in-memory
table).

## 2. Multicast bridge forwarding is fragile

Probes are UDP multicast to `239.7.7.7:7732`. A Linux bridge with
`multicast_snooping=1` and no IGMP querier silently drops unknown-group
multicast, so a peer's mesh NIC sees only its own packets even though
cross-loopback ping works.

The testbed disables snooping on isolated bridges (idempotently, in
`spawn.py` `disable_mesh_snooping()` / `chaos.py` `restore()`).

Real-hardware risk: a managed switch can likewise drop `239.7.7.7:7732`
unless snooping is off or it sees an IGMP querier. A broadcast transport
(255.255.255.255) sidesteps all snooping logic but does not scale past a few
hundred nodes. Open item: detect at boot whether multicast forwards, fall back
to broadcast if not, and log the choice.

## 3. Bridge-slave NIC handling

netd never assigns addresses to a NIC enslaved to a bridge — `is_bridge_port`
checks `/sys/class/net/<nic>/master` (symlink) or `.../brport`, and the bridge
itself (`br0`) is the routable endpoint. This is correct for the common case:
install sets up `br0` with one LAN NIC enslaved so libvirt VMs bridge to the
LAN; other NICs are not bridged.

Not verified: bond-on-top-of-NICs, VLAN-on-bond, or mesh NICs enslaved to a
bridge. The same "expose the bridge, not the slave" logic should apply, but no
test covers these.

## 4. Hysteresis windows are demo-tuned

`UP_HYSTERESIS_S = 5.0`, `DOWN_HYSTERESIS_S = 10.0`. The down window is short
enough that the election self-marks NoQuorum inside the failover window (a
longer down hysteresis would leave an isolated master's `.254` hanging past
the assertion). A flap that recovers in <5 s logs nothing. A real cable cut
takes 10 s to surface as a LINK_DOWN row — gossip-driven routing reacts in
<1 s, but the rqlite-backed dashboard view lags by the down window.

These are tuned for the testbed, not benchmarked on real hardware (the up
window in particular). No anti-flap penalty in the metric calc.

## 5. Path-quality measurement is coarse

`speed_mbps` and `rtt_us` feed the metric/Dijkstra ordering. Both are
measured, with known coarseness:

- Speed (`nic_speed_mbps`): bridges report the min physical-slave speed (the
  kernel hardcodes a bridge to 10000); Thunderbolt/USB4 NICs expose no `speed`
  in sysfs, so netd reports a 15 Gbps honest midpoint so the mesh still
  prefers TB over a 2.5G LAN bridge. virtio NICs read -1/0 and bucket to 0,
  so on the testbed many paths tie on speed and fall through to the
  `(nic_a, nic_b)` name tiebreak — routing is deterministic, just not
  quality-optimised there.
- RTT (`rtt_us`): EWMA from probe round-trip timestamps, with outlier
  rejection (variance / multiplicative / absolute rules), a 3-sample streak
  gate before a degraded path is accepted, and rate-limited BLIP journal lines
  per `(peer, my_nic)`.

Per-NIC link addresses (`link_addr_a` / `link_addr_b`) are in the `paths`
table, emitted from the actual NIC IPs. DRBD `path` blocks list distinct
per-NIC address pairs so DRBD does its own path-level failure detection
independent of kernel routing, with a loopback-fallback path block appended
last as the all-direct-paths-down safety net.

## 6. IPv4 only

Probe codec, route emitter, and loopback identity are IPv4. USB4 links often
come up with IPv6 link-local and no IPv4 by default, so operators must assign
IPv4 explicitly. Relevant to the "MS-S1 boxes via USB4 out of the box" target.

## 7. Loopback /32 collisions if init/join races

Loopback allocation reads the used set from rqlite's `nodes` table
(`cluster_addr.node_loopback_ip`), so a single mgmt master never double-issues
a `/32`. If two nodes briefly both believe they are master (post-failover,
pre-consensus), both could allocate `/32`s for joiners. The single-writer
invariant should prevent it; no test constructs the race.

## 8. What is exercised and what is not

Exercised on the simulated mesh:
- Fresh 4-node init+join, paths discovered + routes installed.
- Cross-loopback ping every-node-to-every-loopback.
- Bridge yank/restore via `ip link set <bridge> down/up` (keeps libvirt's view
  intact and propagates carrier loss to attached vnets, the way a real cable
  cut does — `virsh net-destroy` instead detaches vnets and needs a VM
  re-spawn to repair, so the harness does not use it).
- Loopback IP allocation correctness.

Not exercised:
- Real USB4 hardware.
- Two-node clusters (only 4-node).
- DRBD on the loopback-fallback path block under genuine all-direct-down.
- VM live migration over a mesh path pulled mid-flight.
- Scale beyond 4 nodes.
- IPv6.
- Async return paths under real asymmetry (`rp_filter=2` is set; nothing
  verifies it behaves as expected under real asymmetric routing).

## Solid ground

- Architecture split: identity vs reachability; log for membership, gossip
  for liveness, kernel for routing.
- Signed UDP probe (msgpack + HMAC-SHA256 over `cluster_key`).
- Per-host route entry with metric-ordered multipath backups + a panic
  catch-all.
- Chaos methodology: yank with `ip link`, validate cross-loopback ping every
  event.
- Path-table fold: canonical-key dedup, `observed_at` preserved across
  LINK_QUALITY.

## Consensus peer dialing

Peer addressing uses the chain `loopback_ip -> drbd_ip -> host`, so each peer
is dialed at its `/32` cluster identity and the kernel route picks the best
physical NIC via the mesh's metric-ordered routing. Multipath failover is
uniform across DRBD storage replication and rqlite consensus replication. The
`drbd_ip`/`host` fallbacks stay in the chain so an N=1 master with no loopback
yet (mid-init) still has a dialable address.

## Struct ABI note

Multicast `mreq` structs (`IP_MULTICAST_IF` / `IP_ADD_MEMBERSHIP`) pack as
`4s4sI` — always 12 bytes, fixed-width unsigned int. A bare `L`/`Q` in a
struct format is platform-width (8 bytes on x86_64), which the kernel
silently accepts as wrong-sized and falls back to "default interface",
cross-attributing probes across mesh planes. Ban bare `L`/`Q` in any struct
format on a kernel-ABI path.
