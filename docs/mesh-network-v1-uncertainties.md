# Mesh network v1 — what I'm least sure about

Status after the testbed run on 2026-05-09: 4-node mesh, signed-multicast
discovery, log-driven path table, kernel-route emission, panic-neighbour
catch-all, chaos harness. End-to-end works (loopback ping matrix passes
12/12, average reconvergence ~6 s). What follows is the honest list of
design decisions and assumptions that haven't been stress-tested enough
to bet on. In rough order of "how hard would real hardware bite us."

## 1. The "only mgmt master writes LINK_*" workaround

Right now followers' bedrock-net keeps a complete in-memory neighbour
table for routing decisions but doesn't append LINK_UP/DOWN/QUALITY to
the log. Single-writer is preserved. Cost: the cluster.json paths
section reflects only paths the master has observed (master ↔ each peer);
inter-peer paths (sim-2 ↔ sim-3) don't show up.

This is fine for routing — the routing decisions are local and based on
the in-memory table — but it means the dashboard can't honestly draw
"sim-2 to sim-3 is via mesh-1, 10 Gbps." The fix is straightforward
(followers POST observations to `/api/path-event` on the master, master
appends), but I haven't built it yet. Worth doing in v1.1.

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

## 5. systemd start-limit on bedrock-rust

During init, mgmt_install appends 4-5 log entries in <1 s. The
orchestrator's subscriber fires `render_from_snapshot` after each,
restarting bedrock-rust if daemon.toml changed. Default systemd
start-limit (5 starts in 10 s) hits and bedrock-rust refuses to start.

I bumped the limit to 20/60 via a service drop-in. The real fix is
debouncing in the orchestrator (collect log-entries-arrived signals
for ~250 ms, render once, restart at most once per debounce window).
Untouched in this commit cycle.

**Real-hardware implication**: works fine for normal operation. If a
flapping witness or a misbehaving peer causes thrashing log entries,
the bedrock-rust restart loop could hit the limit. Worth fixing
before scale-up.

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

Currently 5 s up, 30 s down (demo-tuned, not production-tuned). 5 s up
means a flap that recovers in <5 s never logs anything — fine. 30 s
down means a real cable cut takes 30 s to surface as a LINK_DOWN entry
— the gossip layer reacts in <1 s for routing, but the cluster.json
visibility lags.

Real production probably wants 10 s up, 60 s down. I haven't
benchmarked the tradeoff. Anti-flap penalty in the metric calc isn't
implemented yet (mentioned in the design but skipped in v1).

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

## 9. IPv6 not supported

Probe codec, route emitter, loopback identity — all hard-coded to IPv4.
USB4-in-real-life often gets IPv6 link-local with no IPv4 by default;
operators would have to assign IPv4 explicitly. Worth following up if
the v1.0 target is "MS-S1 boxes via USB4 cables out of the box."

## 10. bedrock-rust IPC restart-resilience

The `emit_link_event` retry-on-IPC-error path works (verified by
intentional bedrock-rust restarts). What's not tested: IPC connection
that hangs forever (e.g. deadlocked rust daemon). Right now we'd block
on the rust_ipc.Daemon().__enter__() call indefinitely. A timeout
context manager would be ~10 lines and worth adding.

## 11. Loopback /32 collisions if init/join races

The race in mgmt's loopback allocation is fixed (read used set from
the log, not cluster.json), but the window only closes for a single
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
- Witness integration with the mesh layer
- DRBD using the loopback fallback path block (Phase 6 deferred)
- VM live migration over a mesh path that gets pulled mid-flight
- Cluster scale beyond 4 nodes
- IPv6
- Async return paths (rp_filter=2 is set but no test verified it does
  what we expect under real asymmetry)

## 13. The big "design choice I'd revisit"

The bedrock-net daemon is a separate Python process. Reasonable for v1
because it isolates the netlink/multicast stuff from bedrock-rust's
hot path. But it means we have TWO daemons now per node maintaining
similar state (bedrock-rust knows peers, bedrock-net knows neighbours,
they don't share). Long-term, this should probably collapse into
bedrock-rust — the gossip transport, hysteresis, route emission could
all live there. Doing it in Python first was the right call to iterate
fast; the Rust port is a v1.x cleanup item.

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
  bedrock-c51a36 (192.168.2.60, master): loopback=10.99.0.1/32
  bedrock-4807f4 (192.168.2.62)        : loopback=10.99.0.2/32
  bedrock-606941 (192.168.2.61)        : loopback=10.99.0.3/32
  bedrock-0acd31 (192.168.2.63)        : loopback=10.99.0.4/32

=== Path-table on master ===
  paths: 15            (5 NIC types × 3 peers)
  by NIC pair:
    (br0,    br0   ): 3   ← LAN bridge
    (enp2s0, enp2s0): 3   ← bedrock-drbd plane
    (enp3s0, enp3s0): 3   ← bedrock-mesh-1 plane
    (enp4s0, enp4s0): 3   ← bedrock-mesh-2 plane
    (enp5s0, enp5s0): 3   ← bedrock-mesh-3 plane

=== Routes on master (per-peer multipath) ===
  10.99.0.0/24 via 10.42.65.232 dev enp5s0 metric 999  (panic catchall)
  10.99.0.2 via 192.168.2.62 dev br0    metric 10
  10.99.0.2 via 10.42.209.61 dev enp2s0 metric 11
  10.99.0.2 via 10.42.44.253 dev enp3s0 metric 12
  10.99.0.2 via 10.42.63.196 dev enp4s0 metric 13
  10.99.0.2 via 10.42.65.232 dev enp5s0 metric 14
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

- After init, the mgmt master's log had NODE_LOOPBACK entries for 3
  of 4 nodes — one was lost because the register endpoint's log
  append silently swallowed an IPC connection error (bedrock-rust
  was momentarily restarting via the orchestrator's render-on-entry
  loop). The joiner still got its loopback_ip in the register
  response and claimed it on `lo`; cross-loopback ping works
  (16/16). But cluster.json's nodes section shows only 3 loopback
  IPs because the missing entry never replicated. Easy fix: retry
  the append in register up to N times with exponential backoff.
  Untouched in this commit cycle.

## Next moves

1. Fix the multicast-bridge-forwarding for real hardware (querier or
   broadcast fallback).
2. Implement followers' POST-to-master so cluster.json shows
   inter-peer paths.
3. Real RTT + speed measurement in probes.
4. Phase 6 (DRBD config regen on path-table change) is still pending
   and is the obvious next thing to wire up — DRBD doesn't see the
   mesh yet.
5. Two-node test on the testbed (wasn't covered).
6. orchestrator debouncing for the bedrock-rust restart loop.
