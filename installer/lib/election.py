"""Weighted-vote election — pure function over observable mesh state.

Per docs/cluster-quorum-spec.md, election decides Leader/Follower/
NoQuorum from MESH peer-liveness + witness reachability only. The
arbiter takeover protocol (witness slot inspection, drbd_uuid
match, own-slot write+readback) lives in lib/cluster_arbiter.py and
is gated by this election's Leader outcome.

Inputs (all in-process on every node):
  - peer_liveness: dict[node_name, bool] from netd's neighbour table
  - witness_alive: bool from lib/witness.is_alive(ws)
  - node_loopbacks + current_mgmt_master: from rqlite

Outputs:
  - Outcome ∈ {Leader, Follower, NoQuorum}
  - should_set_mgmt_master: True iff caller should write self as the
    mgmt_master in rqlite (post-takeover bookkeeping; NOT a takeover
    gate).

Weighted-vote formula (unchanged from the original Rust prototype):
  total_votes = 10 * N_nodes + (1 if witness_alive else 0)
  majority    = total_votes // 2 + 1
  my_votes    = 10 * count(reachable) + (1 if witness_alive else 0)

Leader iff my_votes >= majority AND (current_master gone OR == self),
with lowest-loopback-octet as deterministic tiebreaker. Follower if
master is alive and != self. NoQuorum if below majority OR if the
sticky no-quorum marker file is present (operator/explicit override
that survives transient quorum gains)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

NO_QUORUM_MARKER = Path("/run/bedrock-no-quorum")
VOTES_PER_NODE = 10
VOTE_PER_WITNESS = 1


class Outcome(str, Enum):
    LEADER = "leader"
    FOLLOWER = "follower"
    NO_QUORUM = "noquorum"


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
    no_quorum_marker_path: Path = NO_QUORUM_MARKER,
) -> Election:
    """One-shot election decision. See module docstring for semantics.

    `peer_liveness` SHOULD NOT include self (added here). Extra entries
    for self are tolerated (overridden to True)."""
    if no_quorum_marker_path.exists():
        return Election(
            outcome=Outcome.NO_QUORUM, my_votes=0, total_votes=0, majority=0,
            should_set_mgmt_master=False, reachable_peers=(),
            reason="no-quorum marker present (sticky)",
        )

    liveness = dict(peer_liveness)
    liveness[self_name] = True

    # Cluster membership = nodes we've ever heard from + self. Filters
    # fresh-joiners that haven't probed back yet (avoids the master
    # going NoQuorum during a join).
    members = {n for n in node_loopbacks if n in peer_liveness}
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

    # Master gone (or never set). Deterministic tiebreak: lowest-octet
    # reachable peer proposes; everyone else waits a tick and converges.
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


def set_no_quorum_marker(reason: str = "") -> None:
    """Drop the sticky no-quorum marker. Election will then return
    Outcome.NO_QUORUM regardless of current vote tally, until
    clear_no_quorum_marker() is called."""
    try:
        NO_QUORUM_MARKER.parent.mkdir(parents=True, exist_ok=True)
        NO_QUORUM_MARKER.write_text(reason or "election: no quorum\n")
    except OSError:
        pass


def clear_no_quorum_marker() -> None:
    try:
        NO_QUORUM_MARKER.unlink()
    except FileNotFoundError:
        pass
