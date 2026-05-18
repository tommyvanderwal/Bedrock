"""Weighted-vote election — pure function over observable state.

Inputs come from three sources, all already in-process on every node:

  - `peer_liveness`: dict[node_name, bool] sourced from bedrock-net's
    neighbour table (`Neighbour.logged_up` aggregated by peer_node).
  - `witness_alive`: bool from lib/witness.WitnessState.
  - `cluster_info`: the rqlite snapshot — gives `mgmt_master` plus
    every node's `loopback_ip` (used as a deterministic tiebreaker).

Outputs:

  - `Outcome` ∈ {Leader, Follower, NoQuorum, Fenced}
  - `should_set_mgmt_master: bool` — True iff we should write
    `bs.set_mgmt_master(self_name)` to rqlite right now. Election only
    *proposes* a master move; rqlite Raft is the actual single-writer.

Weighted-vote formula (matches the original Rust prototype, kept so
the lessons-log entry still applies):

  total_votes = 10 * N_nodes + (1 if witness_present else 0)
  majority    = total_votes // 2 + 1
  my_votes    = 10 * count(reachable_peers, self_included) +
                (1 if witness_alive else 0)

  I am Leader iff:
    my_votes >= majority
    AND ( current_master is gone-or-down OR current_master == self )
    AND ( I am the lowest-loopback among reachable peers — tiebreaker
          so two co-eligible peers don't race to promote )

  I am Follower iff:
    my_votes >= majority and current_master is alive and != self.

  I am NoQuorum iff:
    my_votes < majority.

  I am Fenced iff:
    /run/bedrock-cluster.fence exists (operator/peer told us to stand
    down). Fence overrides Leader.

This module owns no state — caller passes in everything. It also owns
no side-effects — caller decides whether to act on
`should_set_mgmt_master`. Single pure function, easy to unit-test."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

FENCE_MARKER = Path("/run/bedrock-cluster.fence")
VOTES_PER_NODE = 10
VOTE_PER_WITNESS = 1


class Outcome(str, Enum):
    LEADER = "leader"
    FOLLOWER = "follower"
    NO_QUORUM = "noquorum"
    FENCED = "fenced"


@dataclass(frozen=True)
class Election:
    outcome: Outcome
    my_votes: int
    total_votes: int
    majority: int
    should_set_mgmt_master: bool
    reachable_peers: tuple[str, ...]
    reason: str = ""


def _loopback_octet(ip: str) -> int:
    """Last octet of a /32, used as deterministic tiebreaker. Returns
    a large number (effectively last) for malformed input so we never
    accidentally promote a node whose loopback we can't parse."""
    try:
        return int(ip.rsplit(".", 1)[-1])
    except Exception:
        return 9999


def compute(
    *,
    self_name: str,
    self_loopback: str,
    peer_liveness: dict[str, bool],
    node_loopbacks: dict[str, str],
    witness_alive: bool,
    current_mgmt_master: str | None,
    fence_marker_path: Path = FENCE_MARKER,
) -> Election:
    """One-shot election decision. See module docstring for semantics.

    `peer_liveness` SHOULD NOT include the self node (it's added here);
    extra entries for self are tolerated (overridden to True).
    """
    if fence_marker_path.exists():
        return Election(
            outcome=Outcome.FENCED, my_votes=0, total_votes=0, majority=0,
            should_set_mgmt_master=False, reachable_peers=(),
            reason="fence marker present",
        )

    # Self is always alive to itself.
    liveness = dict(peer_liveness)
    liveness[self_name] = True

    # Cluster size = every node we know about (from rqlite snapshot).
    # Use node_loopbacks's keys as the authoritative member set; that's
    # the rqlite `nodes` table after view_builder. Anything in
    # peer_liveness but not in node_loopbacks is ignored (a fresh
    # joiner not yet committed to rqlite).
    members = set(node_loopbacks)
    members.add(self_name)
    n_nodes = max(len(members), 1)
    reachable = tuple(sorted(n for n in members if liveness.get(n)))

    total_votes = VOTES_PER_NODE * n_nodes + (VOTE_PER_WITNESS if witness_alive else 0)
    majority = total_votes // 2 + 1
    my_votes = VOTES_PER_NODE * len(reachable) + (VOTE_PER_WITNESS if witness_alive else 0)

    if my_votes < majority:
        return Election(
            outcome=Outcome.NO_QUORUM, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason=f"have {my_votes}/{majority}",
        )

    master_is_alive = bool(current_mgmt_master and liveness.get(current_mgmt_master))

    if current_mgmt_master == self_name:
        return Election(
            outcome=Outcome.LEADER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason="already master",
        )

    if master_is_alive:
        return Election(
            outcome=Outcome.FOLLOWER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason=f"following {current_mgmt_master}",
        )

    # Master is gone (or never set). Deterministic tiebreak: only the
    # reachable peer with the lowest loopback proposes. Everyone else
    # waits a tick and converges on the same answer.
    self_octet = _loopback_octet(self_loopback or node_loopbacks.get(self_name, ""))
    contender_octets = [
        (_loopback_octet(node_loopbacks.get(n, "")), n)
        for n in reachable
    ]
    contender_octets.sort()
    if not contender_octets or contender_octets[0][1] != self_name:
        winner = contender_octets[0][1] if contender_octets else "?"
        return Election(
            outcome=Outcome.FOLLOWER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason=f"deferring to lower-octet {winner}",
        )

    return Election(
        outcome=Outcome.LEADER, my_votes=my_votes,
        total_votes=total_votes, majority=majority,
        should_set_mgmt_master=True, reachable_peers=reachable,
        reason=f"master {current_mgmt_master or '<none>'} gone; "
               f"promoting self (lowest octet {self_octet})",
    )


def write_fence_marker(reason: str = "") -> None:
    """Drop the fence marker — election → NoQuorum self-fence path."""
    try:
        FENCE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        FENCE_MARKER.write_text(reason or "election: no quorum\n")
    except OSError:
        pass


def clear_fence_marker() -> None:
    try:
        FENCE_MARKER.unlink()
    except FileNotFoundError:
        pass
