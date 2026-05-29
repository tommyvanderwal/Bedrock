# Mesh network v1 — what I'm least sure about

Status after the testbed run on 2026-05-09: 4-node mesh, signed-multicast
discovery, log-driven path table, kernel-route emission, panic-neighbour
catch-all, chaos harness. End-to-end works (loopback ping matrix passes
12/12, average reconvergence ~6 s). What follows is the honest list of
design decisions and assumptions that haven't been stress-tested enough
to bet on. In rough order of "how hard would real hardware bite us."

## 1. The "only mgmt master writes LINK_*" workaround

Right now followers' netd keeps a complete in-memory neighbour
table for routing decisions but doesn't record LINK_UP/DOWN/QUALITY to
rqlite. Single-writer is preserved. Cost: the path data in rqlite
reflects only paths the master has observed (master ↔ each peer);
inter-peer paths (sim-2 ↔ sim-3) don't show up.

This is fine for routing — the routing decisions are local and based on
the in-memory table — but it means the dashboard can't honestly draw
"sim-2 to sim-3 is via mesh-1, 10 Gbps." The fix is straightforward
(followers POST observations to `/api/path-event` on the master, master
writes to rqlite), but I haven't built it yet. Worth doing in v1.1.

**Real-hardware implication**: dashboard topology view will be
master-centric. Operator sees "I'm reachable to everyone" on the master,
"I'm reachable to master" on each peer. The full mesh visibility waits
on the v1.1 follower-forward.

## 2. Multicast bridge forwarding is fragile

The first chaos run "passed" without exercising mesh paths at all
because Linux bridges silently drop unknown-group multicast when
`multicast_snooping=1` and no IGMP querier is present. Discovered when
pinging confirmed cross-loopback worked but tcpdump on a peer's mesh NIC
showed only its own packets.

For the testbed I disable snooping on all isolated bridges (idempotently,
in `spawn.py`'s `disable_mesh_snooping()` and `chaos.py`'s `restore()`).

**Real-hardware implication**: a managed switch in the rack might also
silently drop our `239.7.7.7:7732` multicast unless IGMP snooping is
either disabled or the switch sees an IGMP querier. A passive workaround
is to switch the probe transport to broadcast (255.255.255.255) — works
on every switch, no snooping logic — but loses scalability past a few
hundred nodes. The right answer for v1: detect at boot whether multicast
is forwarding, fall back to broadcast if not, log the choice prominently.

## 3. virsh net-destroy semantics

The chaos harness's first version used `virsh net-destroy` to simulate
a cable yank. That deletes the bridge AND detaches every VM vnet from
it; `net-start` recreates the bridge but doesn't re-attach the vnets.
The VMs end up running with NICs that have a kernel-up state but no L2
connectivity, and the only way to repair was a full VM
destroy + re-spawn.

The harness now uses `ip link set <bridge> down/up` instead, which keeps
libvirt's view intact and propagates carrier loss to attached vnets the
same way a real cable cut would. That's what we wanted from the start
but only learned after the failed-test forensics.

**Real-hardware implication**: none for production (we don't run virsh
on real hardware). But the chaos test prior to this fix was effectively
testing only the LAN path — "63 events 0 failures" was an artefact, not
a result. The new `ip link` approach genuinely cuts the path.

## 4. Bridge-slave NIC handling

bedrock-net was assigning throwaway IPs to bridge slaves (interfaces
enslaved to the install-time host bridge `br0`). That fights the bridge.
The fix detects `/sys/class/net/<nic>/master` is a symlink → skip; treat
the bridge itself (`br0`) as the routable endpoint.

**Real-hardware implication**: depends on whether the operator's host
configuration involves bridges. On a typical Bedrock node the install
sets up `br0` with `enp1s0` enslaved (so libvirt VMs can be bridged out
to the LAN). Other NICs aren't typically bridged. The current logic
handles this correctly; if someone has an unusual setup with mesh-NICs
also enslaved to a bridge, they'd want to expose the bridge, not the
slave — same logic applies. Tested on the testbed; not stress-tested
on, say, a bond-on-top-of-NICs or a VLAN-on-bond config. Both should
work, neither is verified.

## 5. systemd restart thrash on rapid state changes (historical)

This one is moot under the unified `bedrock-d`. Historically, during
init mgmt_install drove several cluster-state changes in <1 s, and the
orchestrator's subscriber re-rendered config and restarted a separate
daemon after each, tripping the default systemd start-limit (5 starts
in 10 s). Now mesh/election/routing all live inside the long-running
`bedrock-d` netd thread, so there is no per-change daemon restart to
thrash. The general lesson stands: debounce config re-renders (collect
state-changed signals for ~250 ms, render once) if any future actuation
restarts a unit on every change.

## 6. The `struct.pack("4sLi", ...)` bug

This is the kind of bug that earns its own paragraph. `L` in Python's
struct is "unsigned long" — 4 bytes on 32-bit, 8 bytes on 64-bit Linux,
4 bytes on Windows. The old code packed `IP_MULTICAST_IF` /
`IP_ADD_MEMBERSHIP` mreq structs as 16 bytes on x86_64, which the
kernel silently accepted as wrong-sized and fell back to "default
interface." Symptom: probes appeared cross-attributed across mesh
planes — paths between physically-incompatible NICs ended up logged.

Fix: explicit `4s4sI` (always 12 bytes, 4-byte unsigned int regardless
of platform). I'd suggest a lint rule that bans bare `L`/`Q` in struct
format strings in any code path that interfaces with the kernel ABI.

**Real-hardware implication**: bug doesn't depend on hardware; would have
hit the same way on real boxes. Already fixed.

## 7. Hysteresis windows

Currently 5 s up (`UP_HYSTERESIS_S`), 10 s down (`DOWN_HYSTERESIS_S`).
The down window was 30 s originally but got shortened to 10 s so the
election self-marks NoQuorum inside the failover window (a 30 s down
hysteresis left the isolated master's `.254` hanging well past the
assertion). 5 s up means a flap that recovers in <5 s never logs
anything — fine. 10 s down means a real cable cut takes 10 s to surface
as a LINK_DOWN entry — the gossip layer reacts in <1 s for routing, but
the rqlite-backed dashboard visibility lags by the hysteresis window.

These are still demo-tuned, not benchmarked across real hardware; the
up window in particular hasn't been tuned. Anti-flap penalty in the
metric calc isn't implemented yet (mentioned in the design but skipped
in v1).

## 8. No path-quality measurement

`speed_mbps` and `rtt_us` are recorded as 0 because we read
`/sys/class/net/<nic>/speed` (always returns -1 / 0 for virtio) and we
don't actually measure RTT in the probe round-trip. The design says
bucketed quality drives Dijkstra ordering; with all values 0, every
path ties on speed and falls through to the (nic_a, nic_b) name
tiebreak. Routing still works (deterministic, just not
quality-optimised) but doesn't pick the "best" path in any meaningful
sense.

The fix is two simple things: include the sender's `ethtool` speed in
the probe payload (so receivers learn the peer's link speed, not their
own), and timestamp send/receive in probes for RTT. Maybe a day's work.

**UPDATE 2026-05-10**: Per-NIC link addresses are now in the path
table (`link_addr_a` / `link_addr_b` in `LINK_UP` / `LINK_QUALITY`),
emitted by netd from the actual NIC IPs and folded into the snapshot
canonicalised. DRBD's `path` blocks now list distinct per-NIC
addresses (e.g. `10.42.10.1` ↔ `10.42.10.2` for the enp3s0 pair, etc.),
so DRBD does its own path-level failure detection independently of
kernel routing. A loopback-fallback path block is still appended last
as the "if every direct path fails" safety net. Verified live: the
master's generated drbd.conf has 5 direct path blocks per master-peer
pair on the testbed, each with the correct per-NIC address pair.

## 9. IPv6 not supported

Probe codec, route emitter, loopback identity — all hard-coded to IPv4.
USB4-in-real-life often gets IPv6 link-local with no IPv4 by default;
operators would have to assign IPv4 explicitly. Worth following up if
the v1.0 target is "MS-S1 boxes via USB4 cables out of the box."

## 10. cross-daemon IPC restart-resilience (obsolete)

This concern is gone with the unified daemon. It used to describe the
`emit_link_event` retry path against a separate Rust daemon over IPC,
where a hung/deadlocked peer process could block the netd loop
indefinitely. There is no longer a second daemon or an IPC boundary:
mesh, election, and routing run in the same `bedrock-d` process as
in-memory function calls, so there is no IPC connection to hang on.

## 11. Loopback /32 collisions if init/join races

The race in mgmt's loopback allocation is fixed (read the used set
from rqlite's `nodes` table), but the window only closes for a single
mgmt master. If two nodes simultaneously decide they're the master
(post-failover, before consensus settles), they could both try to
allocate /32s for joiners. The single-writer invariant is supposed to
prevent this, but I haven't constructed a test for it.

## 12. What I tested and what I didn't

**Tested**:
- Fresh 4-node init+join on simulated mesh, paths discovered + routes
  installed
- Loopback ↔ loopback ping cross-cluster (12/12)
- Bridge yank/restore via `ip link set down/up`, cluster recovery
- Loopback IP allocation correctness (post-race-fix)
- Discovery + DRBD/IPC integration is dormant — DRBD isn't promoted
  to N≥2 anywhere in this work

**Not tested**:
- Real USB4 hardware
- Two-node clusters (only ran 4-node)
- DRBD using the loopback fallback path block under real all-direct-paths-down
- VM live migration over a mesh path that gets pulled mid-flight
- Cluster scale beyond 4 nodes
- IPv6
- Async return paths (rp_filter=2 is set but no test verified it does
  what we expect under real asymmetry)

## 13. consensus peer dialing — RESOLVED 2026-05-11

The external code review (2026-05-11) flagged that consensus peer
addresses were built from `n.get("drbd_ip") or n.get("host", "")`,
which meant cluster-protocol replication rode the mgmt LAN regardless
of what the mesh layer had discovered. DRBD storage replication got
the full mesh benefit; the consensus traffic didn't.

**Fix landed**: peer addressing uses the preference chain
`loopback_ip → drbd_ip → host`, so each peer is dialed at its `/32`
cluster identity and the kernel route picks the best physical NIC via
the mesh's metric-ordered routing. Multi-path failover is uniform
across DRBD and consensus replication. (After the May-2026 rewrite the
consensus layer is rqlite over the `/32` identities; the loopback-first
preference chain carried over unchanged.)

The legacy fallbacks (drbd_ip / host) stay in the preference chain
so clusters that pre-date the mesh layer keep working, and N=1
clusters whose master genuinely has no loopback yet (mid-init race)
still get a dialable address.

## 14. The big "design choice I'd revisit" — RESOLVED (May-2026 rewrite)

This used to call out a two-daemon split (a separate Python mesh daemon
alongside the old Rust consensus daemon) with each maintaining its own
peer/neighbour state and no sharing. The May-2026 rewrite collapsed
everything into a single Python daemon, `bedrock-d`: mesh discovery,
latency, route emission, election, and witness IO all run in its netd
thread, sharing in-memory state directly with the asyncio
mgmt/orchestrator side. No second process, no IPC, no duplicated state.
The earlier "port the mesh into the consensus daemon" cleanup item is
done — just in Python, and the Rust daemon is gone.

## What I'm confident in

- The architecture decomposition: identity-vs-reachability, log for
  membership, gossip for liveness, kernel for routing.
- Signed UDP probe + cluster_key auth.
- Per-host route entry with metric-ordered backups.
- The chaos test methodology (yank with `ip link`, validate
  cross-loopback ping every event).
- The path-table fold logic (canonical-key dedup, observed_at
  preservation across LINK_QUALITY).

## Test run — 2026-05-09/10

Final cleaned 4-node testbed:

```
=== Per-node view (post chaos) ===
  bedrock-c51a36 (192.168.2.60, master): loopback=100.86.181.1/32
  bedrock-4807f4 (192.168.2.62)        : loopback=100.86.181.2/32
  bedrock-606941 (192.168.2.61)        : loopback=100.86.181.3/32
  bedrock-0acd31 (192.168.2.63)        : loopback=100.86.181.4/32

=== Path-table on master ===
  paths: 15            (5 NIC types × 3 peers)
  by NIC pair:
    (br0,    br0   ): 3   ← LAN bridge
    (enp2s0, enp2s0): 3   ← bedrock-drbd plane
    (enp3s0, enp3s0): 3   ← bedrock-mesh-1 plane
    (enp4s0, enp4s0): 3   ← bedrock-mesh-2 plane
    (enp5s0, enp5s0): 3   ← bedrock-mesh-3 plane

=== Routes on master (per-peer multipath) ===
  100.X.Y.0/24 via 10.42.65.232 dev enp5s0 metric 999  (panic catchall)
  100.86.181.2 via 192.168.2.62 dev br0    metric 10
  100.86.181.2 via 10.42.209.61 dev enp2s0 metric 11
  100.86.181.2 via 10.42.44.253 dev enp3s0 metric 12
  100.86.181.2 via 10.42.63.196 dev enp4s0 metric 13
  100.86.181.2 via 10.42.65.232 dev enp5s0 metric 14
  ... (same metric-ordered chain for .3, .4)

=== Cross-loopback ping (post-chaos) ===
  16/16 OK (every node → every loopback including self)

=== Chaos run ===
  events: 32 (random yank/restore via ip-link, mixed planes)
  validation failures: 0
  reconvergence: avg 6.2 s, max 15.2 s
  final state: ok
```

## Known issue surfaced during the run (not a blocker, worth noting)

- After init, the mgmt master had loopback entries for 3 of 4 nodes
  — one was lost because the register endpoint's write silently
  swallowed a connection error to the (then-separate) consensus
  daemon, which was momentarily restarting via the orchestrator's
  render-on-change loop. The joiner still got its loopback_ip in the
  register response and claimed it on `lo`; cross-loopback ping works
  (16/16). But the recorded nodes set showed only 3 loopback IPs
  because the missing entry never replicated. Easy fix: retry the
  register write up to N times with exponential backoff. (Both the
  separate-daemon restart loop and the swallowed-write window are
  gone under the unified `bedrock-d` + rqlite consensus.)

## Next moves

1. Fix the multicast-bridge-forwarding for real hardware (querier or
   broadcast fallback).
2. Implement followers' POST-to-master so the dashboard topology
   (backed by rqlite) shows inter-peer paths.
3. Real RTT + speed measurement in probes.
4. Two-node test on the testbed (wasn't covered).
