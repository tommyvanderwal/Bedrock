# `election.py`

**Module purpose.** Pure-function weighted-vote election. Takes
observable state (peer liveness from netd's neighbour table,
witness alive flag, current `mgmt_master` from rqlite/cluster.json)
and returns an `Election` outcome plus a `should_set_mgmt_master`
flag.

No I/O. No state. Owns the tie-break rules so they can be
unit-tested in isolation (and they are — `tests/test_netd_phase_a.py`
covers them).

**Weighted-vote formula.** `10` votes per known cluster node + `1`
for an alive witness. Total = `10·N + (1 if witness)`. Majority =
`total // 2 + 1`. A node's own vote tally = `10 ·
count(reachable_members) + (1 if witness_alive)`.

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
- `VOTES_PER_NODE = 10`, `VOTE_PER_WITNESS = 1` — vote weights.

## Enums + dataclasses

- `Outcome` enum: `LEADER`, `FOLLOWER`, `NO_QUORUM`.
- `Election(outcome, my_votes, total_votes, majority,
  should_set_mgmt_master, reachable_peers, reason)` — frozen
  dataclass returned by `compute`.

## Functions

- `_loopback_octet(ip) -> int` — internal. Returns the last octet
  of a `100.X.Y.Z` cluster-loopback as int, used for the
  lowest-octet-wins tie-break. Returns 9999 on parse failure so a
  malformed loopback can never accidentally be the "winner".
- `compute(*, self_name, self_loopback, peer_liveness,
  node_loopbacks, witness_alive, current_mgmt_master,
  no_quorum_marker_path=NO_QUORUM_MARKER) -> Election` — the only
  public entry point. Pure function: no I/O, no state, no time.

  Decision tree, in order:
  1. **Sticky no-quorum override** — if `no_quorum_marker_path`
     exists, immediately return `NO_QUORUM` with
     `reason="no-quorum marker present (sticky)"`. The caller's
     orchestrator `no_quorum_responder` waits for quorum recovery
     before clearing it.
  2. **Build the member set.** Include only nodes that are both in
     `node_loopbacks` (i.e. registered in rqlite) AND keys in
     `peer_liveness` (i.e. bedrock-net has observed them at least
     once). Self is always added. New joiners not yet observed
     are skipped — see module docstring.
  3. **Tally votes.** `total_votes = 10·N + (1 if witness)`,
     `majority = total // 2 + 1`. `my_votes = 10 ·
     count(reachable members) + (1 if witness_alive)`.
  4. **NoQuorum if `my_votes < majority`.** Caller (netd) writes
     the no-quorum marker + demotes singletons after a streak
     hold-down.
  5. **Already-master shortcut.** If `current_mgmt_master ==
     self_name` and we have quorum, return `LEADER,
     should_set_mgmt_master=False` (the rqlite row is already
     correct; no need to re-write).
  6. **Follower if current master is alive.** `Follower,
     should_set_mgmt_master=False, reason="following X"`.
  7. **Master is gone.** Promotion candidate: only the reachable
     peer with the lowest loopback octet promotes. Others defer
     ("Follower, reason=deferring to lower-octet").
  8. **Else return `LEADER, should_set_mgmt_master=True`.** Caller
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
