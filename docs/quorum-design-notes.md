# Quorum design — rationale notes

Why the master-election state machine is shaped the way it is. The
load-bearing protocol lives in the canonical specs
(`cluster-quorum-spec.md`, `cluster_arbiter.md`, `election.md`,
`witness.md`); this file is the design reasoning behind it.

State names are spelled in full — `LEADER`, `FOLLOWER`, `NOQUORUM` —
in both code identifiers and docs.

## Two tiers

- **Base layer** (`netd.py`, `election.py`, `witness.py`,
  `cluster_arbiter.py`): mesh + weighted-vote election + witness +
  the `.254` arbiter. No rqlite dependency — this is the layer that
  *recovers* rqlite.
- **rqlite** holds cluster-wide policy (membership, witnesses, VMs,
  tiers, operators, UUID history). Every node runs a local rqlited
  replica, read via `cluster_state.load_cluster()` at level `none`, so
  policy is readable without quorum. This matters most for per-VM
  `vm_type` (cattle / pet / vipet): an isolated node must keep a cattle
  VM running and pause a pet VM on `NOQUORUM`, and that decision needs
  the `vms` table locally. At N>=3 the two-tier split also gives rqlite
  continuity through master failover (3 of 5 voters stay quorate
  without the new arbiter).

## Weighted vote model (`election.py`)

- `VOTES_PER_NODE = 100`, `VOTE_PER_WITNESS = 1`.
- `total_votes = 100·N_active + N_configured_witnesses`;
  `majority = total_votes // 2 + 1`. ALL configured witnesses count in
  the denominator; only valid+confirmed ones add to `my_votes`. A
  configured-but-invalid witness raises the bar and biases toward "do
  not fail over" (safety over availability).
- The 100/1 spread lets a witness break an exact node-vs-node tie but
  never substitute for a peer: one node alone can never cross the bar on
  witness votes.
- Active-node set = rqlite `nodes` with `state == 'active'` and not in
  maintenance (netd filters before calling `compute`), plus self. The
  denominator is this active count, NOT the heard-from set — so a master
  that *restarts* while partitioned reads N from rqlite, stands alone
  below majority, and falls to `NOQUORUM` instead of faking a one-node
  cluster.

### Votes are active acks, not passive reachability

In steady state (a live master), `compute()` uses reachability and acks
are ignored. Once the master is gone, a candidate's node-vote tally is
`100·(self + acking peers)`. A peer grants its 100 votes only when it
has *also* lost the master AND finds the candidate eligible. Eligibility
= the candidate's advertised arbiter-DRBD UUID classified against the
voter's own 7-day local UUID history (`state.classify_arbiter_uuid`):
`superseded` => refuse, `current`/`unseen` => votable. Each peer makes
that decision in netd and ships it as its `ack_target`; `election.py`
just tallies the resulting `peer_acks`.

When a quorum of acks is reached, a deterministic tiebreak keeps two
candidates from both promoting: the lowest-loopback-octet contender
proposes; everyone else defers a tick and acks it.

## Timing (`netd.py`)

- Election tick: `ELECTION_INTERVAL_S = 1.0` s. The vote recomputes from
  current inputs every tick; result either matches actuator state (no-op)
  or drives a transition.
- A survivor promotes at `MASTER_LOSS_MISSES = 10` consecutive ticks with
  no fresh heartbeat from the believed master (~10 s).
- An isolated old master self-demotes to `NOQUORUM` at
  `SELF_DEMOTE_MISSES = 9` (~9 s) — one tick *earlier*, so `.254` /
  arbiter rqlite is released before any survivor promotes and the VIP is
  never on two nodes at once (INV-1 margin).
- Cold-boot patience `COLD_BOOT_PATIENCE_S = 30.0` s
  (`cluster_arbiter.py`): at N>=2, a node that boots believing it should
  be master defers its *first* promote for 30 s, giving a slower peer
  time to come up and beat it cleanly. Tracked from process start
  (`_COLD_BOOT_AT`). N=1 skips it. Worst case if the peer was truly dead:
  a short DRBD resync on its eventual return.

## Witness — BedRock Echo (`witness.py`)

A passive per-node K/V slot store on UDP 12321, ChaCha20-Poly1305 AEAD
over msgpack. The witness has no logic: it stores the last write per slot
and returns all slots on every reply. One slot per node (key = node_id =
loopback last octet, 1-250). Kept tiny on purpose so an ESP32 can run it
in a few hundred lines.

- `WITNESS_FRESHNESS_S = 12.0` — reply-freshness for the "witness
  reachable" vote.
- `SLOT_STALE_MS = 10_000` — a survivor treats the master's slot as gone
  after >10 s, matching the 10-miss leader-loss detector.
- Slot tag bitflag: **bit 0 = `TAG_LMS` (last-man-standing); bits 1-7
  reserved.** The marker carries the arbiter resource's DRBD current-UUID
  (`MARKER_KIND_DRBD_ARBITER_UUID = 1`).
- **Witness validity**: a witness counts toward the tally only when its
  reply holds a slot for *every* active node in the rqlite `nodes` table
  (`ws.member_ids`, plumbed each netd tick). Older entries are fine; a
  missing member's slot disqualifies the witness. `member_ids = None`
  (membership not yet known) => not valid => 0 votes.
- **Witness confirmed**: our own slot is present at the witness, carries
  our current marker, and is fresh — the readback proof.
- `count_valid_confirmed()` tallies CONFIGURED witnesses that are
  individually valid+confirmed, capped at `n_configured` (a rogue extra
  Echo can't inflate the vote). Single-witness testbed yields 0 or 1.
- LMS denominator/quorum rule: witness quorum = majority of *all
  configured* witnesses, each contributing only when valid. The witness
  layer is the arbiter of LMS uniqueness — each node owns its own slot,
  so at most one node holds an acked `tag.lms = 1`.

## Election tick flow (`_election_tick`)

```
TICK START (1 Hz)
  │
  ├─ witness IO: reprobe/heartbeat + drain replies (best-effort)
  ├─ peer liveness from netd neighbour table (+ ever_seen_peers floor)
  ├─ rqlite snapshot (level='none'): active nodes, mgmt_master,
  │     witnesses → node_loopbacks, member_ids, n_configured_witnesses
  ├─ read own arbiter DRBD current-UUID (debugfs data_gen_id),
  │     record into local 7-day history
  ├─ count valid+confirmed witnesses
  ├─ missed-master-beat detector → master_lost at MASTER_LOSS_MISSES
  ├─ build peer_acks (a peer's ack_target == me, fresh)
  │
  ├─ compute()  ─────────────────────────────► LEADER / FOLLOWER / NOQUORUM
  │     no-quorum marker present?  → NOQUORUM (sticky)
  │     I am master?               → reachability quorum check
  │     live master, not me?       → FOLLOWER
  │     master gone?               → self + acking peers + witnesses
  │                                   ≥ majority, lowest octet → LEADER
  │
  ├─ publish heartbeat fields: believed_master, transitioning,
  │     ack_target, arbiter_uuid
  ├─ persist believed_master to state.json (cold-boot recovery)
  │
  └─ act on outcome:
       NOQUORUM  → after SELF_DEMOTE_MISSES streak: drop no-quorum
                   marker; if hosting, demote_arbiter_host()
                   (release .254, stop arbiter rqlite, drbdadm secondary)
       LEADER    → cluster_arbiter.promote_to_arbiter_host()
                   + ensure_lms_if_last_standing()
       FOLLOWER  → (nothing; follow current master)
```

### Heartbeat fields (mesh, protocol 4)

Each node broadcasts a signed (HMAC-SHA256) election heartbeat carrying:
- `believed_master` — who this node follows now (`""` mid-failover).
- `transitioning` — this node has lost the master AND is advertising
  itself as master-to-be (it is the lowest-octet eligible contender).
- `ack_target` — the contender this node votes for (self if it is the
  lowest-octet eligible contender, else the one it defers to). Computed
  independently of `compute()`'s quorum gate, so the vote can bootstrap:
  peers ack the prospective winner before it reaches quorum.
- `arbiter_uuid` — this node's live arbiter DRBD current-UUID, the
  eligibility evidence peers classify against their own history.

Acknowledgment is implicit: a peer "acks" by naming this node as its
`ack_target` on its next heartbeat. No explicit signed "ACK X" messages.
Heartbeats and witness slot writes both ride the 1 Hz tick.

## Two-tier writeback ordering (who writes `mgmt_master`)

The base layer DRIVES the promote; `cluster_info.mgmt_master` in rqlite
is written by the arbiter as a *result*, only after the arbiter rqlite is
back. The promote needs no rqlite (witness + local commands only), so
there is no deadlock: on a `LEADER` outcome netd calls
`promote_to_arbiter_host()`, which runs the takeover protocol, brings up
DRBD primary + `.254` + arbiter rqlite + filer, then writes `mgmt_master`
once `arbiter_status()` confirms hosting. netd never writes `mgmt_master`
itself. The realtime election is the authority; rqlite follows.

## Arbiter takeover protocol (`cluster_arbiter.py`)

Witness slot inspection + DRBD UUID match + own-slot write/readback,
gated by the election's `LEADER` outcome. All local commands
(`drbdadm`, `ip`, `mount`, `systemctl`) — rqlite is the service being
recovered, never on this path. Outline:

1. **Defer-to-claimer**: if a peer's fresh heartbeat advertises *itself*
   as master, defer — never steal the role back from a live survivor.
2. **Fast path**: no prior master, or self is the recorded master —
   nothing to take over from. Subject to the cold-boot UUID guard and
   (at N>=2) the 30 s patience window.
3. **Cold-boot UUID guard** (`_cold_boot_uuid_ok`): if the witness holds
   our own slot from a previous life with a marker we no longer have
   locally, the cluster advanced without us — refuse to promote a stale
   copy. rqlite-free; applies even with no other master.
4. **Witness reachability**: at N<=2 a witness reply within 5 s is
   mandatory (rqlite quorum depends on the arbiter we are about to
   promote). At N>=3 the cluster has natural rqlite quorum and the
   isolated old master self-demotes via `NOQUORUM`, so takeover proceeds
   cautiously without a witness.
5. **Slot inspection** of the last-known master's slot:
   - missing slot => REFUSE (INV-7: a missing slot is worst-case, could
     have held `lms = 1`; operator decommissions the node or re-keys the
     witness).
   - fresh slot => REFUSE (cluster healthy elsewhere).
   - stale + `lms = 1` => REFUSE (previous master died without clearing
     LMS; LMS never times out — operator clears via override).
   - stale + `lms = 0` => continue.
6. **DRBD UUID match**: the local `cluster` resource's current-UUID
   (`_read_local_drbd_uuid()`, via debugfs `data_gen_id` with a
   `drbdadm dump-md` fallback — `drbdadm current-uuid` does not exist in
   DRBD 9.x) must equal the slot marker exactly; mismatch => REFUSE
   (divergence, operator reconciles).
7. **Go solo**: set own slot `tag = lms`. The arbiter OWNS the LMS bit —
   netd refreshes only the marker each tick and never the tag, so the
   step-5 readback can't be raced back to 0.
8. **Actuate + readback**, then DRBD primary, mount, bind `.254`, start
   arbiter rqlite + filer, write `mgmt_master`.

## LMS (last-man-standing) lifecycle

The witness slot `tag.lms` bit asserts "I am hosting the cluster alone
because the peer is gone." It is an explicit local decision owned solely
by `cluster_arbiter`: set on go-solo / `ensure_lms_if_last_standing`,
cleared on self-demote (`demote_arbiter_host`). netd's per-tick recompute
never flips it (doing so raced the takeover readback).

Clear semantics are strict:

- An `lms = 1` slot is cleared ONLY by a successful write from the slot
  owner to the witness — both online at the same moment. `set_own_slot`
  updates the in-memory tag; the wire write is the next `heartbeat_all`,
  whose `sendto` errors are swallowed (`OSError`), so a failed write is
  invisible. The demote-path clear is best-effort.
- If the owner dies with `lms = 1` outstanding, the slot stays `lms = 1`
  until the owner returns and writes `lms = 0`, or the operator
  decommissions it (remove from rqlite `nodes` so readers ignore its
  slot), re-keys the witness identity, or clears manually.
- A witness that loses state and returns EMPTY is NOT a clear: per INV-7
  a missing slot is worst-case (`lms = 1`-possible) until repopulated by
  a fresh heartbeat from the current owner; takeover stays refused.
- The 10 s slot-staleness rule is NOT a clear either: it only changes how
  a reader interprets a stale `lms = 1` — takeover then needs exact
  UUID/history-chain match (stricter than treating the bit as cleared).

A stuck LMS can leave a visible inconsistency (slot says `lms = 1`, mesh
heartbeats say `lms = 0`). Resolution funnels through the staleness +
UUID-lineage path; operators alert on a stuck LMS and clear via override.

## DRBD UUID provenance — the split-brain guard

The current master's local DRBD state is the single authority. Three
mirrors, increasing staleness tolerance:

1. **Master heartbeat** — broadcasts its current arbiter UUID on every
   mesh heartbeat + witness slot write. Newest, authoritative-now,
   ephemeral.
2. **rqlite UUID-history table** — `(uuid, ts_set, ts_superseded)`, 7-day
   retention, replicated to every node. Lags the heartbeat (rqlite may
   have been down during failover; the write lands after quorum reforms).
3. **Per-node local history** (`state.arbiter_uuid_history`) — each node
   records every arbiter UUID it observes, newest last, capped at 7 days
   (`UUID_HISTORY_RETENTION_S`). Autonomous: needs no rqlite, no peer.
   The cold-boot fallback.

Invariants: the heartbeat may be ahead of rqlite, never behind; all three
agree at steady state.

Eligibility (`state.classify_arbiter_uuid`): a candidate's advertised
UUID is `current` (matches our newest), `unseen` (never recorded —
assume newer, votable), or `superseded` (seen but a later UUID replaced
it — refuse). The refuse case is the split-brain guard: a stale candidate
can never win a vote, even on raw node count. A node refuses to claim
master at all if any known peer holds a newer arbiter generation, even an
unreachable one — promoting would silently lose those writes, including
the SeaweedFS/S3 metadata on the `cluster` singleton. When the
up-to-date peer is permanently lost, the operator can `seize` (force
promote, data loss acknowledged); without it the cluster refuses to come
up, which is the safe default.

## `state.json` — per-node election persistence (`state.py`)

The local `/etc/bedrock/state.json` carries this node's identity plus the
two base-layer facts that must survive reboot with no rqlite (they are
what recovers rqlite):

- `believed_master` — who this node last believed was mgmt master, read
  on cold boot before rqlite quorum exists. Written by the election when
  the believed master changes; atomic tmp+rename+fsync (`state.save`).
- `arbiter_uuid_history` — the local 7-day UUID log above; appended each
  tick via `record_arbiter_uuid`.

The election is the sole writer of these fields. Sagas (join, takeover,
shutdown) influence them only indirectly, by changing the inputs the
election reads (mesh state, rqlite membership, witness slot). On a lost
or 0-byte `state.json`, `recover_identity_from_cluster_json` rebuilds
identity from the local `cluster.json` + hostname;
`arbiter_uuid_history` restarts empty (a candidate then classifies as
`unseen` / votable — acceptable, because the hard promotion gate is the
live local DRBD current-UUID exact match (debugfs `data_gen_id`), not
state.json).

## Maintenance mode

A node in maintenance contributes 0 votes — dropped from both numerator
and denominator. netd's active-node filter excludes
`info.get("maintenance")` nodes from `node_loopbacks` and `member_ids`,
so peers recompute the cluster's vote total without it. Putting the
master into maintenance triggers a planned handoff (same failover
mechanism, operator-initiated), letting a single node shut down cleanly
while the cluster continues on the recomputed quorum.

## Graceful shutdown

- **Single-node shutdown**: enter maintenance on the target first (votes
  drop out, master hands off), then stop services. The cluster continues
  on remaining voters.
- **Cluster-wide shutdown**: every node stops with its votes intact — no
  maintenance bit, no handoff. On the next cold boot the 30 s patience
  window + eligibility rules apply: nodes wait for peers and resume only
  once enough return to form quorum. No "was shut down cleanly" marker is
  needed — refuse-to-promote-alone is the safe default if a peer doesn't
  return.

A graceful daemon stop hands off all cluster work (DRBD demote, release
`.254`) before exiting, so a clean restart re-enters from a quiet
position and the 30 s window starts fresh.

## Join

Operator-initiated, runs on the master, triggered by accepting a pending
join request. It executes via the rqlite saga backend (operations /
operation_steps): rqlite `nodes` insert, key distribution, peer DRBD
config, rqlited join. The joiner polls the master's HTTPS API for the
result. A mid-join (`state == 'joining'`) node is excluded from the
election denominator so the master can't be tipped into `NOQUORUM` while
a peer joins.

## Glossary

- **LMS** — last-man-standing. A node's witness-slot `tag` bit 0
  asserting it hosts the cluster alone; set only after the go-solo
  takeover step.
- **Witness quorum** — majority of all configured witnesses, each
  contributing only when valid.
- **Valid witness** — its last reply holds a slot for every active node
  in the rqlite `nodes` table.
- **Patience window** — the 30 s after process start at N>=2 during which
  a node defers its first arbiter promote.
