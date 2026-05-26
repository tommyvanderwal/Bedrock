# rqlite vs SQLite-on-DRBD — analysis (draft, then validate)

## Method
Draft candidate gain/loss claims. For each, mark VALIDATED, INVALID,
or NEEDS-CHECK after testing against the code audit and Tommy's
pushback. Strip anything not VALIDATED before responding.

---

## Candidate claims

### G1 — "One consensus layer, not two"
Removing rqlite collapses Tier 0 (witness/election) and Tier 1
(rqlite Raft) into a single load-bearing consensus.

### G2 — "No arbiter rqlited service to operate"
No third rqlited process bound to .254 with separate ports 4011/4012,
no separate data dir on the DRBD mount, no special join sequence,
no rqlite-node-id stability rules (lesson L21).

### G3 — "Writes are faster"
SQLite local + DRBD sync to peer = 1 RTT. rqlite Raft = local
fsync + majority ack via network.

### G4 — "One replication mechanism for everything"
DRBD already replicates VM disks and the cluster FS. Putting cluster
control state on DRBD makes it "just another file." Conceptual
unification.

### G5 — "Strict serializability without coordination"
SQLite's single-writer semantics give serial commits trivially.

### G6 — "No two-tier chicken-and-egg"
Today rqlite arbiter cannot start until Tier 0 elects a master, so
during failover Tier 1 is offline. Removing rqlite removes that
coupling.

### L1 — "You build the consensus you removed"
Master election becomes the only safety mechanism; bugs there are
unrecoverable.

### L2 — "No follower-readable state"
Today every node's local rqlited holds a replica; followers read
cluster state locally. With SQLite-on-DRBD only the master has
the file mounted; followers must dial master for every read.

### L3 — "No write availability during failover"
rqlite resumes writes the moment a new leader is elected
(sub-second). SQLite-on-DRBD blocks until base election +
drbdadm primary + mount + SQLite open.

### L4 — "Split-brain blast radius enlarges"
With rqlite, a bad master election causes localized rqlite
misbehaviour. With SQLite-on-DRBD, a bad election could mean two
nodes both write to the same logical database.

### L5 — "Three voters → two"
Today 2-of-3 voters keep cluster alive even if one per-node rqlite
dies. With SQLite-on-DRBD you lose this — back to whatever the
base election decides.

### L6 — "Lose Raft's read consistency model"
rqlite offers weak/strong/none consistency tunables. SQLite-on-DRBD
has only what the filesystem + app give you.

### L7 — "Backups, snapshots, observability change"
rqlite has built-in snapshot API, log compaction, online backup,
leader-redirect, cluster status APIs.

### L8 — "Cross-cluster replication becomes harder"
rqlite has follower modes for read-only DR replicas. DRBD-based
replication is ship-the-LUN.

### L9 — "Watch/poll design has to be rebuilt"
The `bedrock_meta.revision` counter + watch loop in rqlite_client.py
is elegant for HTTP polling. SQLite-on-DRBD needs file-watch or
distributed-lock invalidation.

### L10 — "view_builder.rebuild() projection model changes"
Today every node generates its local cluster.json from its local
rqlited via view_builder. With SQLite-on-DRBD, only the master
can rebuild; cluster.json on followers needs a different push
mechanism.

### L11 — "Loss of async replication"
Per the audit: at N=2 with master A and follower B, B reads its
local rqlite replica with weak consistency (Raft replication lag
~100ms). With SQLite-on-DRBD this becomes a remote call.

### L12 — "Saga executor cross-node coordination"
Per the audit, the saga executor can run on any node; saga state
is in rqlite operations/operation_steps tables. With SQLite-on-DRBD,
sagas would only run on the master.

---

## Validation pass

### G1 — VALIDATED (with caveat)
True architecturally, but per Tommy's pushback: the base election
is *already* load-bearing for 2-node HA. The "two tiers" is real
but each is independently load-bearing. Net simplification = 1 layer
fewer of consensus machinery + the join sequence + the rqlite
service lifecycle. Keep, with framing adjustment.

### G2 — VALIDATED
Confirmed by audit: rqlite_setup.py + cluster_arbiter.py contain
substantial logic for the arbiter rqlited's separate ports, data
dir on DRBD mount, join order ("local rqlite first to avoid 30s
TCP timeout"), and node-id stability. Lesson L21 in the lessons
log is exactly about a real bug in node-id assignment. Removing
the arbiter eliminates all of this. Keep.

### G3 — INVALID per Tommy + analysis
At N=2, both writes need acknowledgment to all replicas:
- rqlite Raft: leader local fsync + majority ack. With 3 voters
  (per-node-1, per-node-2, arbiter co-located with master), the
  leader + arbiter give 2/3 = local quorum, so commit at local
  disk speed.
- DRBD-SQLite: SQLite local fsync + DRBD Protocol C sync to peer
  (one network RTT).
rqlite can actually commit FASTER than DRBD-SQLite at 2-node
because the arbiter is co-located. DROP.

### G4 — VALIDATED
True. DRBD already does block replication; SQLite on the DRBD LUN
gets that replication "for free." It's a real conceptual
simplification. Keep.

### G5 — INVALID
Both systems give serializability. rqlite via Raft single-leader
+ SQLite single-writer at the leader; SQLite-on-DRBD via filesystem
+ SQLite single-writer. No real gain. DROP.

### G6 — VALIDATED (this is the strong one)
Audit confirms the chicken-and-egg is real: rqlite arbiter rqlited
cannot start until DRBD is Primary and `.254` is bound — which
only happens after the base election completes. During failover,
Tier 1 is genuinely offline. Removing rqlite eliminates this gap.
Keep — this is the architectural prize.

### L1 — INVALID per Tommy
Master election is already the load-bearing decision for 2-node HA;
the second consensus layer doesn't reduce the safety burden, it
just adds machinery on top. DROP.

### L2 — VALIDATED (this is the strong loss)
Audit explicitly confirms: per-node rqlited at 127.0.0.1:4001,
weak-consistency reads everywhere (mgmt/app.py:1201,1273,
view_builder, orchestrator watch loops, bedrock CLI). Followers
genuinely read from local replicas without dialing master. This
is a real loss that affects every follower-side read path. Keep.

### L3 — INVALID at 2-node
At N=2: master A dies. Per-node-1 + arbiter both gone. Only
per-node-2 on B remains = 1 voter, NOT quorate. rqlite writes
blocked until arbiter restarts on B (after B is elected and
promotes). So rqlite has the SAME failover-window unavailability
as DRBD-SQLite at 2-node. DROP.
Note: at N=3+ this loss would re-apply; flag for the 3+-node case.

### L4 — INVALID
Both systems prevent split-brain writes via their underlying
primitive:
- rqlite: Raft majority requirement (a partitioned node can't get
  acks, so can't commit).
- DRBD-SQLite: drbdadm primary refuses on 2nd node
  (allow-two-primaries=no).
The "blast radius" framing is wrong. DROP.

### L5 — INVALID per Tommy
The witnesses are already load-bearing for 2-node HA regardless
of rqlite. The "3rd voter" argument is weak because at 2-node
during master failover, the arbiter is unavailable anyway (it
runs on the dying master). DROP.

### L6 — NEEDS-CHECK → DOWNGRADE TO MINOR
rqlite's strong/weak/none consistency tunables ARE real. But the
audit shows only one specific use of strong consistency (bedrock
status's replication-wait at line 581). Everywhere else uses
weak. So the consistency model is mostly used as "follower reads
local" — which loss L2 already covers. The standalone "lose
consistency tunables" is double-counting. DROP as separate item.

### L7 — INVALID per Tommy
Without a Raft log, there's no Raft log to compact. SQLite has
its own VACUUM and WAL checkpoint. The other items (snapshot API,
online backup, leader-redirect, cluster status APIs) are rqlite
HTTP endpoints. Replacement: file-level snapshot via LVM
(already in Bedrock toolkit) and direct SQLite access. Not a
real loss for Bedrock's use case. DROP.

### L8 — DOWNGRADE TO MINOR
True that rqlite has read-only follower modes for DR. Bedrock has
no DR-replica feature today and none in the v1.0 plan. Speculative
future loss; not load-bearing. DROP from main list.

### L9 — VALIDATED
Audit confirms the revision-counter watch loop is the
propagation mechanism for "cluster state changed, regenerate
cluster.json on every node." Without rqlite, this needs a new
mechanism. Real work. Keep.

### L10 — VALIDATED (related to L2 + L9)
view_builder.rebuild() reading local rqlite to write local
cluster.json+state.json on EVERY node is exactly how follower
nodes know who the master is, who the peers are, etc. Without
local rqlite, followers can't autonomously rebuild. Either they
mount the master's SQLite read-only (not how DRBD works without
shared-disk filesystem), or master pushes cluster.json to peers
via mesh (new mechanism). Keep, but it's the same loss as L2
restated for a specific code path. Merge into L2.

### L11 — DUPLICATE of L2
DROP.

### L12 — VALIDATED
Audit: saga executor in rqlite_backend.py uses rqlite for
operations/operation_steps tables. With SQLite-on-DRBD, sagas
can only run on the master. Today they can run on any node
(though writes go through Raft). Real architectural change.
Keep, but note: most sagas are master-driven anyway, so the
operational impact may be small.

---

## Surviving validated points

### Gains
- G2 — removing arbiter rqlited's lifecycle and join complexity
- G4 — one replication mechanism for cluster control state
- G6 — eliminating the two-tier chicken-and-egg dependency

### Losses
- L2 (incl. L10, L11 merged) — followers lose autonomous local
  reads; reads/projection that today happen on every node must
  either dial the master or use a new push mechanism
- L9 — the revision-counter watch loop has to be rebuilt
- L12 — saga executor can only run on the master (likely minor)

### Open questions to think through, not for the answer
- For 3+ node clusters, L3 returns as a real loss
- L8 (DR replicas) is speculative future
