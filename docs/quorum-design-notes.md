# Quorum design — working notes

Scratch design doc kept while we lock in the master-election state
machine, one question at a time. Locked decisions move into the
canonical specs (`cluster-quorum-spec.md`, `cluster_arbiter.md`,
`election.md`); this file is the queue.

## Working conventions
- One question at a time. Don't pile up multi-part asks.
- Spell out state names in full (`LEADER`, `FOLLOWER`, `NOQUORUM`,
  `FENCED`). Same in code identifiers and docs. Single-letter shorts
  save no space that matters and obscure meaning.
- Background lists below stay persistent. Items only leave when
  resolved or explicitly retired.

## Decisions locked in
- Cold-boot patience window = 30 s before any witness-assisted
  decision can fire. Worst case if peer was actually dead = a short
  DRBD resync on its eventual return.
- Witness quorum = majority of **all configured** witnesses
  (1-of-1, 2-of-3, 3-of-5, 2-of-2). Not "majority of valid ones".
- A witness counts toward the quorum only if it currently has slot
  entries for **every node** named in `cluster.json`. Older entries
  are OK; missing entries disqualify the witness. Heartbeats keep
  flowing regardless.
- LMS write is "set" only after acknowledgment from a witness quorum.
- LMS state is persisted in two places: on each contributing witness
  slot AND on the node that set it (local file).
- A new local file (working name `/etc/bedrock/state.json`)
  holds the realtime master-election view, distinct from rqlite's
  `cluster_info.mgmt_master` and from `cluster.json`.
- **One local-state file per node, in memory, written on change.**
  Folds into the existing `/etc/bedrock/state.json` (already
  per-node identity + role; election state goes here too — no new
  file). The DRBD UUID history list lives in the same file as a
  field (per-node observation log, capped at 7 days; same scope).
  In-memory representation is the authority; the file is a
  dump-on-change snapshot, atomic tmp+rename (the existing save
  helper at `installer/lib/state.py:14` already does this).
- **Cluster-wide state stays in rqlite.** Membership, witnesses,
  VMs, tiers, operators, the UUID-history table — all rqlite.
  Locally readable from every node via the per-node rqlited
  replica (the autonomous-isolated-node argument).
- **LMS is +1 vote, not a separate concept.** Simplified vote
  model:
  - Voting pool = nodes in `cluster.json` minus those in
    maintenance mode.
  - Threshold = strict majority of voting pool.
  - Each pool member is 1 vote when its mesh heartbeat shows
    `current_master = candidate`.
  - A node that has successfully written `tag.lms = 1` to the
    witness quorum AND had the write acked gets +1 vote on top
    of its own self-vote.
  - The witness layer is therefore not a "voter" in its own
    right — it is the *arbiter of LMS uniqueness*. Only one
    node at a time can hold an acked `tag.lms = 1` across the
    witness quorum, because the multi-phase advertisement +
    eligibility-check rules prevent two simultaneous holders
    from both reaching the cluster majority.
  - Removes the awkward 10/1 weighting from the older spec; the
    quorum outcomes for every cluster size are identical.
- Two-node baseline locked in first. Operator overrides (maintenance,
  forced promote, scheduled handoff) layered on after the baseline
  is correct.
- **rqlite stays as Tier 1. Two-tier architecture confirmed.**
  Reason: every node needs autonomous read access to cluster policy
  data — most concretely, per-VM `vm_type` (cattle / pet / vipet)
  determines correct local behavior during isolation. A cattle VM
  must keep running on an isolated node; a pet VM must get fenced.
  That policy lives in rqlite's `vms` table; the per-node rqlited
  replica is what makes the isolated node act correctly without a
  live cluster connection. Removing rqlite would just re-implement
  this replication via a different mechanism, not eliminate it.
  At N≥3 the two-tier also gives free rqlite continuity through
  master failover (3 of 5 voters quorate without the new arbiter).
  The 2-node-HA awkwardness is the cost; the master-election
  design fixes the awkwardness, doesn't dodge it.
- **Single unified promote-decision function.** Same logic decides
  "may this node become master right now" at cold-boot, at failover,
  on recovery from NOQUORUM, or any other not-master → master
  transition. There is no separate cold-boot path.
- **Authorization rule** (you may promote, ANY of these is enough):
  - `state.json.last_man_standing == true` for this host, OR
  - vote count ≥ quorum threshold, where votes = 1 per reachable
    non-maintenance node (self always counts as reachable to itself)
    + witness-quorum-as-one-vote when witness-quorum is valid.
    Threshold = strict majority; ties broken by the witness vote.
- **Veto rule — required**: a signed payload (from any witness,
  including witnesses that fail quorum-validity, or any peer mesh
  assertion) that reports a node other than self with
  `last_man_standing = true` blocks promotion. Load-bearing.
- **DRBD UUID + history chain is the tie-break for promotion when
  the LMS flag does not decide.** Each node knows its own current
  UUID and its own history chain (debugfs:
  `/sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id`,
  line 1 = current, then per-peer bitmap UUIDs, then history
  generations). Each node also knows the peer's current UUID
  (last published in the peer's witness-slot marker, or read live
  from DRBD once the connection is up).
  - **Equal current UUIDs** → no divergence; both nodes are equally
    valid as the next master. A tie-break beyond DRBD is needed —
    expected to be the local `state.json` view, but the
    exact fallback rule is open (question 3 below).
  - **Different current UUIDs** → exactly one will be in the
    other's history chain. The node whose current UUID is *not* in
    the peer's history is the newer one — it holds writes the
    peer doesn't — and is the one that promotes. The peer becomes
    Follower and DRBD resyncs it.
  - **Neither side's current UUID is in the other's history** →
    real divergence (split-brain that wasn't caught). Refuse on
    both sides. Operator reconciles.
- **Asymmetry**: authorization needs a witness *quorum*; the
  required veto needs only one signed payload. Easier to reject
  than to authorize.
- **Single-node operation (N=1)**: `last_man_standing` is by
  definition always set; no patience wait; just be the master.
- **30 s patience wait** applies only when `cluster.json.nodes`
  contains more than one node. During the wait the promote-decision
  function still runs, but the "not enough information to authorize"
  outcome stays Pending rather than NoQuorum until 30 s elapse.
- A node in **maintenance mode** contributes 0 votes (does not count
  in either the numerator or the denominator of the quorum math).
- **Maintenance mode is the mechanism that enables clean single-node
  shutdown.** Putting node X into maintenance:
  1. Sets `tag.maintenance = 1` on X's witness slot, state.json,
     and mesh heartbeat.
  2. Peers' election functions observe + recompute the cluster's vote
     count *without* X (numerator and denominator both shrink by X's
     weight).
  3. If X was master, the maintenance bit is incompatible with being
     master — triggers a planned handoff to a peer (same failover
     mechanism, just operator-initiated).
  4. Once the cluster has acknowledged X is in maintenance (peers
     have updated their views), X can shut down cleanly. The cluster
     continues with the recomputed quorum.
- **Two distinct graceful-shutdown flavors:**
  - **Single-node shutdown.** Enter maintenance on the target node
    first (steps above). Then stop services. Cluster continues
    running on remaining voters.
  - **Cluster-wide shutdown.** Every node shuts down with its votes
    intact — no maintenance bit, no handoff. Each node stops
    services in safe order. On the next cold-boot, the existing
    30 s patience window + eligibility rules apply: nodes wait
    for peers, and only resume work once enough come back to
    form quorum. No special "cluster was shut down cleanly"
    marker is needed — the existing rules handle it correctly
    (refuse-to-promote-alone is the safe default if a peer
    doesn't return).
- **Join saga is operator-initiated and runs on the master.** Triggered
  by the operator pressing "Accept" on a pending join request in the
  dashboard. The saga executes via the existing rqlite saga backend
  (operations / operation_steps tables), runs through the
  details — cluster.json update, key distribution, peer DRBD config,
  rqlited join, etc. The joiner side polls the master's HTTPS API
  for the result. Existing flow; nothing master-election-specific.
- **`state.json` writer:**
  - Bedrock-net's election logic is the **sole writer**. Sagas
    (join, takeover, graceful shutdown) influence the file
    *indirectly* by changing the inputs the election reads —
    mesh state via actuators, `cluster.json` via rqlite, witness
    slot via heartbeat. They never write `state.json`
    themselves.
  - This keeps the file a single deterministic function of
    observable cluster state. No two writers, no races.
  - Writes use atomic tmp+rename. Election skips the write when
    the computed content equals the on-disk content.
- **`state.json` fields (all kept in memory; file is a
  dump-on-change snapshot, never updated by syscall-on-read):**
  - `current_master` — node name; realtime cluster-master view
    from this node.
  - `last_man_standing` — bool, this node's own LMS status.
  - `maintenance_mode` — bool, this node's maintenance state
    (mirrors witness slot tag bit 2).
  - `transitioning` — bool, mirror of witness slot tag bit 1.
    Lets a daemon-restart see "I was mid-saga last time".
  - `transition_id` — saga reference (for VictoriaLogs cross-ref),
    set whenever `transitioning = true`.
  - `last_role_change_ms` — local timestamp of last transition.
    Operator-friendly; not used in decision logic.
  - `last_drbd_uuid_observed` — DRBD `current-uuid` recorded the
    last time the election successfully completed a tick. Forensic.
  - `cluster_uuid` — sanity check that this file belongs to this
    cluster (catches "node moved between clusters without wipe").
  - `self_node_name` — sanity check this file is for the node it
    claims (catches accidental file-copy operator errors).
  - **Rule**: every field must be readily available in memory
    when the election tick runs. No command calls or external
    lookups to populate the file. The election maintains an
    in-memory state struct that is updated event-driven (mesh
    callback, witness reply, DRBD observation, operator
    command); the file is a dump of that struct, written only
    when the in-memory content changes.
- **Promote-decision cadence:**
  - Runs every 1 Hz tick, same loop as the witness heartbeat.
    Recomputes from current inputs each second; result either
    matches existing actuator state (no-op) or triggers a
    transition.
  - Event-driven wake-ups for the promote-decision logic itself
    are nice-to-have optimizations, low priority — the next
    1 Hz tick will catch the change anyway.
- **Heartbeat cadence is 1 Hz baseline PLUS immediate
  out-of-band heartbeats on local state change.** Whenever this
  node's `state.json` changes value (LMS flips,
  transitioning bit flips, `current_master` view changes, etc.),
  the next heartbeat goes out *immediately* — both via mesh to
  peers and via slot-write to witnesses — without waiting for
  the next 1 Hz tick. This is what makes the multi-phase consensus
  converge in sub-second time instead of multiple ticks.
- **Peer acknowledgment is implicit and computed independently.**
  When peer Y receives an incoming heartbeat from X carrying
  `current_master = X, transitioning = true`, Y does NOT
  blindly relay. Y runs its own promote-decision logic against
  current inputs (X's claim being one of them) and emits a
  heartbeat with Y's own conclusion. When Y's conclusion is
  `current_master = X`, that heartbeat IS the acknowledgment X
  was waiting for. When Y's conclusion differs, Y broadcasts its
  own view and X sees no ack from Y, no quorum on Y's side.
- **Patience window lifecycle (30 s):**
  - Clock starts when local disk state has been read at boot —
    `state.json`, `cluster.json`, witness list. The read is
    fast, so the clock effectively starts a few ms after daemon
    start.
  - **No resets** during a node's life. A graceful daemon stop
    should hand off all cluster work first (DRBD demote, release
    `.254`, etc.) before exiting, so a clean restart re-enters the
    BOOT state from a known-quiet position and the 30 s window
    starts again naturally. Daemon restart without graceful
    handoff is not a normal operation.
- **Witness slot tag bitflag — locked assignments:**
  - bit 0 = `last_man_standing` (LMS)
  - bit 1 = `transitioning` (mid-saga; this node is doing something
    non-instantaneous — see VictoriaLogs for the saga details)
  - bit 2 = `maintenance_mode` (node has 0 votes; the cluster's
    quorum math excludes it from both numerator and denominator)
  - bits 3–7 = reserved
- **Phase-1 / phase-3 advertisement is both A and B together:**
  - **Marker sentinel** — phase 1 writes `slot.marker =
    0x0000000000000000`. Trips any "are UUIDs equal" comparison
    trivially without anyone needing to interpret it specially.
    Phase 3 restores the real new UUID.
  - **Tag bit** — phase 1 sets `tag.transitioning = 1`. Phase 3
    clears it. The flag is a generic "something non-instantaneous
    is in progress" signal; the witness payload deliberately stays
    minimal (witnesses are sometimes ESP32, not interrogable for
    rich state). All saga details live in VictoriaLogs.
  - **VictoriaLogs forensic line at each phase**, including:
    `"writing all-zeros UUID + transitioning=1 to witness slot N,
     from current UUID <X>"` at phase 1; and
    `"writing new UUID <Y> + transitioning=0 to witness slot N"`
    at phase 3. DRBD's own history-uuid chain on disk is the
    secondary forensic record.
- **Cluster master-claim consensus spans BOTH peers AND witnesses,
  not witnesses only.** Other cluster nodes are first-class
  participants in the master-election quorum and report their own
  observed view of who the master is.
  - Quorum vote-weights stay as the existing formula: 10 votes per
    reachable non-maintenance cluster node + 1 vote per witness in
    the witness-quorum. Threshold = strict majority of the total.
  - A peer "acknowledges" a master claim by updating its OWN
    `state.json` with the new `current_master` value
    (carrying the transitioning bit while the claim is mid-saga)
    and broadcasting that view on its next mesh heartbeat. There
    are no explicit signed "ACK X" messages — observation + relay
    via the existing heartbeat is the ack.
  - The claiming node waits until ENOUGH peers' heartbeats AND
    witness slot replies reflect itself as master (with
    transitioning bit) to clear the weighted-vote threshold.
  - In 3-node: this node alone (10) is below threshold (16 of 31).
    Needs at least one peer ack (10) to clear. Witness alone is
    not sufficient.
  - In 2-node: this node alone (10) is below threshold (11 of 21).
    Needs either peer ack (10) or witness-quorum ack (1) to clear.
- **Failover is multi-phase: advertise intent, gather acks,
  actuate, advertise end state.**
  - **Phase 1 — local commit.** Compute new state. Write own
    `state.json` first with `current_master = self`,
    `transitioning = true`, `last_man_standing = <new value>`.
    In-memory state matches.
  - **Phase 2 — broadcast intent.** Next mesh heartbeat carries
    the new view to peers. Next witness slot write: `marker =
    0x00…0`, `tag.transitioning = 1`, `tag.lms = <new value>`.
  - **Phase 3 — gather acks.** Wait until peers' mesh heartbeats
    AND witness slot replies confirm they see this node as the
    new (transitioning) master. The weighted-vote sum of acking
    participants must reach the cluster's majority threshold.
    Timeout → abort, revert local file + slot to previous state.
  - **Phase 4 — actuate.** `drbdadm primary` (new UUID), mount,
    bind `.254`, start arbiter rqlite, start filer.
  - **Phase 5 — end-state broadcast.** Update own
    `state.json` (`transitioning = false`,
    `last_drbd_uuid_observed = <new>`,
    `last_role_change_ms = now`). Next mesh heartbeat carries
    final view. Next witness slot write: `marker = <new real
    UUID>`, `tag.transitioning = 0`, `tag.lms = <as set>`.
  - **Phase 6 — peer convergence.** Peers observe the final state
    and update their own `state.json` (clear transitioning
    bit, record new master+UUID). No explicit handshake; the next
    heartbeat tick on each peer carries the update.
  - Crash anywhere is recoverable: phase 1 commits locally before
    any cluster effect, and phases 2-6 leave the transitioning
    bit visible to the cluster until phase 5 clears it.
    Recovery on reboot inspects `state.json.transitioning`
    + live DRBD state + last observed slot to decide whether to
    re-run from phase 4 (DRBD already primary) or abandon (DRBD
    not yet primary).
- **Candidate eligibility — strict.** A node refuses to claim
  master entirely if *any* known cluster peer has a more
  up-to-date DRBD `tier-critical` generation, regardless of
  whether that peer is currently reachable. Reason: if the peer's
  data is ahead, promoting this node would silently lose those
  writes — including S3/SeaweedFS metadata on the tier-critical
  volume. The peer might be temporarily unreachable, not
  permanently dead.
- **"rqlite came back online" is an event, not a saga.** A small,
  reusable event source that fires once when `_rqlite_ready`
  transitions `False → True` on this node. The probe already
  exists in `installer/lib/netd.py:1027–1064` — a per-tick
  strong-consistency `SELECT 1` against the local rqlited. Turn
  the per-tick boolean into an edge-triggered event so other
  components can subscribe. Triggers from any cause that makes
  rqlite writable from this node: arbiter rqlited just joined
  quorum after a new master started it, quorum recovery after a
  mass-partition heals, the local rqlited recovering from a brief
  outage, etc. Subscribers don't care why — only that "now I can
  write".
- **The master maintains a UUID-history backlog.** While rqlite
  is not writable from the master (between the new master
  becoming DRBD Primary and the arbiter rqlited joining quorum),
  any DRBD UUID transitions that need to be logged into the
  history table are buffered. Source of the buffer is the local
  DRBD UUID history file (always written first; rqlite is a
  follower of the local record). On `rqlite_came_back_online`,
  the master replays unposted entries from the local history
  file into the rqlite history table. Replay is idempotent
  (uuid + ts_set as primary key).
- **Mass-isolation recovery.** When every node has lost quorum
  (e.g., 4–5 nodes simultaneously partitioned from one another,
  rare), the cluster comes back via the same mechanism: whichever
  node first regains a quorum-forming partition + holds the
  last-known-master role flushes the backlog from its local
  history file into rqlite. Other nodes' history files converge
  once rqlite replicates the writes. No special "mass-isolation
  recovery saga" is needed if the per-master backlog + replay-on-
  rqlite-came-back works correctly.
- **Authoritative model for DRBD UUID provenance.** The current
  master's local DRBD state is the single authority. Three
  mirrors, in increasing order of staleness tolerance:
  1. **Master's heartbeat** — current master broadcasts its
     current DRBD UUID on every mesh heartbeat + witness slot
     write. Newest signal; authoritative for "what is true right
     now"; ephemeral.
  2. **rqlite UUID history table** — master writes `(uuid,
     ts_set, ts_superseded)` into rqlite. Keeps 7 days. Replicated
     to every node. Lags the heartbeat (rqlite may have been
     down during failover; the write lands after rqlite quorum
     re-forms on the new master).
  3. **Per-node local DRBD UUID history file** — each node
     records every UUID it observed in a master heartbeat, with
     the observation timestamp. Autonomous: needs no rqlite, no
     peer. This is the cold-boot fallback before rqlite is up
     and before any peer is reachable.
  Invariants: heartbeat may be ahead of rqlite (rqlite hasn't
  caught up). Heartbeat is never behind rqlite. All three
  agree at steady state. The eligibility check uses whichever
  mirrors are available and compares this node's current local
  DRBD UUID against the most-recent UUID found across them.
- **Operator seize override.** When a peer with more-recent data
  is permanently lost (irrecoverable hardware, building burned
  down, etc.), the operator can explicitly force promotion with
  the stale-data warning acknowledged. Conceptual parallel to
  Active Directory's FSMO seize on a lagging domain controller:
  the operator asserts "the up-to-date node is never coming
  back, proceed with what I have, accept the data loss." Without
  this command, the cluster refuses to come up — which is the
  safe default. CLI shape TBD (separate operator-abuse layer
  design).
- **Tie-break order when LMS does not decide**, in this exact
  priority:
  1. **DRBD current-UUID comparison.** If UUIDs differ, the history
     chain says who's newer (per the locked rule above) — that node
     promotes, no further check needed.
  2. **`state.json` agreement.** Used only when DRBD UUIDs
     are equal. The node whose file says `current_master = self`
     promotes; the other follows.
  3. **Files disagree while UUIDs match** is considered structurally
     impossible: a role transition implies `drbdadm primary` on at
     least one side, which bumps the UUID. If this state is
     observed in practice, treat as inconsistent: both nodes refuse
     and surface to operator. (A failed file-write mid-saga falls
     here only if the other side already did `drbdadm primary` —
     but then UUIDs differ, and rule 1 fires before this case.)

## Open questions — queue, top-down

1. (resolved 2026-05-22 — see locked decisions)
2. (retired 2026-05-22 — DRBD UUIDs don't carry useful timestamps;
   the history-chain mechanism is the actual tie-break, see locked
   decisions)
3. (resolved 2026-05-22 — clock starts after local disk state is
   read, no resets, see locked decisions)
3a. (resolved 2026-05-22 — both: sentinel marker + tag bit 1, see
    locked decisions)
4. (resolved 2026-05-25 — maintenance mode folds into the
   single-node graceful-shutdown flow; see locked decisions)
5. (resolved 2026-05-22 — 1 Hz tick primary, events as low-priority
   nice-to-have, see locked decisions)
6. (resolved 2026-05-22 — election is sole writer; sagas influence
   inputs only, see locked decisions)
7. Stale-but-present witness slot: counted as fresh-enough for the
   slot's data to be used as a veto, or only fresh slots veto?
8. (resolved 2026-05-25 with correction — LMS clear requires
   slot-owner AND witness both online; no self-correction. See
   "LMS clear semantics — verified against code" section.)
9. cluster.json / state.json / state.json — authority order
   and who writes which on each transition.
10. Witness reconfiguration mid-flight: how the validity rule
    behaves when an operator adds or removes a witness.
11. (resolved 2026-05-25 — two flavors: single-node via maintenance,
    cluster-wide via plain stop; see locked decisions)
12. Operator seize command shape: CLI invocation, what it writes,
    safety prompts, audit trail. (Layered on after baseline.)

## Bonus safety nets (operator-abuse resilience, not load-bearing)
- (UUID-history check moved up to a locked decision — it is
  load-bearing for the tie-break, not bonus. This section is
  reserved for future genuine extras.)

## LMS clear semantics — verified against code (2026-05-25)

The witness slot tag bit 0 (`last_man_standing`) cannot be assumed
to clear "automatically" or "best-effort with self-correction".
Verified facts from the source:

- `installer/lib/witness.py:317-328` `set_own_slot` only updates
  the in-memory `own_tag` field. The wire write happens in the
  next `heartbeat_all` (line 246).
- `installer/lib/witness.py:255` silently swallows `OSError` from
  `sendto`. A failed wire write is invisible to the caller.
- `installer/lib/netd.py:1122` recomputes `lms_bit` from current
  observables every tick — but the new value reaches the witness
  only if the UDP packet lands.
- `installer/lib/cluster_arbiter.py:602-618` demote path calls
  `set_own_slot(tag=0)` and explicitly labels it best-effort.

The hard rule:

- An `lms = 1` slot is cleared ONLY by a successful write from the
  slot owner to the witness, requiring both online at the same
  moment.
- If the slot owner dies with `lms = 1` outstanding, the slot stays
  `lms = 1` until either the slot owner comes back and successfully
  writes `lms = 0`, or the witness loses state (e.g. ESP32 reboot),
  or operator manually clears.
- The 15 s slot staleness rule is **not** a clear. It changes how
  readers interpret a stale `lms = 1` — peer takeover via UUID
  equality / history-chain match (stricter than treating the bit
  as cleared).

Operational consequence: an LMS that cannot be cleared can leave
the cluster in an inconsistent visible state (witness slot says
`lms = 1`, mesh heartbeats say `lms = 0`; or two slots end up
`lms = 1` simultaneously after a handoff that crossed a witness
outage). Resolution then funnels through the staleness +
UUID-lineage path, which is more restrictive than the normal
"one LMS holder, others defer" model. Worth surfacing to
operators (alert on stuck LMS) and providing an explicit clear
command for the edge case.

## Doubts / uncertainties (fill as we go)
- (DRBD UUID timestamp / recency question retired — DRBD UUIDs
  don't carry useful timestamps and history-chain membership is
  the better, well-defined safety net.)
- Cold-boot with peer reachable but neither side LMS: how is the
  promoter chosen — by local file, by deterministic tie-break, or
  by inspection of last witness slots? (Tracked as next question.)

## Pending sub-questions raised by the multi-phase protocol
- Mesh heartbeat payload extension: what fields does each peer's
  heartbeat need to carry so peers can see one another's
  `state.json` view? (Likely: `current_master`,
  `transitioning`, `last_man_standing`, `last_drbd_uuid_observed`,
  `cluster_uuid`, signed with cluster key.)
- Ack timeout: how long does a claiming node wait for the quorum
  threshold before aborting? (Witness reply latency typically
  < 100 ms; peer heartbeat tick is 1 Hz; total must be a few
  ticks worst case.)
- Peer-refuses-to-ack: what happens if a peer sees the claim and
  refuses (e.g. because the peer already considers itself master
  or because of an inconsistency)? Is "refusal" silent (peer's
  heartbeat just keeps showing the old `current_master`) or
  explicit (peer broadcasts a different view that's clearly a
  conflict)?
- Multi-claimer race: two peers simultaneously start the protocol
  for becoming master. Both write their own files first, both
  broadcast. Peers ack whichever they observe first. The losing
  claimant aborts when it sees its own claim outnumbered. Need to
  verify this resolves deterministically.

## Election flow (one 1 Hz tick)

```
                       ┌─────────────────────────┐
                       │  TICK START (1 Hz)      │
                       └────────────┬────────────┘
                                    │
              ┌─────────────────────▼─────────────────────┐
              │  READ INPUTS (all should be in memory;    │
              │  refresh fast from these sources)         │
              │                                            │
              │   • own DRBD current-UUID + history chain  │
              │     (debugfs)                              │
              │   • signed mesh heartbeats from peers      │
              │     (last received per peer)               │
              │   • signed witness slot replies            │
              │     (last received per witness)            │
              │   • state.json (own last election state)   │
              │   • local UUID history (field in state.json)│
              │   • rqlite UUID history table (if reachable)│
              │   • /run/bedrock-cluster.fence marker      │
              └─────────────────────┬─────────────────────┘
                                    │
                                    ▼
                       ┌────────────────────────┐
                       │  fence marker present? ├── yes ──► role = FENCED, done
                       └────────────┬───────────┘
                                    │ no
                                    ▼
                       ┌────────────────────────┐
                       │  self in maintenance?  ├── yes ──► role = FOLLOWER (0 vote), done
                       └────────────┬───────────┘
                                    │ no
                                    ▼
              ┌─────────────────────────────────────────┐
              │  ELIGIBILITY: does ANY known peer have  │
              │  a newer DRBD UUID than mine?           │
              │  (peer's UUID NOT in my history chain   │
              │   AND mine IS in their history chain)   │
              │                                          │
              │  Evidence sources for peer UUID:        │
              │   • peer's mesh heartbeat (newest)      │
              │   • peer's witness slot marker (signed) │
              │   • rqlite UUID history table           │
              │   • own state.json UUID history field   │
              └──────────────────┬──────────────────────┘
                                 │
                  yes (a peer is ahead)              no (I am at-least-as-up-to-date)
                                 │                          │
                                 ▼                          ▼
              role = FOLLOWER             ┌──────────────────────────────────┐
              (refuse to claim;           │  COUNT VOTES                     │
              await operator              │   = 1 (self)                     │
              seize)                      │   + Σ peers whose mesh heartbeat │
                                          │     shows current_master = me    │
                                          │   + 1 if self.tag.lms is         │
                                          │     witness-quorum-acked         │
                                          └──────────────┬───────────────────┘
                                                         │
                                          ┌──────────────▼───────────────┐
                                          │  votes ≥ strict majority of  │
                                          │  voting pool?                │
                                          │  (pool = cluster.json nodes  │
                                          │   minus maintenance)         │
                                          └──────────────┬───────────────┘
                                                         │
                                       no                │              yes
                                       │                                │
                                       ▼                                ▼
                          role = FOLLOWER (if a       ┌──────────────────────────┐
                          peer is observed as         │ another peer also shows  │
                          master) or NOQUORUM         │ tag.lms=1 fresh AND      │
                          (no master observable)      │ claims master?           │
                                                      └──────────┬───────────────┘
                                                                 │
                                            yes                  │              no
                                            │                                   │
                                            ▼                                   ▼
                                  apply tie-break:                   role = LEADER
                                  1. DRBD UUID + history
                                     (newer wins; if neither
                                      in other's history,
                                      refuse and operator)
                                  2. lowest loopback octet
                                  → role = LEADER or FOLLOWER

                              ┌─────────────────────────────────────────────┐
                              │  COMPARE computed role to actuator state    │
                              │                                              │
                              │  same  → no-op                              │
                              │  change → run two-phase transition:         │
                              │            phase 1: update state.json,      │
                              │                     set transitioning=1,    │
                              │                     immediate heartbeat,    │
                              │                     write witness slot      │
                              │                     (sentinel marker + lms) │
                              │            phase 2: wait for convergence    │
                              │                     (peer heartbeats +      │
                              │                      witness acks reflect   │
                              │                      me as new master)      │
                              │            phase 3: actuate (drbdadm        │
                              │                     primary → new UUID,     │
                              │                     mount, .254, services)  │
                              │            phase 4: update state.json,      │
                              │                     clear transitioning,    │
                              │                     write witness slot      │
                              │                     (real UUID),            │
                              │                     immediate heartbeat     │
                              └─────────────────────────────────────────────┘
```

## Glossary
- **LMS** — "last-man-standing". A flag on a node's witness slot
  asserting "I am hosting the cluster alone because the peer is
  gone." Set only after a witness-quorum-acknowledged write.
- **Witness quorum** — majority of all configured witnesses, where
  each contributing witness must be **valid** (has slot entries
  for every cluster node).
- **Valid witness** — a witness whose last reply contained a slot
  for every node in `cluster.json`.
- **Patience window** — fixed 30 s after bedrock-d start during
  which no witness-assisted decision fires.
