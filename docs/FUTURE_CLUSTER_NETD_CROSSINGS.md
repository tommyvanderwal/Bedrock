# Future cluster ↔ netd cross-layer improvements

The cluster thread (`lib/cluster_daemon.py`) and netd thread (`lib/netd.py`) are
**intentionally independent** after the first split. Each owns its primary purpose:

| Layer | Thread | Primary job |
|-------|--------|-------------|
| netd | `bedrock-netd` | Mesh probes, TCP routing sessions, route emit, ICMP/L2 |
| cluster | `bedrock-cluster` | Protocol-4 election HB, witness, quorum, arbiter promote/demote |

They coordinate only through `BedrockState` (`cluster_lock`, `netd_lock`) for
fields that must be shared (fence evidence, election outcome, witness state).

This document lists **deferred** optimizations where combining signals from both
layers could improve failover behaviour. Do not wire these until deliberately
reviewed — premature coupling reintroduces the old monolith timing.

---

## 1. Death oracle + mesh reachability combined

**Today:** Master is hidden from election only when protocol-4 HB misses **and**
the witness slot shows the master is no longer HOSTING.

**Possible improvement:** Also require (or weight) mesh `Neighbour.logged_up` /
`last_seen` before treating the master as gone. Would reduce false takeover when
HB is lost but the data path is still healthy (or vice versa).

**Risk:** Re-couples election to netd's 10s DOWN_HYSTERESIS — may slow legitimate
failover.

---

## 2. DRBD fence fast heal via probe `last_seen`

**Today:** `drbd_down_peers` entries expire on TTL in the cluster thread only.
A peer that comes back on the mesh is not explicitly cleared from fence evidence.

**Possible improvement:** When netd sees fresh probes from a peer whose octet is
in `drbd_down_peers`, clear that entry early so election re-admits the peer
without waiting for TTL.

**Risk:** DRBD and mesh can disagree briefly after a partition; clearing too
early could admit a ghost peer.

---

## 3. `ever_seen_peers` floor for quorum during mesh-down

**Today:** Cluster peer liveness comes from election HB only. netd's
`ever_seen_peers` is used elsewhere (e.g. vm_failover) but not in `_election_tick`.

**Possible improvement:** During netd startup or HB-only partitions, seed
`peer_liveness` from `ever_seen_peers` or last-known loopbacks so quorum
denominator doesn't collapse while HB is still converging.

**Risk:** Stale membership could block NoQuorum when a node is truly gone.

---

## 4. Event-driven election depth vs 1 Hz polling

**Today:** Cluster thread polls at 250 ms, election at 1 Hz.

**Possible improvement:** Trigger an extra election tick on HB receive, DRBD fence
feed, or witness slot change instead of waiting up to 1 s.

**Risk:** Thundering herd under flapping links; needs rate limits.

---

## 5. VIP / mgmt route origination coordination

**Today:** netd emits transit routes from TCP mesh RIB; cluster drives arbiter
promote and mgmt_master in rqlite independently.

**Possible improvement:** Coordinate VIP origination so only the elected leader's
netd advertises the mgmt VIP path, or suppress stale leader routes faster.

**Risk:** Routing blackholes if election and RIB updates race.

---

## 6. Unified peer table for dashboard / orchestrator

**Today:** Dashboard mesh view reads `state.netd`; election outcome reads
`state.last_election_outcome` from cluster.

**Possible improvement:** Merge a read-only "peers" view under one lock for API
consumers (mesh RTT + HB freshness + witness slot in one payload).

**Risk:** Lock contention; mostly ergonomics.

---

## 7. Mesh-informed witness probe targets

**Today:** Cluster witness probes use configured Echo addresses + broadcast;
netd knows which NICs reach which subnets.

**Possible improvement:** netd publishes best egress per witness IP so cluster
doesn't probe from the wrong interface.

**Risk:** Low; mostly operational polish.

---

## Implementation note

When adding any crossing:

1. Document the signal direction (netd → cluster, cluster → netd, or both).
2. Choose the lock (`netd_lock` vs `cluster_lock`) explicitly.
3. Add a test that fails if the coupling is removed accidentally.
4. Update this file — move the item to "Done" or delete if rejected.
