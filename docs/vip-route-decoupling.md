# VIP route decoupling — the network layer stops reading "who is master"

*The `.254` cluster VIP is now an ordinary advertised `/32`, originated by
whoever hosts it; the `/24` panic catch-all points at the lowest-octet node, not
the master. Result: `compute_routes()` reads nothing from rqlite — the data plane
converges bottom-up, independent of the control plane.*

---

## 1. The three layers — dependency points down only

```
  ┌─ rqlite ──────────────── whole cluster state (its own Raft)         ▲ depends on
  │                                                                      │ the layer
  ├─ bedrock-d election ──── picks mgmt_master; actuates the arbiter     │ BELOW it,
  │                          (DRBD primary + .254 + arbiter rqlite)      │ never above
  └─ bedrock-net (network) ─ every node learns every path to every      │
                             node. NO leader concept. Converges first.  │
```

If the network layer needed to know the master to route, boot would deadlock:
you need connectivity to elect a master, but you'd need the master to get
connectivity. So **the network layer must converge without any control-plane
fact.** Before this change it didn't — the `/24` catch-all was pinned "via the
mgmt-master," read from rqlite every tick. Two things rode that violation:

1. `.254` delivery (the VIP sat at the top of the `/24`, reachable only through
   the master-pinned catch-all — it had no `/32` of its own).
2. The catch-all's own next-hop selection.

This change splits those two jobs and removes the rqlite read entirely.

---

## 2. `.254` as a connected route — folded into the existing advertisement

The master appends **one** entry to the `paths[]` it already broadcasts every
advertisement cycle. **No new packet, no doubled hello.**

```
  {
    dest:                  "@vip"          # sentinel — can't collide with a hostname
    via_chain:             [master]        # originated here = connected
    bottleneck_bw_mbps:    1_000_000_000   # "4 TB/s" — the identity element
    cumulative_latency_us: 0               # "0.0 µs"
  }
```

It rides the **same** path-vector machinery as every other route. The receiver
composes the metric identically (`bw = min(adv_bw, my_bw_to_adv)`,
`lat = adv_lat + my_rtt_to_adv`):

```
  bw  = min(∞, my_bw_to_master)  = my_bw_to_master
  lat = 0  + my_rtt_to_master    = my_rtt_to_master
```

Advertising **infinite bandwidth / zero latency at the origin** makes the
composed cost to `.254` collapse to *the true cost of reaching the host* — which
is correct, because the VIP **is** at the host. The "connected at 4 TB/s, 0 µs"
framing is literally the identity element of the path-composition algebra; every
hop adds its real cost on top, and nobody special-cases the metric.

Multi-hop propagation, `via_chain` loop detection, backup paths, and free
kernel failover on link-down are all inherited unchanged. The **address** is
resolved locally from `cluster_uuid` (`cluster_addr.cluster_vip`) on install —
never an rqlite/membership lookup.

```
  ORIGINATE   build_advertisement_paths(): append @vip iff i_am_mgmt_master(d)
              (local single-writer role = self-knowledge, not a remote lookup)
  PROPAGATE   recompute_best_transit_paths(): unchanged — @vip is just a key
  INSTALL     compute_routes() transit loop: if dest == @vip,
              dest_lo = cluster_addr.cluster_vip(cluster_uuid)   # pure, no rqlite
  WITHDRAW    on demote the master simply stops emitting the entry → it ages out
              cluster-wide (ADV_STALE_S = 6 s) → the /32 is withdrawn → the new
              host re-originates it. Ownership transfer == re-origination,
              exactly like a loopback /32.
```

---

## 3. The `/24` catch-all — lowest octet, loop-free, master-independent

The panic route now points at **the lowest-loopback-octet node I can directly
reach whose octet is strictly lower than my own.** No rqlite read.

```
  my_octet = last octet of my loopback (== node_index)
  next_hop = min-octet direct neighbour with octet < my_octet
  if none  → install NO catch-all  (I am a local sink: drop unknown traffic)
```

**Loop-freedom by well-ordering.** Every node forwards only toward a *strictly
lower* octet, so packets descend monotonically over the total order on octets
and reach the global-lowest node in ≤ N hops. The global-lowest node has no
lower-octet neighbour → installs nothing → **sinks** unknown traffic (the base
case). No loop is possible, even mid-convergence or under partial connectivity.

```
  full mesh 1..4      every node → 1 in one hop
  line 4-3-2-1        4→3→2→1   (gradient descent, still no loop)
  partition {3,4}     3 is island-lowest → sinks (black-hole = correct on a
                      genuine partition; no consensus protocol can be live, FLP)
  mutual {2,3} only   3→2 then 2 sinks   ← the OLD "freshest peer" rule LOOPED
                      here (each picked the other, bounded only by IP TTL)
```

So the new rule is not just decoupled — it is **strictly more correct** than the
`freshest-neighbour` fallback it replaces, whose own comment admitted "Loops are
bounded by IP TTL."

---

## 4. Why the two changes are complementary

During a failover gap (old host demoted, new host not yet promoted) nobody
advertises `@vip` — the same window the loopback `/32`s blink. The now
master-independent `/24` catch-all is the safety net that keeps the subnet
routable across that gap while `.254/32` re-originates from the new host.

The network layer carries `.254` as opaque learned state. It never has to know
that `.254` *means* "the master." That is the decoupling.

---

## 5. Touch-points (all in existing machinery)

| File | Change |
|---|---|
| `cluster_addr.py` | `ARBITER_VIP_OCTET` + `cluster_vip(uuid)` — single source of the octet |
| `cluster_arbiter.py` | `arbiter_loopback_ip()` now derives via `cluster_addr.cluster_vip` |
| `netd.py` `build_advertisement_paths` | append `@vip` entry when `i_am_mgmt_master` |
| `netd.py` `compute_routes` (transit) | resolve `@vip` address locally |
| `netd.py` `compute_routes` (catch-all) | lowest-octet rule; rqlite read deleted |
| `netd.py` | dead `_mgmt_master_loopback()` removed |

Verified: loop-freedom holds on full-mesh / line / partition / adversarial-mutual
topologies; `cluster_vip` resolves to `.254`; all three modules byte-compile.

See `docs/failover-quorum-aware-follower.md` for the companion control-plane
change (quorum-aware follower).
