# Post-0.8-alpha rewrite considerations

**Status**: design notes captured 2026-05-16, no commitments. Bedrock has
no production deployments and no external lock-in at this point. This
window is the right time to revisit foundational choices that v1.0+ will
have to live with. Validation of the current 0.8-alpha stack is ongoing
in parallel; nothing here gets implemented until that work has confirmed
where the current design actually stands.

## Why this doc exists

Two structural rewrite questions surfaced in the same design discussion:

1. **Storage backend consolidation** — currently Bedrock runs two S3
   stacks side-by-side (Garage for `scratch`-tier, RustFS for `bulk`
   and `critical` EC tiers), plus `s3fs` FUSE on top of Garage and
   `s3backer` block-device-over-S3 for VM data disks. SeaweedFS could
   in principle replace all of these with one stack: native FUSE
   (`weed mount`), per-collection replication and EC policy, integrated
   S3 gateway.

2. **Cluster-state store consolidation** — currently `bedrock-rust`
   carries its own Raft implementation, weighted-vote witness scheme,
   and hash-chained typed-entry log. If a witness-aware HA layer can
   be defined cleanly enough, the *storage* of cluster state could
   move to a standard distributed KV store (etcd or RQLite), leaving
   bedrock-rust's job reduced to "witness arbitration + emit decisions."

Both questions share a common architectural pattern (a thin witness/
arbitration layer on top of a proven distributed primitive) and could
plausibly share infrastructure (the same etcd cluster could back both
cluster state and SeaweedFS filer metadata). Hence considering them
together.

## What's NOT being proposed

- Replacing DRBD for tier-replicated VM disk storage. DRBD is mature,
  fits Bedrock's 2-way/3-way per-tier model, and the user-space
  alternatives don't.
- Removing the witness arbitration layer. It moves down the stack but
  keeps its job of "decide what physical events happened, who's alive,
  where roles live."
- Changing the cluster-size growth pattern (N=1 → 2 → 3 → 4+witness).
  Both rewrites need to support the same lifecycle.
- Dropping Bedrock's loopback /32 mesh identity scheme. It's working;
  any rewrite composes with it rather than replacing it.

## Constraints any new design must satisfy

- **VMs must never experience disk-IO timeouts during failover.**
  The Linux virtio-blk default timeout (30 s) is what makes this
  sound like a 30 s budget — but it only applies to a VM that's
  RUNNING and issuing IOs. Bedrock's existing fence-responder does
  `virsh suspend` immediately when the fence marker appears, freezing
  the VM at the QEMU level. A suspended VM does no IO and its
  guest-kernel timeout effectively pauses. So the real failover
  budget is bounded by Python's 270 s fence-cleanup window (per
  `cluster-protocol-overview.md` §7), not by the guest's 30 s IO
  timeout. Extending guest IO timeouts (e.g. to 180 s via
  `/sys/block/vda/device/timeout`) is belt-and-suspenders, not the
  load-bearing constraint.

  Concretely the failover sequence is:
  1. Witness TTL elapses (5 s)
  2. bedrock-rust self-fences locally (<1 s): NICs down, marker file
  3. Python fence-responder notices marker (≤1 s with current 1 Hz poll)
  4. Python `virsh suspend` runs (<1 s/VM)
  5. Cluster has up to 270 s to resolve before safety-net reboot

  This loosens the arbiter VM/service startup constraint from "must
  boot in <30 s" to "must come back inside the 270 s budget" — which
  is comfortable headroom for any of the arbiter forms (VM, LXC,
  systemd-service).

- **N=1 must work without any HA infrastructure ceremony.** A
  single-node Bedrock should be installable and useful as-is. HA
  components light up at N=2 and become real at N=3+.

- **Strong consistency for cluster state.** Stale reads of "who's
  the mgmt master" or "where is volume X" are not acceptable. This
  rules out using serializable-only quorum modes for cluster state.
  (For SeaweedFS filer metadata specifically, stale reads of
  largely-static collections like `/models` are operationally
  tolerable; for actively-mutating data they're not.)

- **The witness layer remains a Bedrock-native primitive.** It's the
  one piece that's specifically designed around Bedrock's N=4+witness
  arbitration shape, which no off-the-shelf consensus system models
  natively. Standard Raft (etcd, TiKV, ydb) is strict 1-node-1-vote
  with no witness concept.

## Sketch of the proposed layering — grounded against actual code

**Important clarification surfaced during design discussion**: the
witness in Bedrock is PASSIVE — it does NOT make decisions and does
NOT write to any store. Per `docs/03-witness-and-orchestrator.md`:
"The witness is a passive heartbeat tracker. It does NOT make
decisions. It only answers: 'Who have I heard from recently?'"
Per `docs/cluster-protocol-overview.md` §2 table: "Tiebreak signal
only — never in the commit path."

Decisions are made locally per-node by `bedrock-rust`, using the
witness's STATUS_LIST as one input to `compute_election()` (see
`rust/bedrock-rust/src/witness.rs` and the election logic in
`compute_election` referenced from the overview §6).

```
Layer 0 (external, passive):  Witness service
                              • Receives HEARTBEAT over UDP:12321 (Echo proto)
                              • Answers STATUS_LIST queries with (sender_id, ms_seen)
                              • Lives on a non-Bedrock device — MikroTik container or ESP32
                              • Stateless, in-memory only, no decisions

Layer 1 (per-node, HARD REAL-TIME):  bedrock-rust  (~3k LOC Rust)
                              • Heartbeats witness(es) at ~1 Hz
                              • TCP-probes peers (peer.rs registry)
                              • compute_election() runs locally every tick
                              • Self-fences on TTL exhaustion — purely local
                                decision, NO external store dependency
                              • Writes /run/bedrock-rust.role (contract to Layer 2)
                              • Writes /tmp/bedrock-rust.fence on fence
                              • Owns its append-only log (today)
                              • Knows membership from locally cached state

Layer 2 (per-node, soft real-time):  bedrock-mgmt  (Python)
                              • Reads /run/bedrock-rust.role
                              • Polls /tmp/bedrock-rust.fence at 1 Hz
                              • If role=Leader: accepts mutations, writes
                                to Layer 3
                              • If role=Follower: serves reads from local cache
                              • Reactor reacts to Layer 3 changes

Layer 3 (cluster-wide state store):  TODAY = bedrock-rust's log
                                     FUTURE = could be etcd / RQLite / etc.
                              • Holds operators, obs_backends, tier_state,
                                VM definitions — non-fencing state
                              • NOT in the fencing path under any design
```

### Three invariants any rewrite must preserve

1. **The fencing path is Layer-1-only.** `bedrock-rust` decides to
   self-fence using witness reachability + peer visibility + TTL.
   Layers 2 and 3 must never appear on this hot path. Fencing latency
   stays at ~witness-TTL (5 s default) + sub-second local action.

2. **Layer 1 has no dependency on Layer 3.** `bedrock-rust` reads its
   peer list and election parameters from local cached state, not
   from etcd or any distributed store. If Layer 3 is down,
   `bedrock-rust` keeps electing correctly; the cluster just can't
   commit new state mutations until Layer 3 comes back. Membership
   changes during a Layer 3 outage simply don't propagate until
   recovery — they don't break Layer 1.

3. **Layer 3 availability tracks cluster availability via the arbiter
   pattern.** If Layer 3 becomes etcd, its strict 1-node-1-vote Raft
   quorum doesn't natively understand Bedrock's weighted-vote +
   witness scheme. The arbiter VM/service (whose placement is
   decided by Layer 1's role outcome and propagated via Layer 2)
   gives etcd a "5th voter" controlled by Bedrock's witness-aware
   scheme, so etcd's failure tolerance composes with the cluster's.

The witness never writes to any store. The store never influences
the witness. Decisions emit from Layer 1; the store records their
consequences via Layer 2.

## Timing budget — corrected

### Fencing path (Layer 1 only — what your TTL allows)

| Step | Time | Layer |
|---|---|---|
| Witness heartbeat TTL elapses | 5 s (default) | 1 |
| `compute_election` runs | <1 ms | 1 |
| `ip link down` + write `/tmp/bedrock-rust.fence` | <100 ms | 1 |
| Python fence-responder notices marker (1 Hz poll) | up to 1 s | 2 |
| `virsh suspend` running VMs + `exportfs -au` | 1-5 s | 2 |
| **Total to fully-fenced safe state** | **~7-10 s** | |

No etcd / Layer 3 anywhere on this path. The witness-TTL dominates.
The 1 Hz fence-responder poll could be replaced with inotify for
sub-100 ms detection if the budget gets tight.

### State mutation path (uses Layer 3)

| Step | Time | Layer |
|---|---|---|
| Operator → mgmt API on Leader node | <1 ms | 2 |
| Python verifies role = Leader (read role file) | <1 ms | 2 |
| Python writes to Layer 3 | ~5 ms (local log) / ~10-50 ms (etcd LAN) | 3 |
| Followers observe change | <100 ms (etcd watch) / <1 s (log poll today) | 2 |
| Reactor takes action | varies (DRBD promote: 1-3 s, VM start: 5-15 s) | 2 |

These are soft real-time. They don't gate fencing. The Layer-2 → Layer-3
round-trip is sub-second on a LAN; the dominant cost in any user-visible
operation is the physical action (Layer 2's reactor doing DRBD/VM work),
not the consensus path.

## The arbiter primitive

If etcd or any Raft-based KV becomes the consensus store, Bedrock needs
a way to give the store a number of voters that matches Bedrock's
witness-aware quorum semantics. Three implementations of the same idea
were discussed:

| Form | Failover time | Notes |
|---|---|---|
| Arbiter as a VM (Pet-class, DRBD-replicated disk) | 12–26 s | VM cold-boot dominates |
| Arbiter as an LXC / `systemd-nspawn` container | 6–14 s | No kernel boot |
| Arbiter as a plain systemd service with bedrock-net floating /32 | 5–12 s | No container, no VM; arbiter is just a systemd unit |

The plain-systemd-service form is the simplest and fastest. It depends
on bedrock-net being able to claim/release a /32 in response to
orchestrator commands — likely a small extension to existing mesh code,
worth verifying before committing.

The container form is the cleanest conceptually if the arbiter is
ALWAYS implemented as its own mesh participant — it has its own
bedrock-net daemon, its own loopback /32, and joins the mesh like any
other node. Other nodes never need special "floating IP" logic; the
mesh handles the routing transparently. This requires bedrock-net to
behave correctly with "exactly one direct peer" (the container's host
over a veth or shared bridge) and to converge near-instantly on
triggered updates.

## Bedrock-net implications

For any of these to work with sub-30-second failover, bedrock-net must
deliver routing updates **on change, instantly** — periodic refresh
becomes a cleanup mechanism, not the primary signaling channel.
Specifically:

- Triggered routing updates on every change in this node's table
- Cascade propagation: each receiving peer compares against its own
  table; only re-emits if its decisions also changed (avoids storms)
- Rate-limiting per-source (minimum interval between consecutive
  triggered updates from the same source — typical 100–500 ms — to
  prevent flapping-link storms)
- Periodic full status: every 3–5 s as the safety net for "what if a
  triggered update was lost?"

Worth verifying what bedrock-net does today vs. what this needs. The
gap (if any) is small but real.

The mesh protocol itself may be worth a separate revisit with the
"timing is the constraint" framing — but only if validation of the
current implementation reveals gaps that justify it.

## Storage rewrite considerations

### Current state recap

| Tier | Backend | Activates at |
|---|---|---|
| Cattle (VM disks, no HA) | Local thin LV | N=1 |
| Pet (VM disks, 2-way HA) | DRBD 2-way + XFS | N=2 |
| Pet+ / VIPet (VM disks, 3-way) | DRBD 3-way + XFS | N=3 |
| Scratch (S3, no redundancy) | Garage RF=1 + s3fs FUSE | N=2 |
| Bulk (S3, EC:1) | RustFS REDUCED_REDUNDANCY | N=2 |
| Critical (S3, EC:2) | RustFS STANDARD | N=4 |

Two S3 stacks (Garage + RustFS), a FUSE shim (s3fs), and a block-device
shim (s3backer) for VM data disks on Bulk.

### SeaweedFS as a unifying option

| Capability | Fit for Bedrock |
|---|---|
| Native FUSE (`weed mount`) | Replaces s3fs cleanly |
| Per-collection replication policy | `replication=000` for scratch, `replication=001` for working/models, replication-then-EC for backups |
| Erasure coding | Open-source SeaweedFS hardcodes EC at 10+4 (`weed/storage/erasure_coding/`). No `6+2` or `4+2` configurability in OSS. At 7+ disks, 10+4 distributes cleanly (2 shards/disk) and tolerates 2-node loss at 40% overhead. At 4 disks the math still works but with zero margin after first failure. |
| Volume size | 30 GB default, configurable via `-volumeSizeLimitMB` |
| Hot/cold tiering via disk-type tags | `weed volume -dir=... -disk=ssd,hdd` is operator-declared, not auto-probed; collections target a disk-type at creation |
| Filer metadata HA | Significant operational decision — see below |

### SeaweedFS filer metadata at scale

SeaweedFS filer requires a metadata DB. The OSS-supported backends are:

- Embedded (single-node): `sqlite`, `leveldb`, `leveldb2`, `leveldb3`,
  `rocksdb` (cgo build only)
- Distributed SQL: `postgres`, `mysql`, `sqlserver` (commercial)
- Distributed KV: `etcd`, `tikv`, `redis_cluster`, `redis_sentinel`
- Other: `mongodb`, `cassandra`, `hbase`, `elasticsearch`, `arangodb`

For Bedrock's cluster-size profile, two patterns make sense:

1. **SQLite on DRBD-replicated critical tier** — filer pinned to
   mgmt master, data dir DRBD-replicated, fails over with mgmt role.
   Zero new daemons; uses existing primitives. Reads pause briefly
   during mgmt-master failover.

2. **etcd cluster with arbiter VM/service** — filer can be active on
   multiple nodes simultaneously; read availability survives
   single-node loss without failover. Adds etcd as an operational
   dependency. The arbiter pattern is shared with cluster-state if
   that rewrite happens.

### Storage rewrite known caveats

- SeaweedFS EC is a **post-write conversion**: writes go to replicated
  volumes, sealed volumes are `ec.encode`'d as a separate operation.
  No write-time EC. Requires operator-scheduled cycle (likely cron or
  systemd timer).
- EC volume compaction (reclaiming space from deleted needles in
  encoded volumes) requires `ec.decode → volume.vacuum → ec.encode`
  cycle. Documented as individual commands, not as a single workflow.
  Needs verification under realistic backup-churn workload before
  committing the backup tier.
- Sealed volumes are never written into; updates always go to a new
  active volume, leaving orphaned needles in the sealed/EC volume
  until compaction.
- Volume sizing: SeaweedFS volume server's `-max` flag caps volumes
  per directory. Must be sized to LV capacity (`max_volumes * 30 GB`
  ≤ LV size, with headroom). Wrong cap → ENOSPC mid-write.

### Storage rewrite open questions

- Does `ec.decode → vacuum → ec.encode` work reliably mid-cluster-life
  under load? Benchmark before committing the backup tier.
- What does degraded read latency look like under EC 10+4 with one
  node down at the inference-cluster read load (300+ GB sequential
  model loads, multiple concurrent readers)? Reads should still be
  sub-second-per-stripe with serialization on the slowest of 10
  sources; verify in practice.
- Filer metadata HA: SQLite-on-DRBD vs etcd-backed. Decision couples
  with the cluster-state rewrite if etcd is in the picture for both.

## Cluster-state rewrite considerations

### What bedrock-rust does today

1. Raft-based consensus with weighted votes (10/node + 1/witness)
2. Append-only hash-chained log of typed operations
3. Snapshot fold via view_builder → cluster.json projection
4. Replicated log delivery between nodes

### What changes under "etcd for cluster state"

| Concern | Today (bedrock-rust) | Proposed (witness-only + etcd) |
|---|---|---|
| Consensus and arbitration | bedrock-rust Raft + witness | Smaller bedrock-rust (witness-only) |
| State storage format | Hash-chained append log | etcd KV store |
| Projection to cluster.json | view_builder fold | Direct read of etcd keys |
| Reactor pattern | Log-subscriber with idx tracking | etcd watch |
| HA semantics at N=2 | Weighted witness vote | etcd with arbiter VM (or run single etcd on DRBD) |
| Operational tooling | Custom | `etcdctl` |

### Cluster-state rewrite known caveats

- **etcd is strict 1-node-1-vote.** No weights, no witnesses in the
  standard Raft implementation. Means the witness layer must be
  Bedrock's own and writes its decisions TO etcd (etcd doesn't
  internally know about the witness).
- **At N=2, etcd is worse than 1-node** — quorum of 2-of-2 means
  either failure → write outage. Needs the arbiter VM/service to
  make N=2 work, OR runs as single-etcd on DRBD (same shape as
  SQLite-on-DRBD).
- **Migration from bedrock-rust's log to etcd needs a real story.**
  Dual-write phase, authority flip, log retention for audit. Non-
  trivial but tractable.
- **Application code becomes etcd-watch-shaped** instead of log-
  subscriber-shaped. Different programming model.

### Cluster-state rewrite open questions

- How much hardening work is genuinely open on bedrock-rust today?
  Validation of 0.8-alpha needs to answer this. If significant,
  etcd's prior art (10+ years, k8s control-plane scale) is cheaper
  than finishing. If nearly done, the rewrite is mostly redo.
- Does the witness layer, reduced to "arbitrate physical events
  and emit decisions," fit in <2000 lines of Rust? If yes, the
  shrink is a real win. If the witness logic is irreducibly
  complex, the savings are smaller than they look.
- What's the realistic operator persona for v1.0? Operators who
  already know etcd benefit from the standardization. Operators
  learning Bedrock anyway see less benefit.

## Cross-cutting consideration: the arbiter VM/service as a primitive

If either rewrite (or both) lands, the arbiter pattern becomes
reusable Bedrock infrastructure. Concretely, this is a small new
primitive:

```
floating_services: {
  etcd-cluster-state: {
    type:  arbiter
    image: bedrock-arbiter-etcd
    /32:   100.x.y.99
    placement: follows-elected-leader
    on:    DRBD critical tier
  }
  etcd-seaweedfs-filer: {  # if separate from above
    type:  arbiter
    ...
  }
}
```

How placement actually works (corrected after further design
discussion): for the **arbiter that backs Layer 3 itself** (i.e. the
etcd-cluster-state arbiter), Layer 1 owns the bootstrap end-to-end,
because Python has no Layer 3 to consult until the arbiter is up.
The sequence is:

1. `bedrock-rust` elects (witness + peer visibility + generation
   check against witness's recorded DRBD UUID)
2. `bedrock-rust` decides "I host the arbiter" → DRBD-promote the
   arbiter's data volume → exec the arbiter service start (small
   subprocess invocation, not via Python)
3. etcd quorum forms once the arbiter joins
4. Python becomes useful, reads role file, begins serving mgmt API

For **arbiters of other services** that ride on Layer 3 (e.g. an
arbiter for SeaweedFS filer's etcd, or a Patroni DCS arbiter, or any
floating_service in the table above), Layer 2 (Python) owns
placement and writes the decision to Layer 3 as state. The chain is:
bedrock-rust elects → role file → Python on Leader writes
`floating_services.X.host = <leader>` to Layer 3 → all nodes' Layer
2 reactors see the change → bedrock-net migrates the /32 →
orchestrator on the new host starts the service. No witness writes
anywhere; the witness is one of the inputs to Layer 1's election,
plus the recorder of DRBD generation for the Layer-3-backing arbiter
specifically.

## Decision criteria for committing to any of this

Before committing to either rewrite:

1. **Finish 0.8-alpha validation.** Open work on bedrock-rust and the
   current storage stack should be fully scoped first. "98% done can
   become 30% done very fast" — validation work is exactly where that
   happens.
2. **Benchmark the storage tier's EC compaction cycle** under
   realistic backup churn. If `ec.decode → vacuum → ec.encode` is
   reliable, SeaweedFS is a real option. If it surprises us, that
   shifts the calculation.
3. **Spec the witness layer at the proposed reduced scope.** If it
   doesn't shrink meaningfully when separated from log storage, the
   etcd rewrite's value is smaller.
4. **Verify bedrock-net's triggered-update behavior.** Read the
   current code; identify whether event-driven propagation needs
   adding or already exists.

## Lessons from current design that inform any rewrite

- **DRBD is the right primitive for shared-storage failover at small
  scale.** Don't replace it; build on it.
- **Loopback /32 mesh identity is good.** Any rewrite preserves it.
- **The "two quorum systems" problem is real.** If etcd is the
  cluster-state store, its quorum must align with Bedrock's witness
  arbitration; arbiter VM is the mechanism. The cost of misalignment
  (etcd up but cluster down, or vice versa) is operational confusion.
- **Operator-driven, event-driven, fast.** Triggered updates over
  periodic gossip. Same principle for witness, for bedrock-net, for
  the orchestrator reactor.
- **Single-active is fine if failover is fast.** Most of the
  "distributed databases" question is "do we need multi-active reads,
  or just fast failover for single-active?" For Bedrock's traffic
  profile, fast failover is usually enough. The complexity of
  multi-active is rarely justified.

## Fundamental-issue scan (added after deep reading)

After reading `cluster-protocol-design.md`, `cluster-protocol-v1-plan.md`,
`cluster-protocol-overview.md`, `04-boot-recovery-gaps.md`,
`06-mesh-network.md`, `mesh-network-v1-uncertainties.md`, the full
`witness.rs` lease loop including `compute_election`, and the IPC
contract in `ipc.rs`: the proposed factoring (slim bedrock-rust +
etcd state store + arbiter primitive) has **two genuine corner cases
that need explicit design** but no fundamental architectural blockers.

### Corner case A: arbiter-VM placement is Rust's responsibility

At N=4 with arbiter VM = 5 etcd voters. If a partition splits the
cluster and the arbiter happens to be on the no-witness side, etcd's
quorum lands opposite Bedrock's `compute_election` quorum.

**Critical structural point** (clarified during design discussion):
this is bedrock-rust's job, not Python's. The dependency chain is:

```
Python state ← etcd ← arbiter VM/service ← DRBD promote ← bedrock-rust
```

Python has no useful state to consult until etcd is up, and etcd can't
reach quorum at N=2 until the arbiter is placed. So bedrock-rust owns
the entire bootstrap: elect leader, consult witness for generation
data, decide promote/refuse, DRBD-promote the arbiter volume, exec
the arbiter service start (small subprocess call, not via Python),
wait for etcd quorum, *then* unblock Python.

Partition sequence:
1. bedrock-rust on the no-witness side reaches `NoQuorum` → self-fence
2. NICs down → the old arbiter (if it was on that side) goes dark
3. bedrock-rust on the witness side reaches `Leader`
4. bedrock-rust on the new leader: DRBD-promote the arbiter's disk,
   exec arbiter service start
5. Arbiter rejoins etcd at the same member ID, different physical
   location, etcd accepts the reconnection
6. Etcd quorum restored on the witness side (~20-30 s total)

The arbiter is not "just a VM" — it's a Bedrock-managed primitive
whose placement is bound to the elected leader, and whose bootstrap
is owned by Rust because Python's state store depends on it being up.

### Corner case B: loss of defense-in-depth on single-writer

Today the hash-chained log catches any "follower wrote" violation at
the protocol level (replication refuses to apply across a chain
break). With etcd as the state store, "only mgmt-master writes"
becomes a Python-side discipline (gate every write on `role ==
"leader"`). A Python bug bypassing the gate silently writes to etcd.

Mitigation: use etcd's native lease primitive — the mgmt-master holds
a named etcd lease, every write is conditioned on lease validity. Loss
of leadership → lease expires → writes fail with explicit error.
Well-trodden pattern, but a real regression from "protocol prevents
it" to "discipline plus etcd primitive." Worth documenting in the
implementation plan when the rewrite happens.

### The witness payload morphs, it doesn't disappear

A second key insight: the witness's per-node payload echo
(`epoch`, `last_committed_log_index`, `last_committed_log_hash`)
**looked** obsolete under "etcd handles the log" — but it's not
obsolete, it morphs. The same divergence-detection structure is
exactly what the arbiter's DRBD volume needs at cold-boot and
last-man-standing transitions.

**OLD payload semantic**: `(epoch, last_log_index, last_log_hash)` —
prevents a stale leader from re-electing with an outdated log.

**NEW payload semantic**: `(arbiter_drbd_uuid, drbd_generation,
last_man_standing_marker)` — prevents a stale node from wrongly
force-promoting the arbiter volume and overwriting the peer's
solo-mode progress.

Same mathematical structure (monotonic generation counter + state
fingerprint, third independent observer records both), same defense
pattern, different content. The Rust code stays nearly identical —
only the field interpretations change.

#### Cold-boot scenarios under the morphed payload

**Both nodes boot simultaneously, peer link works**: DRBD itself
resolves this when both Secondary with matching UUID — both sides
agree on the data, one runs `drbdadm primary` (chosen deterministically
by bedrock-rust's lowest-sender_id-with-quorum election), the other
stays connected Secondary. No witness consult needed for the
generation check; the witness only contributes liveness.

**Both nodes boot simultaneously, peer link broken**: each sees only
self + witness. `compute_election` already does the right thing via
`smaller_id_alive_anywhere` — lower-ID node force-promotes (single-
Primary, peer Secondary-disconnected), higher-ID defers. Existing
pattern, no new logic.

**Single node boots alone, peer dead**: this is where the morphed
payload earns its keep. Node A boots, sees no peer heartbeats. Before
force-promoting DRBD and entering last-man-standing, A asks the
witness: "what's the recorded generation?" If A's local DRBD UUID
matches the witness's last-recorded generation → A is up-to-date →
safe to take over → A force-promotes, bumps generation in the witness,
starts arbiter, runs solo. If A's UUID is behind the witness's
recorded generation → A's data is stale → refuse to promote, wait for
peer or operator.

**Last-man-standing handoff (stale node tries to solo)**: A was
last-man-standing, made changes (bumped generation N), crashed. B
boots alone later. B asks witness, sees "generation N owned by A's
UUID X." B's local UUID is older. B refuses to promote. Operator
either revives A, or explicitly forces split-brain resolution.

This last case is the one that **2-node DRBD without a witness gets
wrong** — without the witness's persistent generation memory, B has
no way to know it's the stale side. The witness's generation echo is
the third observer that makes 2-node DRBD safe at last-man-standing
transitions.

#### What this means for the rewrite scope

Witness-protocol-wise: payload field interpretations change, lease
loop structure is unchanged. The "witness records per-node state for
third-observer divergence detection" pattern is preserved end-to-end.

Bedrock-rust-wise: the cold-boot arbiter promotion gets a new
component (consult witness's generation before DRBD-force-promote),
but it's small. The existing fence/election scaffolding handles all
other cases.

DRBD-wise: no changes. Standard `drbdadm primary` semantics; the
witness is consulted *before* the call, not by DRBD itself.

The Phase 7 "snapshot + log compaction" work item still disappears
under etcd, because the snapshot+compaction it referred to was for
bedrock-rust's *own log*, not the arbiter volume. The arbiter volume
is just DRBD; DRBD has no log to compact.

### Items that are solvable, not blockers

- **Witness generalization to fileshare/quorum-read** — current
  `LeaseConfig` already supports `Vec<WitnessSpec>`. Adding a
  `WitnessBackend` trait that abstracts over Echo-UDP and
  SMB/NFS/S3-fileshare backends is incremental code, not a rewrite.
  Quorum-read-before-act semantics is a layer above the per-backend
  protocol.
- **Witness payload size** — Echo HEARTBEAT carries 64 bytes with
  16 reserved; the unused `last_index`/`last_hash` fields can be
  zeroed in an etcd world or repurposed. No protocol change required.
- **Bedrock-net latency on routing changes** — already flagged in
  `mesh-network-v1-uncertainties.md` §1; triggered-on-change updates
  with rate-limiting are incremental work.
- **Bootstrap order at cold boot N=2** — bedrock-rust already elects
  deterministically via lowest sender_id with quorum. Python on that
  node owns etcd init + arbiter placement. Pre-existing pattern.
- **Snapshot/log compaction (Phase 7 deferred)** — disappears entirely
  with etcd; etcd does this internally. Direct win.

### Items that look concerning but aren't

- **Etcd-leader ≠ mgmt-master**. Etcd forwards writes transparently;
  Python on mgmt-master can send writes to local etcd regardless of
  which etcd member is the etcd leader at that instant.
- **Etcd GC pauses**. Hot lease path stays in Rust on bedrock-rust;
  etcd's GC isn't in the fencing path.
- **DRBD-both-Secondary at cold boot**. Previously flagged in
  `04-boot-recovery-gaps.md` as a real gap. The morphed witness
  payload (DRBD UUID + generation) actively solves this for the
  arbiter volume — bedrock-rust's election decides which node calls
  `drbdadm primary`, and the witness's generation echo gates the
  unsafe "force-primary while alone" case. The remaining gap is
  for tier-volumes (Pet/Pet+ DRBD), where the same pattern applies:
  bedrock-rust election decides the primary, no force-promote
  happens unless witness confirms generation is current. The fix
  isn't new architecture — it's wiring existing primitives.

### Validation against actual code (deep read 2026-05-16)

After reading `log_store.rs`, `peer.rs`, the rest of `ipc.rs`,
`config.rs`, `payload.rs`, `log_entries.py`, `view_builder.py`,
`mgmt/orchestrator.py`, `02-drbd-replication.md`, `05-drbd-internals.md`,
the full `06-mesh-network.md`, `mesh-network-v1-uncertainties.md`,
and selected lessons (L27, L28, L35): the rewrite framing holds up
against the actual implementation. Specific observations:

**Morphed-witness validated against DRBD's UUID mechanism.** Per
`05-drbd-internals.md` "Cold Boot Decision Matrix": DRBD's UUID/AL/
bitmap layer **detects** split-brain (both wrote independently →
both have different new UUIDs → "CONFLICT, needs policy") but does
**not prevent** it — by the time the peers reconnect both have
already written. The morphed witness payload `(arbiter_drbd_uuid,
generation, last_man_standing_marker)` fills exactly this gap: it
prevents a stale node from creating the split-brain in the first
place by refusing the unsafe force-promote when the witness's
recorded generation says someone else has moved ahead. The two
mechanisms compose cleanly — witness gates "should I go primary
alone?", DRBD UUIDs gate "what direction is resync?" after reconnect.

**Clean Rust/Python split confirmed by `payload.rs`.** Rust knows
only `Bootstrap = 0x01` and `Opaque = 0x02`. All typed entries
(NODE_REGISTER, TIER_STATE, VM_*, BACKUP_*, LINK_*, etc.) ride as
opaque MessagePack bytes that Python interprets. Under etcd: every
typed-entry constructor in `log_entries.py` (~500 LOC of `encode()`
helpers) is replaced by direct etcd `put()` calls with structured
schemas. The encode/decode layer goes away; the **fold logic in
`view_builder.py` is preserved** (driven by etcd watch events
instead of log replay) — that's the part that's actually load-
bearing for materialising cluster.json.

**Single-writer is currently protocol-level enforced.** L35 confirms
the hash chain's defense-in-depth role concretely: followers' netd
used to call `rust_ipc.Daemon().append()` and the chain divergence
detection caught it — the master's subsequent entries couldn't
replicate forward. This is exactly the safety net Corner Case B
flagged. The protocol-level enforcement is real today; replacing
the log with etcd moves it to "Python discipline + etcd lease."

**Concrete LOC accounting for what disappears.** Under the rewrite:
| File | Today | After |
|---|---|---|
| `rust/bedrock-rust/src/log_store.rs` | 467 LOC | gone |
| `rust/bedrock-rust/src/peer.rs` log replication | ~400 of 533 LOC | gone |
| `rust/bedrock-rust/src/peer.rs` heartbeat/liveness | ~130 LOC | kept |
| `rust/bedrock-rust/src/ipc.rs` Append/Read/Subscribe | ~250 of 383 LOC | gone |
| `rust/bedrock-rust/src/ipc.rs` Status/PeerStatus | ~130 LOC | kept |
| `rust/bedrock-rust/src/payload.rs` | 29 LOC | gone |
| `rust/bedrock-rust/src/witness.rs` | ~600 LOC | kept (payload morph only) |
| `rust/bedrock-rust/src/main.rs` | ~500 LOC | smaller (~250 LOC) |
| `installer/lib/log_entries.py` encode() helpers | ~400 of 515 LOC | gone |
| `installer/lib/view_builder.py` fold logic | 521 LOC | kept (etcd-driven) |
| `mgmt/orchestrator.py` log_subscriber | ~150 of 890 LOC | replaced with etcd-watch subscriber |
| `mgmt/orchestrator.py` rest (boot/fence/reactor/scheduler) | ~740 LOC | unchanged |

Net: ~2000 LOC of bedrock-rust shrinks to ~1000 LOC + ~500 LOC of
Python log_entries.py disappears. The Python orchestrator and
fold logic are mostly preserved — only the IPC layer and the
typed-encoder library change shape.

**Restart-thrashing during init goes away.** Per
`mesh-network-v1-uncertainties.md` §5: today the orchestrator
restarts bedrock-rust after every log entry that changes
`daemon.toml`, hitting systemd's start-limit (5 starts in 10 s)
during burst initialisation. Workaround: bumped limit to 20/60
via service drop-in. Under etcd, bedrock-rust reads peer
membership from etcd directly (or via a SIGHUP-style reload),
not from a static `daemon.toml` regenerated by `render_from_snapshot`.
The restart loop disappears; no debouncing needed.

**No new fundamental blockers surfaced.** Every concern that came
up during the deep read is either:
- Already in this document (single-writer regression, arbiter
  placement, witness generalization)
- Already known and worked around (mesh-network uncertainties,
  systemd start-limit)
- Solved by the rewrite itself (Phase 7 snapshots/compaction,
  daemon.toml regen + restart loop)

The architecture validation is done.

## What this doc is not

- Not a commitment to any rewrite.
- Not a prioritized work plan.
- Not a substitute for actually reading the bedrock-rust and bedrock-net
  source before deciding what to optimize or replace.
- Not a complete design — many specifics (etcd version, exact filer
  metadata DB choice, EC compaction scheduling) need empirical work to
  pin down.

This is the design conversation captured so the same ground isn't
walked again from scratch later. When 0.8-alpha is validated and the
actual remaining work is known, this doc gets revisited with that
context.

## Generalization: the witness+arbiter pattern as a standalone primitive

The pattern that emerged from this design discussion solves a class
of problems wider than Bedrock's own state-store needs. Specifically,
"how do you get strict-Raft quorum to work at exactly 2 physical
nodes" is a known unsolved problem for many systems:

- etcd / k8s control plane at 2 nodes
- Ceph monitors at 2 nodes
- Patroni's DCS at 2 nodes
- Consul at 2 nodes
- HashiCorp Vault at 2 nodes
- Galera / MariaDB clusters at 2 nodes

All of them require 3+ voters. Nobody runs them at 2 because the math
doesn't work — a single-node failure either has no failover (1 of 2)
or no quorum (2 of 2, lose one = down). The standard answer is "add
a 3rd node," which doubles hardware cost for some deployments.

Bedrock's witness+arbiter pattern offers a different answer:

1. **Witness service** — a tiny passive heartbeat tracker on a non-
   Bedrock device (ESP32, switch container, fileshare). Doesn't make
   decisions. Reports liveness.
2. **Witness-aware election** — bedrock-rust's `compute_election`
   with weighted votes (10/node + 1/witness). Picks the surviving
   side of a partition deterministically.
3. **Arbiter primitive** — a Pet-class resource (VM, container, or
   plain systemd service with floating IP) that follows the elected
   leader. The arbiter runs the "extra voter" for any Raft-based
   service that needs odd-count quorum.
4. **Self-fence** — non-witness-side nodes bring their NICs down on
   lease loss, preventing split-brain at the network layer.

If this primitive ships as a small standalone runtime (~few thousand
lines of Rust + Python), it makes etcd / Ceph / Patroni / k8s
controllable at 2 nodes with witness-aware failure tolerance. That's
a real product seam separate from Bedrock-the-VM-platform.

This generalization is not required for Bedrock's own rewrite, but
the design choices that make Bedrock's rewrite clean (small witness
layer, self-fence, arbiter as a Pet-class resource) also happen to be
exactly the choices that would make the primitive reusable. Worth
keeping in mind when scoping the rewrite — biasing toward "clean
generic primitive" over "Bedrock-bespoke" costs little extra and
preserves the option.

## Next concrete validation steps (replaces the earlier "open
implementation questions" framing)

The architectural validation feels done after this scan. What
remains is implementation-level validation that's specific to actual
code, not architecture. In rough priority:

1. **Verify bedrock-rust's witness lease loop is rock-solid**.
   `compute_election` has unit tests; the lease loop's TTL/skew
   handling needs scenario testing. The user has flagged this as
   "not tight yet" — generalizing to multi-backend witnesses is its
   own work item. Whatever shape the witness lease takes, it survives
   the rewrite intact.
2. **Spike Python ↔ etcd subscribing**. Replace `view_builder.py`'s
   log-subscriber with an `etcd-watch` consumer reading from a test
   etcd. Confirms the watch semantics map cleanly to the existing
   subscriber pattern. ~100 lines.
3. **Spec the arbiter Pet-class primitive concretely**. What field
   in cluster.json declares it? How does Python on the leader trigger
   "promote the arbiter's DRBD here and start the service?" What
   happens on simultaneous boot of both nodes? Concrete design, not
   architectural debate.
4. **Spec the etcd-lease single-writer gate**. Which etcd keys are
   write-gated by the lease? How does the lease renew on the master?
   What's the failure mode if the lease expires mid-write?
5. **Confirm bedrock-net's "floating /32" feasibility**. Read the
   current code; identify whether claiming/releasing a /32 that
   doesn't belong to the local node's identity needs new code.

None of these are architectural; they're "go look at the code and
write the precise specification." When 0.8-alpha validation surfaces
its findings, this list gets re-prioritized against whatever those
findings show.

## Arbiter LXC: network attachment

Chosen form for the arbiter (decision 2026-05-16): **LXC container with
rootfs on DRBD `tier-arbiter` volume, bridged into the host's existing
front-end bridge `br0`.** LXC over VM for boot speed (~2-4 s cold start
vs ~10-20 s for a VM); LXC over plain systemd service for clean
isolation and a single deployment unit (rootfs + etcd binary + config
in one image).

### Network topology

```
peer ──[any mesh NIC]── host (current arbiter holder)
                          │
                          ├── br0 ── LXC arbiter
                          │           eth0:  169.254.<cluster>.254/16
                          │           lo:    100.X.Y.254/32
                          │           etcd bound to lo's /32
                          │
                          ├── enp2s0 (mesh)
                          ├── enp3s0 (mesh)
                          └── ...
```

The LXC is bridged into br0 (NOT a dedicated cluster-only bridge).
Reachability composes the way the mesh already does:

- The host's bedrock-net advertises `100.X.Y.254/32` over **every**
  mesh NIC's routing-advertisement protocol (per `06-mesh-network.md`
  §"Protocol 3").
- Peers install a `/32` route via the host's loopback, then via
  whichever physical NIC's metric wins (typically a dedicated mesh
  cable, not br0).
- Br0 itself ALSO carries discovery probes today (`netd.py:151` blocks
  `br-*` prefix but not bare `br0`; `is_bridge_slave()` blocks slaves
  but treats the bridge itself as the routable endpoint). So br0 is
  one more advertised path, ranked alongside the dedicated mesh NICs
  by the receiver-side metric — the receiver picks the dedicated
  cable when both exist.
- **Any working path host↔peer = reachable arbiter.** Front-end NIC
  down → mesh NICs still route to the LXC. Mesh NICs down → front-end
  still routes. Only "every NIC on the host down" makes the LXC
  unreachable, which is identical to "host is gone."

The br0 ↔ LXC bridge link is purely host-internal L2; it never carries
inter-node traffic. So br0's external operational state (does the
upstream NIC have carrier?) doesn't affect the LXC's reachability from
peers — the host's mesh NICs handle that.

### Address assignment

**Every node's br0 gets a deterministic Bedrock link-local in addition
to whatever the operator configured.** The arbiter LXC gets one too.
Without this, the LXC's routing table can't form cleanly — peer
next-hops (each node's br0 address) would be in a foreign subnet
from the LXC's eth0, requiring kernel `onlink` flags as a workaround.
Cleanest if every node on br0 has an address in the same /16 as the
arbiter so standard kernel routing works.

| Endpoint | Address | Notes |
|---|---|---|
| Node N's br0 | `169.254.<cluster_byte>.<node_index>/16` | Added alongside operator-configured IP (DHCP, static, or nothing) |
| Arbiter LXC's eth0 | `169.254.<cluster_byte>.254/16` | Static in LXC config, only assigned by the currently-hosting node |
| Arbiter LXC's lo | `100.X.Y.254/32` | Top of cluster's CGNAT /24 — etcd binds here |

`cluster_byte = sha256(cluster_uuid)[2]` (same derivation as the
cluster's /24 in `06-mesh-network.md` §"Identity"). `node_index` is
the sorted-name index already used in cluster.json. The `.254` octet
is reserved cluster-wide for "the arbiter," parallel to nodes taking
`.1, .2, …` from the bottom. Future cluster-internal services can
take `.253, .252, …`.

No ARP probe needed for any of these because every address is unique
by construction. Operator equipment on the same LAN won't randomly
collide because the cluster_byte derivation keeps the prefix
deterministic-per-cluster (chance of collision across two clusters
sharing a LAN ≈ 0.4%); collisions across third-party 169.254 traffic
are bounded by the standard RFC 3927 defense behaviour of other
implementations (other devices will renumber, not us).

### Adding link-local alongside operator IPs (NetworkManager)

NM's "backoff to link-local on DHCP fail" mode doesn't help here —
it only assigns link-local when DHCP fails, not alongside. The
correct pattern is `ipv4.method=auto` plus a supplementary static
entry:

```bash
nmcli con mod "<br0 connection>" ipv4.method auto \
                                 +ipv4.addresses 169.254.<cluster>.<node_index>/16
```

`method=auto` continues to run DHCP. The `+ipv4.addresses` entry is
treated as supplementary static; it survives DHCP renewals, DHCP
lease loss, and DHCP server unreachability. The interface ends up
with both addresses simultaneously; outbound source-address
selection per route is automatic (operator IP for default-route
egress, link-local for cluster-internal /32 destinations).

For nodes without NetworkManager (systemd-networkd, plain
`/etc/network/interfaces`):

- `systemd-networkd`: list both via repeated `Address=` lines in
  the `.network` file
- `interfaces(5)`: `iface br0 inet manual` plus `up ip addr add
  169.254.<cluster>.<node_index>/16 dev br0` (and parallel DHCP via
  `dhclient` or similar)
- Bedrock-net can manage this directly via netlink on systems
  without NM — a small extension to today's `ensure_link_local`
  path, scoped to br0 specifically

### Operator-IP coexistence

Two IPs on one interface is normal Linux behaviour. The link-local
doesn't interfere with the operator's IP:

- Outbound to operator-LAN destinations: kernel source-selects
  the operator IP (the default route's preferred src)
- Outbound to cluster /32s: kernel installs routes specifying the
  link-local as next-hop, so source-selection picks the link-local
- DHCP renewal: NM rewrites the auto-assigned address; the
  supplementary static entry stays in place

### LXC connectivity inside the cluster

The LXC needs to send Raft messages to other etcd members at their
loopback /32s. With every node's br0 carrying a deterministic
link-local in the same /16 as the LXC's eth0, this is now standard
kernel routing — the LXC sees peer link-locals as L2-adjacent
next-hops, no `onlink` flag, no special-case routing logic.

Three options for HOW the LXC learns peer routes:

**Option A (recommended): LXC runs minimal bedrock-net on eth0.**
The LXC listens for signed UDP multicast probes on its br0-attached
eth0, learns peer loopbacks via the routing-advertisement protocol,
installs kernel routes locally. The `cluster_key` is mounted into
the LXC from the DRBD volume (alongside the etcd data dir and
rootfs). The LXC is a first-class mesh participant identified by
HMAC signature — uniform with how nodes participate. Multicast on
br0 spills onto the operator LAN, same as the rest of the cluster's
br0 discovery, secured by the same `cluster_key` HMAC; no different
from how the mesh already works.

With every node's br0 carrying `169.254.<cluster>.<node_index>/16`
and the LXC at `.254/16`, the routes the LXC installs look natural:
`100.X.Y.<peer_index>/32 via 169.254.<cluster>.<peer_index> dev
eth0` — peer's br0 link-local is the next-hop, and it's in-subnet.

**Option B: LXC uses one of the host's br0 link-locals as a default
gateway.** The currently-hosting node's br0 link-local
(`169.254.<cluster>.<host_index>/16`) is reachable from the LXC by
construction. The LXC's default route is `default via
169.254.<cluster>.<host_index> dev eth0`; the host forwards onward
via mesh. When leadership moves, the LXC reads the new host's
link-local from the LXC's config (set at lxc-start time by the host
that's currently hosting). Avoids running bedrock-net inside the
LXC. Simpler LXC rootfs but requires writing a small config file at
each lxc-start.

**Option C: Host pushes static routes into the LXC at boot.** Host
computes peer loopback list from cluster.json, writes them into the
LXC's network config right before `lxc-start`. Refresh on cluster
membership change. Simplest for tiny static clusters; doesn't scale
gracefully.

Recommendation: **A**, for uniformity with the rest of the mesh.

### Cold-boot bootstrap, concretely

When `bedrock-rust` elects this host as leader (per the layering in
this doc — Layer 1 owns the arbiter bootstrap because Python's state
store depends on it):

| Step | Action | Time |
|---|---|---|
| 0 | Host's br0 link-local `169.254.<cluster>.<this_node>/16` already present (set at first boot, persists) | 0 |
| 1 | `compute_election` → role=Leader | <1 ms |
| 2 | Consult witness generation vs. local DRBD UUID for `tier-arbiter` | network round-trip to witness |
| 3 | `drbdadm primary tier-arbiter` | ~200 ms |
| 4 | Mount LXC rootfs from DRBD volume | ~100 ms |
| 5 | Configure veth + addresses (LXC's `lxc.net.0.*`) | ~50 ms |
| 6 | `lxc-start arbiter` (Alpine + etcd) | ~2-4 s |
| 7 | LXC's bedrock-net probes on br0; sees peer link-locals already there; learns peer routes | ~1-2 s |
| 8 | etcd rejoins cluster (same member ID, new address) | ~1-3 s |
| 9 | Quorum restored; Python unblocked | — |

Total cold-boot: **~5-10 s**. Comfortably inside the 270 s fence-
cleanup budget, and steps 7-8 overlap. The etcd member ID is
preserved across moves because etcd's data dir is on the DRBD
volume that moved with the leader.

Step 0 is the prerequisite this section's "Address assignment"
covers — every node's br0 has its Bedrock-deterministic link-local
permanently assigned from first boot, so the LXC sees a populated
/16 the moment its eth0 comes up. No chicken-and-egg.

## Bedrock-net protocol revisions for the rewrite

Two changes flagged for the rewrite, both incremental to the
existing three-protocol design — neither requires architectural
rework.

### 1. Floor latency contribution below 1 ms

The current `local_metric` in `06-mesh-network.md` §"Path
selection":

```python
def local_metric(bw_mbps, latency_us, loss_rate, age_s):
    bw_cost  = 1_000_000 / max(bw_mbps, 1)       # 12 at 80G → 400 at 2.5G
    lat_cost = latency_us / 100                   # 1 unit per 100 µs
    flap     = 50 if age_s < 60 else 0
    loss     = 500 * min(1.0, loss_rate * 20)
    return bw_cost + lat_cost + flap + loss
```

On a healthy LAN, every path RTT is sub-millisecond. Sub-ms
measurements at userspace timestamping resolution are noisy
enough that the latency term can incorrectly tip the metric
between near-equal paths. Worse, the latency term is small enough
(0.1 to 10 units) to be drowned out by bandwidth cost in normal
operation but big enough to introduce flapping at the margins.

**Change**: floor `lat_cost` to 0 when `latency_us < 1000`. Latency
only enters the metric when paths span genuine distance.

```python
def local_metric(bw_mbps, latency_us, loss_rate, age_s):
    bw_cost  = 1_000_000 / max(bw_mbps, 1)
    lat_cost = max(0, (latency_us - 1000) / 100)   # <1 ms → 0
    flap     = 50 if age_s < 60 else 0
    loss     = 500 * min(1.0, loss_rate * 20)
    return bw_cost + lat_cost + flap + loss
```

For LAN clusters all paths collapse to "bandwidth competition only,"
which is the right thing — local is just local.

### 2. ECMP across equal-bandwidth paths

With latency floored, multiple direct paths to the same peer with
the same bandwidth produce IDENTICAL metrics. Today's
`emit_routes()` installs one route per metric level (`Metric 10..N:
every direct path, ordered by local metric`), so only the lowest-
metric route is used as the primary; the rest are passive backups
the kernel fails over to.

For "every available link in use simultaneously" behaviour — proper
ECMP — emit a single route with multiple `nexthop` entries when
metrics tie:

```bash
ip route add 100.X.Y.2/32 \
   nexthop via 169.254.A.B dev enp2s0 weight 1 \
   nexthop via 169.254.A.C dev enp3s0 weight 1
```

Kernel hashes flows across nexthops. Requires
`sysctl net.ipv4.fib_multipath_hash_policy=1` for L4 (5-tuple)
hashing — important for clusters where a single flow (e.g., one
DRBD connection) shouldn't be silently pinned to one NIC.

Tie-detection rules for ECMP grouping:
- **Same bucketed bandwidth**: bucket `bw_mbps` to nearest 100 Mbps
  (or 1 Gbps for ≥10 Gbps paths) to absorb minor link-speed
  detection noise. Two 10 Gbps cables ≈ identical bucket.
- **Same flap penalty**: both up ≥60 s, or both within the flap
  window — don't ECMP a stable link with a freshly-flapped one.
- **Same loss**: both healthy. Any path with measurable loss
  drops out of the ECMP group and becomes a metric-based
  backup.

`emit_routes()` change-shape:
- Old: one `/32 via X dev nicA metric 10`, plus `/32 via Y dev nicB
  metric 11`, plus `/32 via Z dev nicC metric 12` (one per direct
  path, ordered).
- New: `/32 nexthop via X dev nicA weight 1 nexthop via Y dev nicB
  weight 1` (tied paths grouped), plus separate metric-50+ entries
  for transit fallbacks.

DRBD `path` blocks separately benefit: DRBD's own
connection-per-path machinery becomes effective application-layer
load distribution for replication traffic, complementing kernel
ECMP for non-DRBD protocols.

### 3. Triggered-on-change advertisement (already noted earlier)

Per the existing "Bedrock-net implications" section above: triggered
updates on local routing changes, periodic refresh as cleanup
mechanism. Cadence: ≤100 ms triggered, ~3-5 s periodic. Already
discussed; the latency-floor and ECMP changes compose with it
cleanly — a tie that breaks (a path's bandwidth degrades) triggers
a route re-emission immediately, rather than waiting for the next
2 s advertisement cycle.

### Combined effect

On the testbed's 4-node mesh with 5 NIC pairs per peer
(`mesh-network-v1-uncertainties.md` §13 test run), today's routes
list five metric levels per destination. With the revisions:

- All 5 paths at sub-ms latency, all the same `ethtool` speed → all
  end up with identical bucketed metric → ONE ECMP route with 5
  nexthops.
- Transit paths (via another peer) end up in a higher metric tier
  as fallback.
- Operator sees `ip route show` with a single multipath entry per
  peer destination — cleaner, more honest representation of the
  fabric.

Real-hardware implication: clusters with multiple direct cables now
actually USE all of them simultaneously for non-DRBD traffic
(libvirt migrate, NFS, dashboard inter-node, the future etcd
control plane). Today they're warm spares. Bandwidth aggregation
without operator configuration is a real upgrade.

---

## Locked decisions (2026-05-17)

The architectural design phase concludes here. Decisions below are
**binding** for the v1.0 rewrite. Earlier sections of this doc remain
as the journey-log of what was considered; this section is the
authoritative summary of what was chosen.

### State store and consensus

- **D-01** — Cluster state lives in **rqlite** (on-disk SQLite mode).
  HTTP/JSON wire protocol, MIT licensed, ~25 MB RSS per node,
  inspectable via stock `sqlite3` against the .db file. Bedrock's
  per-hour QPS doesn't need etcd's gRPC watch fidelity; rqlite's
  poll-based subscription is sufficient.
- **D-02** — 3 rqlite instances per HA cluster: one per physical
  node + one **arbiter** instance hosted on the elected master.
  Quorum = 2 of 3.
- **D-03** — Witness arbitration (Layer 1) is **not replaced**.
  bedrock-rust's `compute_election` + weighted votes + self-fence
  stays. Witness layer remains Bedrock-native; rqlite doesn't know
  about it. The arbiter rqlite is what makes rqlite's strict
  1-vote-per-node Raft work at 2 physical nodes — witness handles
  the partition arbitration, arbiter handles the rqlite vote count.

### Arbiter form

- **D-04** — Arbiter rqlite runs as a **bare systemd service**, NOT
  in an LXC. Co-resident with the elected master. The LXC form was
  considered and dropped: it adds rootfs/kernel-boot overhead without
  contributing to consensus correctness.
- **D-05** — Arbiter's network identity is `100.X.Y.254/32` (top of
  cluster /24) — a secondary /32 added to master's `lo` when the
  master role transitions to this host, removed when it transitions
  away. Standard `ip addr add` / `ip addr del` via a small role-change
  hook in bedrock-rust.
- **D-06** — Arbiter's data directory is on a **shared singletons
  DRBD volume** (new tier name `singletons`, 2-way replication
  between physical nodes), mounted at `/var/lib/bedrock/singletons/`.
  Filesystem choice is XFS or ext4 — doesn't materially matter.

### Singleton service co-location

- **D-07** — All cluster-singleton services co-locate on the
  singletons DRBD volume:
    - `rqlite-arbiter` (Raft state + WAL)
    - SeaweedFS filer SQLite (the `weed filer` metadata DB)
    - Any future singleton-shape service that fits this pattern
- **D-08** — Master role transition = atomic move of the singletons
  FS: DRBD-promote + mount + start-all-services on the new master,
  reverse on the old master. Same lifecycle the existing tier-master
  mechanism already implements for `drbd-nfs` tiers.

### Storage backend

- **D-09** — **SeaweedFS** replaces both Garage (scratch) and RustFS
  (bulk/critical). Single S3 stack. Closes the "two S3 daemons"
  problem from the earlier considerations.
- **D-10** — Filer metadata: **SQLite single-instance on the
  singletons DRBD volume** for v1.0. Upgrade path to PostgreSQL via
  `fs.meta.save` / `fs.meta.load` is **bidirectional and documented**
  (project-confirmed: "It is easy to switch between different filer
  stores ... move to distributed one or in reverse"). Migration
  doesn't move any file data — only filer metadata.
- **D-11** — SeaweedFS S3 endpoint is **externally exposed** on the
  cluster's front-end IPs. Bedrock acts as an S3 target for other
  Bedrock clusters, Kopia, awscli, rclone, or any S3 client. Enables
  the "1-node NAS box backs up the 2-node cluster" composition.
- **D-12** — Per-collection replication policy:
    - `scratch` → `replication=000` (no redundancy, ephemeral)
    - `working`, `models` → `replication=001` (one copy on each side)
    - `backups` → `replication=001` (no EC until 7+ nodes; OSS EC
      is hardcoded at 10+4 and only distributes cleanly at 7+ disks)

### Routing layer (bedrock-net)

- **D-13** — Panic catch-all changes from "via freshest neighbour" to
  **"via mgmt-master loopback"**. Routes any cluster-prefix /24
  destination via the current master by default. The arbiter `.254/32`
  flows through naturally; no extra advertisement code needed. Master
  doesn't install a /24-via-self route on itself (would loop) —
  master's table uses a sinkhole or absence at this position.
- **D-14** — Local-metric latency contribution **floors to 0 below
  1 ms**. Sub-ms is noise on a healthy LAN; bandwidth dominates the
  metric.
- **D-15** — **ECMP** across paths with tied metric — kernel
  multi-nexthop routes with `fib_multipath_hash_policy=1` (L4-hash).
  Bandwidth aggregation across all direct cables, not just primary-
  with-warm-spares. Bucketed bandwidth comparison so minor speed-
  detection noise doesn't split ties.

### Witness layer

- **D-16** — Witness payload morphs from `(epoch, last_log_index,
  last_log_hash)` to `(arbiter_drbd_uuid, generation,
  last_man_standing_marker)`. Same mathematical structure (monotonic
  counter + state fingerprint, third independent observer), different
  semantic. Closes the cold-boot DRBD-both-Secondary gap from
  `04-boot-recovery-gaps.md`.
- **D-17** — Witness backend trait abstracts Echo (UDP / MikroTik /
  ESP32) and **fileshare** (SMB / NFS / S3) backends. The single-
  witness lease loop in `witness.rs` already supports `Vec<WitnessSpec>`;
  what's new is the per-backend protocol implementation. Multi-
  backend simultaneous configuration ("Echo+NAS-S3 both active") is
  allowed; explicit quorum-of-witnesses logic (3-of-5 voting across
  witnesses) is **parked for after v1.0**.
- **D-18** — Witness is **critical at the failover moment only**.
  After one side has gone solo and bumped its generation in the
  witness, it can keep running even if the witness becomes unreachable
  ("last-man-standing" mode). Witness availability is not a continuous
  quorum requirement.

### Work-queue model

- **D-19** — The cluster state store doubles as the work queue.
  Operator requests → INTENT rows in rqlite; the owning node observes
  via poll/watch, performs idempotent work, writes OUTCOME rows. This
  pattern is already in `log_entries.py` today
  (`VM_CREATE_INTENT` → `VM_CREATED` / `VM_CREATE_FAILED`); it
  carries forward unchanged into rqlite tables.
- **D-20** — Single-writer enforcement is **application-discipline**:
  only the elected master writes to rqlite state-mutation tables;
  reads are unrestricted. The hash-chain defense-in-depth of today's
  log is intentionally given up; recovered via Python's role-check
  gate at every write site. Bug-class regression flagged; well-trodden
  pattern in k8s + etcd-based systems.

### Operator contract

- **D-21** — User-visible v1.0 promise for S3:
  > *"S3 storage has RF=2 for data and metadata. Brief 5xx errors
  > during cluster maintenance (arbiter failover, planned reboots);
  > all 5xx responses are retry-able. Tested-compatible with
  > retry-aware clients (Kopia, awscli, rclone). PostgreSQL upgrade
  > path available in v1.x for fully-HA filer metadata."*

### Bedrock-rust scope after the rewrite

- **D-22** — bedrock-rust shrinks to:
    - Witness lease loop + `compute_election`
    - Self-fence sequence
    - Peer-liveness TCP heartbeat (NOT log replication)
    - Status / role / peer-status IPC
    - bedrock-net mesh + routing (or that splits to a separate daemon —
      decision deferred, doesn't affect the rewrite)
  **~870 LOC retained**; ~1600 LOC of log + replication code deletes
  (per the earlier LOC-accounting table).

### Out of scope for v1.0

- Multi-witness explicit quorum voting (3-of-5)
- LXC-based arbiter (systemd-service form is the v1.0 default;
  LXC remains a possible future re-encapsulation if isolation
  needs surface)
- PostgreSQL filer metadata (upgrade path documented, default is
  SQLite-on-DRBD)
- Operator UI for witness selection / storage tier settings (parked
  to the dashboard cycle)
- ESP32 witness firmware (Phase 7 of cluster-protocol v1 plan,
  still deferred)
- Multi-link transport in bedrock-rust peer protocol (mesh + ECMP
  obviates this for routing; if needed for direct Raft transport it's
  an additional change)

---

## Rework plan

Roughly sized phases. Each is a coherent unit; sequencing reflects
what must work before the next can be exercised. All sizes are
rough — calibrate after Phase A.

### Phase A — Foundation prep (~3-5 days)

- bedrock-net: change panic-route emission from "via freshest" to
  "via current mgmt-master" (read from snapshot)
- bedrock-net: floor `lat_cost` to 0 below 1 ms in `local_metric`
- bedrock-net: emit ECMP multi-nexthop routes for tied paths;
  set `net.ipv4.fib_multipath_hash_policy=1` via sysctl
- Verify on testbed (existing chaos harness covers paths and
  cross-loopback ping)

### Phase B — rqlite integration (~1-2 weeks)

- Package rqlite binary into install.sh + iso-build payload
- systemd unit for `bedrock-rqlite-node@.service` (binds to node's
  loopback /32, data dir on local LV)
- Python `rqlite_client.py` wrapper around `httpx.AsyncClient`
  (replaces `rust_ipc.py` for state reads/writes)
- Define `bedrock_schema.sql` — one table per current log_entries.py
  type (nodes, tiers, vms, witnesses, params, operators, etc.)
  with appropriate keys and ordering columns
- Re-point `view_builder.py` fold loop to consume from rqlite via
  poll-watch
- **Dual-run validation period**: keep bedrock-rust's log running
  alongside rqlite; write to both, reactor consumes from rqlite,
  diff snapshots periodically to catch divergence

### Phase C — Arbiter mobility (~1 week)

- New tier definition `singletons` (DRBD 2-way replicated, XFS or
  ext4, mounted at `/var/lib/bedrock/singletons/`)
- systemd unit for `bedrock-rqlite-arbiter.service` (binds to
  `100.X.Y.254/32`, data dir at `/var/lib/bedrock/singletons/rqlite/`)
- bedrock-rust hook on role transition:
    - On role=Leader: DRBD-promote singletons → mount →
      `ip addr add 100.X.Y.254/32 dev lo` → start arbiter
    - On role=Follower: reverse sequence
- Scenario tests: cold boot, planned `transfer-mgmt`, kill-master,
  network partition, both-nodes-simultaneous-boot

### Phase D — bedrock-rust shrinkage (~1 week)

- Delete `log_store.rs`, `peer.rs` replication path, `ipc.rs`
  Append/Read/Subscribe handlers, `payload.rs`
- Morph witness payload: `(uuid, generation, marker)` replaces
  `(idx, hash)` in `witness.rs` Echo HEARTBEAT
- Remove the orchestrator's "restart bedrock-rust on daemon.toml
  change" pattern (bedrock-rust now reads peer membership from
  rqlite via Python passing it through `/run/bedrock-rust.peers`
  or equivalent file)
- Verify on testbed: every existing scenario passes

### Phase E — SeaweedFS migration (~2 weeks)

- Package `weed` binary into install.sh + iso-build
- Replace tier-storage Garage scratch path with SeaweedFS
  collection `scratch` (replication=000)
- Replace tier-storage RustFS bulk/critical paths with SeaweedFS
  collections `bulk` / `critical` (replication=001; EC available
  at 7+ nodes per D-12)
- SeaweedFS `filer` service on singletons DRBD with SQLite backend
- SeaweedFS `s3` gateway exposed on cluster front-end /32s
- Migration tooling docs for `weed filer.meta.backup` upgrade path
- Backup-target test from a separate Bedrock cluster

### Phase F — 1-node NAS mode (~3-5 days)

- N=1 configuration with no arbiter, no DRBD (all roles local on
  one node)
- SeaweedFS `replication=000` for all collections at N=1
- Operator docs: "Bedrock as a single-node NAS"
- Test as backup target for a separate 2-node Bedrock cluster

### Phase G — Cleanup + ship (~1 week)

- README, install guide, operator handbook refresh
- Remove deprecated CLI verbs, deprecated config keys
- v1.0 release notes

### Risk callouts

- **Phase B dual-run validation is non-negotiable.** Most rqlite
  migration risk is "subtle semantic differences" — concurrent writes,
  ordering guarantees, watch event delivery. Run both for a real
  week before flipping the cutover switch.
- **Phase C arbiter mobility has the most operational sharp edges.**
  DRBD-promote + mount + service start has many ways to fail mid-
  sequence. Pre-/post-condition checks at each step; reversibility
  on each failure.
- **Phase E SeaweedFS EC compaction** under realistic backup-churn
  workload is still unbenchmarked (see "Storage rewrite known
  caveats" earlier). Run a backup-load test before committing the
  bulk/critical tier to EC mode. v1.0 ships with replication-only
  per D-12, so EC compaction is post-v1.0 concern in practice.

### LOC delta estimate

| | Delete | Add | Net |
|---|---|---|---|
| Rust (bedrock-rust) | ~1600 | ~150 (witness payload morph, /32 mgmt hooks) | **-1450** |
| Python (mgmt + installer/lib) | ~600 (encoders, rust_ipc, log-subscriber paths) | ~800 (rqlite_client, arbiter glue, seaweedfs install) | **+200** |
| Bedrock-net | 0 | ~200 (panic-via-master, ECMP, latency floor) | **+200** |
| Install/config (toml, systemd units, scripts) | ~500 (Garage + RustFS configs) | ~300 (rqlite + SeaweedFS configs) | **-200** |
| **Total** | **~2700** | **~1450** | **-1250** |

Net code reduction ~1250 lines, with the surviving code doing more.
Most of what disappears is reimplementing wheels the rqlite + SeaweedFS
teams have spent years polishing; most of what stays is the actual
Bedrock value-add (witness arbitration, mesh routing, fence sequencing,
floating-singleton mobility).
