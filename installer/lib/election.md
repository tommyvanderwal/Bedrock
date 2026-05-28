# `election.py`

**Module purpose.** Pure-function weighted-vote election. Takes
observable state (peer liveness + per-peer acks from netd's neighbour
table, configured/valid witness counts, current `mgmt_master` from
rqlite/cluster_state) and returns an `Election` outcome plus a
`should_set_mgmt_master` flag.

No I/O. No state. Owns the tie-break rules so they can be
unit-tested in isolation (and they are — `tests/test_election.py` +
`tests/test_netd_phase_a.py` cover them).

**Weighted-vote formula (100/1).** `100` votes per active cluster node
+ `1` per *valid+confirmed* witness. `total_votes = 100·N_active_nodes
+ N_configured_witnesses` (both from rqlite). Majority = `total // 2 +
1`. At failover a candidate's tally = `100·(self + acking peers) +
valid_witnesses`; in steady state (we are master / following a live
master) the node term uses reachable members.

**Denominator rule.** ALL configured witnesses count in `total_votes`;
only valid+confirmed ones add to `my_votes`. A configured-but-invalid
witness therefore *raises* the majority bar and biases toward "do not
fail over" (3 configured / 1 valid / 2 nodes → total=203, majority=102,
lone survivor 100+1=101 < 102 → no takeover → safe).

**Active acks, not reachability.** When the master is gone, a peer only
contributes its 100 votes if it has ACKED this candidate in its
node-to-node heartbeat — meaning the peer also lost the master AND found
the candidate's advertised arbiter-DRBD UUID eligible (classified
against the peer's own local 7-day UUID history via
`lib.state.classify_arbiter_uuid`). The ack/eligibility decision is made
per-peer in netd; `compute()` just tallies the `peer_acks` map.

**"Known cluster member"** at the election level means a node whose
loopback is in `cluster.json` AND has been observed by bedrock-net
at least once (caller passes `peer_liveness` keyed only by
ever-seen peers — see `netd.Daemon.ever_seen_peers`). Fresh
joiners that haven't probed back aren't counted yet, so master
doesn't go NoQuorum mid-join.

## Constants

- `NO_QUORUM_MARKER = Path("/run/bedrock-no-quorum")` — sticky
  marker file. When present, `compute()` returns `NO_QUORUM`
  regardless of vote tally, until `clear_no_quorum_marker()` is
  called. Written when election goes NoQuorum + holddown;
  `mgmt/orchestrator.no_quorum_responder` watches it and pauses
  local VMs, then clears it after quorum returns.
- `VOTES_PER_NODE = 100`, `VOTE_PER_WITNESS = 1` — vote weights.

## Enums + dataclasses

- `Outcome` enum: `LEADER`, `FOLLOWER`, `NO_QUORUM`.
- `Election(outcome, my_votes, total_votes, majority,
  should_set_mgmt_master, reachable_peers, acking_peers, reason)` —
  frozen dataclass returned by `compute`.

## Functions

- `_loopback_octet(ip) -> int` — internal. Returns the last octet
  of a `100.X.Y.Z` cluster-loopback as int, used for the
  lowest-octet-wins tie-break. Returns 9999 on parse failure so a
  malformed loopback can never accidentally be the "winner".
- `compute(*, self_name, self_loopback, peer_liveness,
  node_loopbacks, current_mgmt_master, n_configured_witnesses=0,
  n_valid_witnesses=0, peer_acks=None,
  no_quorum_marker_path=NO_QUORUM_MARKER) -> Election` — the only
  public entry point. Pure function: no I/O, no state, no time.

  Decision tree, in order:
  1. **Sticky no-quorum override** — if `no_quorum_marker_path`
     exists, immediately return `NO_QUORUM` with
     `reason="no-quorum marker present (sticky)"`. The caller's
     orchestrator `no_quorum_responder` waits for quorum recovery
     before clearing it.
  2. **Build the active-node set.** Include only nodes that are both
     in `node_loopbacks` (registered in rqlite) AND keys in
     `peer_liveness` (bedrock-net has observed them at least once).
     Self is always added. New joiners not yet observed are skipped.
  3. **Compute the denominator.** `total_votes = 100·N +
     1·N_configured_witnesses`, `majority = total // 2 + 1`. Only
     `min(n_valid_witnesses, n_configured_witnesses)` add to
     `my_votes`.
  4. **Already-master shortcut.** If `current_mgmt_master ==
     self_name`: node term is `100·count(reachable)`; if that +
     witnesses `< majority` go NoQuorum, else `LEADER,
     should_set_mgmt_master=False`.
  5. **Follower if current master is alive.** `Follower,
     should_set_mgmt_master=False, reason="following X"`.
  6. **Master is gone — the failover decision.** Node term is
     `100·(self + acking peers)`. If `my_votes < majority` →
     `NoQuorum` (not enough acks/witnesses). Otherwise the reachable
     candidate with the lowest loopback octet promotes; others defer
     (`Follower, reason="deferring to lower-octet"`).
  7. **Else return `LEADER, should_set_mgmt_master=True`.** Caller
     writes `bs.set_mgmt_master(self)` to rqlite (Raft enforces
     single-writer in case two peers race). The actual takeover
     (drbdadm primary + `.254` + filer + s3) is gated separately
     by `cluster_arbiter.promote_to_arbiter_host()` per
     `docs/cluster-quorum-spec.md` — election only decides
     Leader/Follower; the takeover protocol decides whether it's
     safe to flip `.254`.
- `set_no_quorum_marker(reason)` — drop `/run/bedrock-no-quorum`
  with the reason text. Best-effort: silently swallows OSError
  (we don't want to crash the election tick if /run is read-only).
- `clear_no_quorum_marker()` — unlink the marker, swallow
  FileNotFoundError. Called by orchestrator's
  `no_quorum_responder` after cleanup + quorum return.
