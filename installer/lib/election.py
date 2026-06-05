"""Weighted-vote election — pure function over observable cluster state.

Per docs/cluster-quorum-spec.md + EXECUTION-PLAN BAD-1, election decides
Leader/Follower/NoQuorum for the cluster base layer (bedrock-d, no rqlite
dependency — this is what *recovers* rqlite). The arbiter takeover
protocol (witness slot inspection, drbd_uuid match, own-slot
write+readback) lives in lib/cluster_arbiter.py and is gated by this
election's Leader outcome.

Vote model (100/1):
  * node = 100 votes; each valid+confirmed witness = 1 vote.
  * total_votes = 100·N_active_nodes + N_configured_witnesses
    (both counts from rqlite/cluster_state).
  * majority = total_votes // 2 + 1.
  * my_votes = 100·(node acks incl. self) + (valid+confirmed witnesses).
  Rationale for 100/1: a witness — even several — can only ever break an
  exact node-tie, never overrule a real node.

Votes are ACTIVE ACKS, not passive reachability. A peer grants its 100
votes to a candidate only if the peer has ALSO lost the master AND finds
the candidate eligible. Eligibility is the candidate's advertised
arbiter-DRBD UUID checked against the voter's own local 7-day UUID
history (lib/state.classify_arbiter_uuid): superseded ⇒ refuse, current
or unseen ⇒ votable. That ack/eligibility decision is made by each peer
in netd and arrives here as `peer_acks` — this function just tallies it.

The denominator rule: ALL configured witnesses count in total_votes,
only valid+confirmed ones add to my_votes. A configured-but-invalid
witness therefore *raises* the majority bar and biases toward "do not
fail over" (3 configured / 1 valid / 2 nodes: total=203, majority=102,
lone survivor 100+1=101 < 102 → no takeover → safe).

Outputs:
  - Outcome ∈ {Leader, Follower, NoQuorum}
  - should_set_mgmt_master: True iff caller should write self as the
    mgmt_master in rqlite (post-takeover bookkeeping; NOT a takeover
    gate).

NoQuorum is also returned (sticky) whenever the no-quorum marker file is
present — an operator/explicit override that survives transient quorum
gains."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

NO_QUORUM_MARKER = Path("/run/bedrock-no-quorum")
VOTES_PER_NODE = 100
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
    acking_peers: tuple[str, ...] = ()
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
    current_mgmt_master: str | None,
    n_configured_witnesses: int = 0,
    n_valid_witnesses: int = 0,
    peer_acks: dict[str, bool] | None = None,
    casting_vote_node: str | None = None,
    master_witness_alive: bool = False,
    no_quorum_marker_path: Path = NO_QUORUM_MARKER,
) -> Election:
    """One-shot election decision. See module docstring for semantics.

    Counts (from rqlite/cluster_state):
      * node_loopbacks — dict[node_name, loopback_ip] of the cluster's
        ACTIVE nodes (state=='active' AND not maintenance), filtered by
        netd before this call. ITS KEY SET (∪ self) is the election
        denominator — n_nodes — NOT the heard-from set (C1). This is what
        makes a restarted, isolated master correctly see n_nodes=N and
        fall to NoQuorum instead of faking a one-node cluster.
      * peer_liveness — dict[node_name, bool], reachability from netd's
        neighbour table. SHOULD NOT include self (added here; an entry
        for self is tolerated and overridden to True). Used ONLY for
        reachability (the reachable set) and the ack tally — never for
        the denominator.
      * n_configured_witnesses — every witness in the cluster's rqlite
        `witnesses` table. The full denominator term.
      * n_valid_witnesses — witnesses that are both valid (slot for
        every active node) and confirmed (our own slot read back). Only
        these add to my_votes.

    Acks (the active-vote mechanism):
      * peer_acks — dict[node_name, bool]; True iff that peer has, in
        its node-to-node heartbeat, acked THIS node as master-to-be
        (it lost the master AND found our advertised arbiter-UUID
        eligible). When the current master is gone, a candidate's
        node-vote tally = self + acking peers, NOT all reachable peers.
        While a master is still alive (steady state) acks are
        irrelevant and reachability is used for the quorum check.
    """
    if no_quorum_marker_path.exists():
        return Election(
            outcome=Outcome.NO_QUORUM, my_votes=0, total_votes=0, majority=0,
            should_set_mgmt_master=False, reachable_peers=(),
            reason="no-quorum marker present (sticky)",
        )

    peer_acks = peer_acks or {}
    liveness = dict(peer_liveness)
    liveness[self_name] = True

    # Active node set = the cluster's ACTIVE nodes (per rqlite,
    # node_loopbacks is already filtered to state=='active' AND not
    # maintenance by netd) + self. The denominator is the ACTIVE-node
    # count, NOT the heard-from set (C1): a master that RESTARTS during a
    # partition reads all active nodes from rqlite, so n_nodes=N and it
    # stands alone < majority → NoQuorum (safe), instead of seeing
    # n_nodes=1 and keeping quorum by itself. Fresh-joiners are excluded
    # upstream via the 'joining' lifecycle state, not by a liveness
    # filter here, so the join-grace is preserved without letting an
    # isolated restart fake a single-node cluster. peer_liveness is used
    # ONLY for reachability (the reachable set + ack tally below).
    members = set(node_loopbacks)
    members.add(self_name)
    n_nodes = max(len(members), 1)
    reachable = tuple(sorted(n for n in members if liveness.get(n)))

    total_votes = VOTES_PER_NODE * n_nodes + VOTE_PER_WITNESS * n_configured_witnesses
    majority = total_votes // 2 + 1
    witness_votes = VOTE_PER_WITNESS * min(n_valid_witnesses, n_configured_witnesses)

    # DEATH-ORACLE: the master is alive if the mesh can reach it OR its witness
    # slot is FRESH and HOSTING (master_witness_alive, computed by netd from the
    # witness). So a clean mesh-only split does NOT make the far side think the
    # master died — it follows it instead of taking over. See
    # docs/witness-death-oracle.md.
    master_is_alive = bool(current_mgmt_master and (
        liveness.get(current_mgmt_master) or master_witness_alive))

    if current_mgmt_master == self_name:
        # Already master. Steady-state quorum check uses reachability:
        # as long as we still see a majority we keep the role.
        my_votes = VOTES_PER_NODE * len(reachable) + witness_votes
        # CASTING VOTE (2-node witness-loss rescue). When a 2-node cluster's only
        # witness is confirmed-bad + cleanly removed, the saga arms an explicit
        # casting_vote_node = the CURRENT MASTER, giving it +1 so it stays sticky
        # at 101/200 (no failover; if the master dies the cluster halts — accepted).
        # LOAD-BEARING: credited ONLY in this steady-state-master branch, NEVER the
        # follower (171) or promote (182) branches — so a partitioned FOLLOWER that
        # reads casting_vote_node==<the peer master> still computes exactly 100 →
        # NoQuorum. That asymmetry is the whole split-brain proof; do not move it.
        if casting_vote_node and casting_vote_node == self_name:
            my_votes += VOTE_PER_WITNESS
        if my_votes < majority:
            return Election(
                outcome=Outcome.NO_QUORUM, my_votes=my_votes,
                total_votes=total_votes, majority=majority,
                should_set_mgmt_master=False, reachable_peers=reachable,
                reason=f"master but only {my_votes}/{majority}",
            )
        return Election(
            outcome=Outcome.LEADER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason="already master",
        )

    # NODE-MAJORITY BOUND on deference (docs/witness-death-oracle.md rule 2):
    # - If the master is MESH-reachable, we are in steady state → always follow.
    # - If the master is alive only via the witness (mesh-unreachable — a
    #   partition), defer ONLY when we lack a node-majority among the nodes we
    #   CAN reach. If we hold a node-majority WITHOUT the master, the master is a
    #   provable minority that will self-demote, so we fall through to takeover.
    #   In a clean even split neither side has a node-majority, so we defer
    #   (correct: nothing moves).
    mesh_reachable_master = bool(
        current_mgmt_master and liveness.get(current_mgmt_master))
    i_have_node_majority = VOTES_PER_NODE * len(reachable) >= majority
    if master_is_alive and (mesh_reachable_master or not i_have_node_majority):
        # A live master we are not, and either we can reach it on the mesh or we
        # can't outvote it on nodes alone — follow it. (Quorum to *unseat* a live
        # master is never sought.)
        my_votes = VOTES_PER_NODE * len(reachable) + witness_votes
        return Election(
            outcome=Outcome.FOLLOWER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            reason=f"following {current_mgmt_master}",
        )

    # Master gone (or never set). This is the failover/promote decision:
    # node-votes = self (100) + each peer that has ACKED us (100 each).
    # Acks are active — a peer only acks once it too has lost the master
    # and found our advertised arbiter-UUID eligible.
    acking = tuple(sorted(
        n for n in members
        if n != self_name and liveness.get(n) and peer_acks.get(n)
    ))
    node_votes = VOTES_PER_NODE * (1 + len(acking))
    my_votes = node_votes + witness_votes

    if my_votes < majority:
        return Election(
            outcome=Outcome.NO_QUORUM, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            acking_peers=acking,
            reason=f"master gone; have {my_votes}/{majority} acks+witness",
        )

    # We have a quorum of acks. Deterministic tiebreak among the
    # reachable contenders so two candidates don't both promote: the
    # lowest-loopback-octet candidate proposes, everyone else defers a
    # tick and acks it instead.
    self_octet = _loopback_octet(self_loopback or node_loopbacks.get(self_name, ""))
    contender_octets = sorted(
        (_loopback_octet(node_loopbacks.get(n, "")), n) for n in reachable
    )
    if not contender_octets or contender_octets[0][1] != self_name:
        winner = contender_octets[0][1] if contender_octets else "?"
        return Election(
            outcome=Outcome.FOLLOWER, my_votes=my_votes,
            total_votes=total_votes, majority=majority,
            should_set_mgmt_master=False, reachable_peers=reachable,
            acking_peers=acking,
            reason=f"deferring to lower-octet {winner}",
        )

    return Election(
        outcome=Outcome.LEADER, my_votes=my_votes,
        total_votes=total_votes, majority=majority,
        should_set_mgmt_master=True, reachable_peers=reachable,
        acking_peers=acking,
        reason=f"master {current_mgmt_master or '<none>'} gone; "
               f"promoting self ({my_votes}/{majority}, "
               f"lowest octet {self_octet})",
    )


def set_no_quorum_marker(reason: str = "") -> None:
    """Drop the sticky no-quorum marker. Election will then return
    Outcome.NO_QUORUM regardless of current vote tally, until
    clear_no_quorum_marker() is called.

    Idempotent: if the marker already exists, do nothing. The file's
    mtime is the "when did this NoQuorum episode begin?" timestamp
    that downstream code (vm_failover suspend timer) reads — a
    per-tick rewrite would reset it and the suspend timer would
    never expire."""
    try:
        if NO_QUORUM_MARKER.exists():
            return
        NO_QUORUM_MARKER.parent.mkdir(parents=True, exist_ok=True)
        NO_QUORUM_MARKER.write_text(reason or "election: no quorum\n")
    except OSError:
        pass


def clear_no_quorum_marker() -> None:
    try:
        NO_QUORUM_MARKER.unlink()
    except FileNotFoundError:
        pass
