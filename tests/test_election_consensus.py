"""Unit tests for the BAD-1 consensus core (Phase 2).

Covers the three load-bearing pure-function pieces:

  * lib.election.compute — the 100/1 vote math, the denominator rule
    (all configured witnesses count, only valid ones tally), and the
    ack-based failover decision.
  * lib.witness.is_valid / is_confirmed / drain_replies membership
    filter — witness validity hinges on holding a slot for every active
    node.
  * lib.state.classify_arbiter_uuid / record_arbiter_uuid — the local
    7-day arbiter-UUID history that drives split-brain eligibility.

Pure functions over inputs; no live rqlite / witness / DRBD needed.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import election  # noqa: E402
from lib import witness   # noqa: E402
from lib import state as lstate  # noqa: E402


# ────────────────────────────────────────────────────────────────────
#  election.compute — vote math + denominator rule
# ────────────────────────────────────────────────────────────────────

NO_MARKER = Path("/nonexistent/bedrock-no-quorum-marker")


def _loops(*names):
    """node_loopbacks for names sim-1..sim-N → 100.64.0.X."""
    return {n: f"100.64.0.{i + 1}" for i, n in enumerate(names)}


def test_vote_weights_are_100_and_1():
    assert election.VOTES_PER_NODE == 100
    assert election.VOTE_PER_WITNESS == 1


def test_single_node_no_master_promotes_self():
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={}, node_loopbacks=_loops("sim-1"),
        current_mgmt_master=None,
        n_configured_witnesses=0, n_valid_witnesses=0,
        no_quorum_marker_path=NO_MARKER,
    )
    # total = 100·1 = 100, majority = 51, my_votes = 100 (self ack).
    assert r.total_votes == 100
    assert r.majority == 51
    assert r.outcome is election.Outcome.LEADER
    assert r.should_set_mgmt_master is True


def test_already_master_with_quorum_does_not_rewrite():
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={"sim-2": True}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.outcome is election.Outcome.LEADER
    assert r.should_set_mgmt_master is False
    assert r.reason == "already master"


def test_follower_when_master_alive():
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": True}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.outcome is election.Outcome.FOLLOWER
    assert r.should_set_mgmt_master is False


def test_failover_needs_acks_not_mere_reachability():
    # 2 nodes, master sim-1 gone. sim-2 reachable to nobody else, no
    # acks → only its own 100 votes. total = 200, majority = 101.
    # 100 < 101 → NoQuorum (a lone survivor must NOT auto-promote at
    # N=2 without a witness/ack).
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        peer_acks={},
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 200
    assert r.majority == 101
    assert r.my_votes == 100
    assert r.outcome is election.Outcome.NO_QUORUM


def test_failover_three_nodes_one_ack_promotes_lowest_octet():
    # 3 nodes, master sim-1 gone. sim-2 (octet 2) sees sim-3 ack it.
    # total = 300, majority = 151. self + ack = 200 >= 151 → and
    # sim-2 is the lowest-octet reachable contender → LEADER.
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False, "sim-3": True},
        node_loopbacks=_loops("sim-1", "sim-2", "sim-3"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        peer_acks={"sim-3": True},
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 300
    assert r.majority == 151
    assert r.my_votes == 200
    assert r.acking_peers == ("sim-3",)
    assert r.outcome is election.Outcome.LEADER
    assert r.should_set_mgmt_master is True


def test_failover_defers_to_lower_octet_contender():
    # sim-3 (octet 3) has the acks for quorum but sim-2 (octet 2) is
    # also a reachable contender → sim-3 defers, doesn't double-promote.
    r = election.compute(
        self_name="sim-3", self_loopback="100.64.0.3",
        peer_liveness={"sim-1": False, "sim-2": True},
        node_loopbacks=_loops("sim-1", "sim-2", "sim-3"),
        current_mgmt_master="sim-1",
        peer_acks={"sim-2": True},
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.outcome is election.Outcome.FOLLOWER
    assert "deferring to lower-octet sim-2" in r.reason


def test_denominator_rule_invalid_witnesses_block_lone_survivor():
    # THE locked example: 3 configured witnesses, only 1 valid, 2 nodes.
    # total = 100·2 + 3 = 203, majority = 102. Lone survivor sim-2
    # (master gone, no peer acks) = 100 + 1 valid witness = 101 < 102
    # → no takeover → safe.
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=3, n_valid_witnesses=1,
        peer_acks={},
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 203
    assert r.majority == 102
    assert r.my_votes == 101
    assert r.outcome is election.Outcome.NO_QUORUM


def test_lms_survivor_with_all_witnesses_valid_keeps_quorum():
    # Same 2-node failover but all 3 witnesses valid: 100 + 3 = 103
    # >= 102 → the survivor reaches quorum and promotes.
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=3, n_valid_witnesses=3,
        peer_acks={},
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 203
    assert r.my_votes == 103
    assert r.outcome is election.Outcome.LEADER


def test_denominator_is_active_node_count_not_heard_from(monkeypatch):
    # C1: the denominator = the ACTIVE-node set (node_loopbacks ∪ self),
    # NOT the heard-from (peer_liveness) set. A restarted, isolated
    # master sees all N active nodes in node_loopbacks but only itself
    # reachable. Previously it would have filtered the denominator down
    # to {self} (n_nodes=1) and kept quorum alone (the split-brain bug).
    # Now n_nodes=2 → 100 < majority 101 → NoQuorum (safe).
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        # No peer heard from at all (we just restarted, partitioned).
        peer_liveness={},
        # rqlite still lists both active nodes.
        node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 200      # 100·2, NOT 100·1
    assert r.majority == 101
    assert r.my_votes == 100         # only self reachable
    assert r.outcome is election.Outcome.NO_QUORUM


def test_joining_node_excluded_from_denominator():
    # C1: netd filters node_loopbacks to ACTIVE nodes before calling
    # compute, so a mid-join 'joining' node is simply absent from the
    # denominator. With sim-2 excluded, the master sim-1 is a 1-node
    # cluster (n_nodes=1, majority 51) and keeps quorum alone during the
    # join — the join-grace the lifecycle state preserves.
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={},                 # joiner hasn't probed back yet
        node_loopbacks=_loops("sim-1"),   # sim-2 ('joining') excluded
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 100
    assert r.majority == 51
    assert r.my_votes == 100
    assert r.outcome is election.Outcome.LEADER


def test_netd_is_active_filter_excludes_joining_and_maintenance():
    # The active-node filter netd applies before building node_loopbacks
    # (C1). Mirror its predicate against a view_builder-shaped nodes dict
    # (state + maintenance always present) and assert the denominator
    # input is the active set, with self always kept.
    nodes = {
        "sim-1": {"loopback_ip": "100.64.0.1", "state": "active",
                  "maintenance": False},
        "sim-2": {"loopback_ip": "100.64.0.2", "state": "joining",
                  "maintenance": False},
        "sim-3": {"loopback_ip": "100.64.0.3", "state": "active",
                  "maintenance": True},
        "sim-4": {"loopback_ip": "100.64.0.4", "state": "active",
                  "maintenance": False},
    }
    my_node = "sim-1"

    def _is_active(name, info):
        if name == my_node:
            return True
        info = info or {}
        return (info.get("state", "active") == "active"
                and not info.get("maintenance"))

    active = {n for n, i in nodes.items() if _is_active(n, i)}
    # sim-2 ('joining') and sim-3 (maintenance) drop out; sim-1 + sim-4 stay.
    assert active == {"sim-1", "sim-4"}


def test_valid_witness_count_capped_at_configured():
    # Defensive: never count more valid witnesses than are configured.
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={}, node_loopbacks=_loops("sim-1"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=1, n_valid_witnesses=5,
        no_quorum_marker_path=NO_MARKER,
    )
    # total = 100 + 1 = 101; my_votes = 100 + min(5,1)=101.
    assert r.total_votes == 101
    assert r.my_votes == 101


# ────────────────────────────────────────────────────────────────────
#  witness validity / confirmation / membership filter
# ────────────────────────────────────────────────────────────────────

def _ws(member_ids=None, my_id=2):
    return witness.WitnessState(
        cluster_uuid="u" * 16, cluster_key=b"k" * 32,
        my_node_id=my_id, member_ids=member_ids,
    )


def _slot(nid, ts_ms=None, tag=0, marker=b"abc"):
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return witness.Slot(node_id=nid, ts_writer_ms=ts_ms, tag=tag,
                        marker=marker, seen_at_monotonic=time.monotonic())


def test_witness_invalid_when_membership_unknown():
    ws = _ws(member_ids=None)
    ws.slots = {1: _slot(1), 2: _slot(2)}
    assert witness.is_valid(ws) is False  # no member set => can't certify


def test_witness_invalid_when_a_member_slot_is_missing():
    ws = _ws(member_ids={1, 2, 3})
    ws.slots = {1: _slot(1), 2: _slot(2)}  # node 3 absent
    assert witness.is_valid(ws) is False


def test_witness_valid_when_every_member_has_a_slot_even_if_stale():
    ws = _ws(member_ids={1, 2})
    stale = int(time.time() * 1000) - 3_600_000  # an hour old — fine
    ws.slots = {1: _slot(1, ts_ms=stale), 2: _slot(2, ts_ms=stale)}
    assert witness.is_valid(ws) is True


def test_witness_confirmed_requires_fresh_own_slot_with_our_marker():
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    # Our own slot present, fresh, marker matches → confirmed.
    ws.slots = {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    assert witness.is_confirmed(ws) is True
    # Marker mismatch (witness hasn't taken our latest write) → not confirmed.
    ws.slots = {1: _slot(1), 2: _slot(2, marker=b"oldgen")}
    assert witness.is_confirmed(ws) is False
    # Own slot stale → not confirmed.
    old = int(time.time() * 1000) - witness.SLOT_STALE_MS - 1
    ws.slots = {1: _slot(1), 2: _slot(2, marker=b"mygen", ts_ms=old)}
    assert witness.is_confirmed(ws) is False


def _ep(echo_id, slots, fresh=True):
    ep = witness.EchoEndpoint(addr=("1.2.3.4", 12321), echo_id=echo_id)
    ep.slots = slots
    ep.last_reply_monotonic = (time.monotonic() if fresh
                               else time.monotonic() - witness.WITNESS_FRESHNESS_S - 1)
    return ep


def test_count_valid_confirmed_single_witness_yields_0_or_1():
    # M10: single configured witness, valid+confirmed → 1; capped at
    # n_configured so it never exceeds it.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    ws.discovered = {"e1": _ep("e1", good)}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1
    # A member slot missing → not valid → 0.
    ws.discovered = {"e1": _ep("e1", {2: _slot(2, marker=b"mygen")})}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0


def test_count_valid_confirmed_tallies_multiple_witnesses():
    # M10: 3 configured witnesses, 2 individually valid+confirmed, 1
    # missing a member slot → tally is 2 (each valid one contributes +1).
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = lambda: {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    bad = {1: _slot(1)}  # node 2 (us) missing → not confirmed
    ws.discovered = {
        "e1": _ep("e1", good()),
        "e2": _ep("e2", good()),
        "e3": _ep("e3", bad),
    }
    assert witness.count_valid_confirmed(ws, n_configured=3) == 2


def test_count_valid_confirmed_binds_to_configured_witness_ids():
    # Split-brain guard: a valid+confirmed endpoint whose echo_id is NOT a
    # configured witness_id (a rogue Echo holding the cluster key, or a
    # just-REMOVED witness's not-yet-aged entry) must NOT supply a vote.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = lambda: {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    # echo_id not in the configured set → 0
    ws.configured_witness_ids = {"w-known"}
    ws.discovered = {"w-rogue": _ep("w-rogue", good())}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0
    # echo_id IS a configured witness_id → counts
    ws.discovered = {"w-known": _ep("w-known", good())}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1
    # mixed: only the configured one counts
    ws.discovered = {"w-known": _ep("w-known", good()),
                     "w-rogue": _ep("w-rogue", good())}
    assert witness.count_valid_confirmed(ws, n_configured=2) == 1
    # None (early boot, membership unknown) → no filter (back-compat)
    ws.configured_witness_ids = None
    ws.discovered = {"w-rogue": _ep("w-rogue", good())}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1
    # EMPTY set (lagging local replica momentarily read 0 witnesses) → no
    # filter, so a live legit Echo is NOT evicted (availability guard).
    ws.configured_witness_ids = set()
    ws.discovered = {"w-known": _ep("w-known", good())}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_count_valid_confirmed_caps_at_configured():
    # M10: more valid Echoes answering than configured → capped at the
    # configured count (a rogue extra Echo can't inflate the vote).
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = lambda: {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    ws.discovered = {"e1": _ep("e1", good()), "e2": _ep("e2", good())}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_count_valid_confirmed_skips_stale_witness():
    # M10: a witness whose last reply is stale doesn't count.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    ws.discovered = {"e1": _ep("e1", good, fresh=False)}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0


# ── fileshare-backend fold into the tally (witness_file worker → verdict) ──

def _fv(witness_id, valid_confirmed=True, fresh=True):
    return witness.FileWitnessVerdict(
        witness_id=witness_id, valid_confirmed=valid_confirmed,
        evaluated_monotonic=(time.monotonic() if fresh
                             else time.monotonic() - witness.WITNESS_FRESHNESS_S - 1),
    )


def test_count_valid_confirmed_folds_fresh_fileshare_witness():
    # A fileshare witness whose cached verdict is fresh + valid_confirmed adds +1.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.file_witnesses = {"fs1": _fv("fs1")}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_count_valid_confirmed_fileshare_stale_verdict_not_counted():
    # A hung/dead IO worker → verdict ages past the freshness window → 0
    # (biases toward "do not fail over").
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.file_witnesses = {"fs1": _fv("fs1", fresh=False)}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0


def test_count_valid_confirmed_fileshare_invalid_verdict_not_counted():
    # Worker evaluated it as NOT valid+confirmed (missing member slot / our
    # readback failed) → 0 even though fresh.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.file_witnesses = {"fs1": _fv("fs1", valid_confirmed=False)}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0


def test_count_valid_confirmed_fileshare_binds_to_configured_ids():
    # Same split-brain binding as Echo: a fileshare verdict whose witness_id is
    # not configured must not vote; falsy configured set = no filter.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.file_witnesses = {"fs-rogue": _fv("fs-rogue")}
    ws.configured_witness_ids = {"fs-known"}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0
    ws.file_witnesses = {"fs-known": _fv("fs-known")}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1
    ws.configured_witness_ids = None          # early boot → no filter
    ws.file_witnesses = {"fs-rogue": _fv("fs-rogue")}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_count_valid_confirmed_mixes_echo_and_fileshare():
    # One Echo + one fileshare, both valid+confirmed → tally 2.
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    ws.discovered = {"e1": _ep("e1", good)}
    ws.file_witnesses = {"fs1": _fv("fs1")}
    assert witness.count_valid_confirmed(ws, n_configured=2) == 2


def test_count_valid_confirmed_same_id_both_backends_counts_once():
    # Defensive: if one witness_id surfaced under BOTH backends, the set means
    # it contributes exactly one vote (never double-counts the denominator).
    ws = _ws(member_ids={1, 2}, my_id=2)
    ws.own_marker = b"mygen"
    good = {1: _slot(1), 2: _slot(2, marker=b"mygen")}
    ws.discovered = {"w1": _ep("w1", good)}
    ws.file_witnesses = {"w1": _fv("w1")}
    assert witness.count_valid_confirmed(ws, n_configured=2) == 1


def test_drain_replies_drops_non_member_slots():
    # Build a real ack envelope so drain_replies runs end-to-end, then
    # confirm a decommissioned node's slot (id 9) is filtered out.
    ws = _ws(member_ids={1, 2})
    # Stand up a loopback socket pair to feed one packet.
    import socket
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.setblocking(False)
    ws.sock = rx
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    now_ms = int(time.time() * 1000)
    slots = {
        1: witness._encode_slot(_ws_for(1, ws), now_ms),
        2: witness._encode_slot(_ws_for(2, ws), now_ms),
        9: witness._encode_slot(_ws_for(9, ws), now_ms),  # decommissioned
    }
    mp = witness._msgpack()
    env = mp.packb({"v": 1, "t": "ack", "cu": ws.cluster_uuid,
                    "echo_id": "e1", "slots": slots}, use_bin_type=True)
    pkt = witness.MAGIC + witness._aead_seal(ws.cluster_key, env)
    tx.sendto(pkt, rx.getsockname())

    witness.drain_replies(ws)
    assert set(ws.slots.keys()) == {1, 2}  # node 9 filtered out
    rx.close()
    tx.close()


def _ws_for(node_id, base):
    """A throwaway WitnessState that writes slot for `node_id` with the
    same cluster key, so _encode_slot produces a peer's slot blob."""
    return witness.WitnessState(
        cluster_uuid=base.cluster_uuid, cluster_key=base.cluster_key,
        my_node_id=node_id, own_marker=b"abc",
    )


# ────────────────────────────────────────────────────────────────────
#  state — 7-day arbiter-UUID history + eligibility classification
# ────────────────────────────────────────────────────────────────────

def test_unseen_uuid_is_votable():
    st = {"node_name": "sim-1", "arbiter_uuid_history": []}
    assert lstate.classify_arbiter_uuid("deadbeef", st) == lstate.UUID_UNSEEN
    assert lstate.is_uuid_eligible("deadbeef", st) is True


def test_current_uuid_is_votable():
    now = time.time()
    st = {"node_name": "sim-1",
          "arbiter_uuid_history": [
              {"uuid": "aaaa", "ts_seen": now - 100, "ts_superseded": now - 50},
              {"uuid": "bbbb", "ts_seen": now - 50, "ts_superseded": None},
          ]}
    assert lstate.classify_arbiter_uuid("bbbb", st, now=now) == lstate.UUID_CURRENT
    assert lstate.is_uuid_eligible("bbbb", st, now=now) is True


def test_superseded_uuid_is_refused():
    now = time.time()
    st = {"node_name": "sim-1",
          "arbiter_uuid_history": [
              {"uuid": "aaaa", "ts_seen": now - 100, "ts_superseded": now - 50},
              {"uuid": "bbbb", "ts_seen": now - 50, "ts_superseded": None},
          ]}
    # 'aaaa' was superseded by 'bbbb' → a candidate advertising it is stale.
    assert lstate.classify_arbiter_uuid("aaaa", st, now=now) == lstate.UUID_SUPERSEDED
    assert lstate.is_uuid_eligible("aaaa", st, now=now) is False


def test_uuid_classification_normalizes_format():
    now = time.time()
    st = {"node_name": "sim-1",
          "arbiter_uuid_history": [
              {"uuid": "deadbeef", "ts_seen": now, "ts_superseded": None}]}
    # 0x prefix, trailing semicolon, upper case all normalize to the
    # stored bare lower-case hex.
    assert lstate.classify_arbiter_uuid("0xDEADBEEF;", st, now=now) == lstate.UUID_CURRENT


def test_record_supersedes_previous_and_prunes_7_days(monkeypatch):
    # record_arbiter_uuid persists, so capture the saved dict instead of
    # touching /etc/bedrock.
    saved = {}

    def fake_save(state):
        saved.clear()
        saved.update(state)

    monkeypatch.setattr(lstate, "save", fake_save)

    now = 1_000_000.0
    st = {"node_name": "sim-1", "arbiter_uuid_history": []}
    st = lstate.record_arbiter_uuid("0xAAAA", st, now=now)
    assert st["arbiter_uuid_history"][-1]["uuid"] == "aaaa"
    assert st["arbiter_uuid_history"][-1]["ts_superseded"] is None

    # A new UUID supersedes the previous.
    later = now + 60
    st = lstate.record_arbiter_uuid("bbbb", st, now=later)
    hist = st["arbiter_uuid_history"]
    assert [e["uuid"] for e in hist] == ["aaaa", "bbbb"]
    assert hist[0]["ts_superseded"] == later
    assert hist[1]["ts_superseded"] is None

    # Recording the same current UUID again is a no-op.
    st = lstate.record_arbiter_uuid("bbbb", st, now=later + 1)
    assert len(st["arbiter_uuid_history"]) == 2

    # A third UUID far in the future prunes the now->8-day-old 'aaaa'
    # but keeps 'bbbb' (superseded but within window) and 'cccc' (newest).
    much_later = later + lstate.UUID_HISTORY_RETENTION_S + 10
    st = lstate.record_arbiter_uuid("cccc", st, now=much_later)
    remaining = [e["uuid"] for e in st["arbiter_uuid_history"]]
    assert "aaaa" not in remaining
    assert remaining[-1] == "cccc"


def test_record_blank_uuid_is_noop():
    st = {"node_name": "sim-1", "arbiter_uuid_history": []}
    out = lstate.record_arbiter_uuid("", st, now=1.0)
    assert out["arbiter_uuid_history"] == []


def test_believed_master_roundtrip(monkeypatch):
    saved = {}
    monkeypatch.setattr(lstate, "save", lambda s: saved.update(s))
    st = {"node_name": "sim-1"}
    lstate.set_believed_master("sim-3", st)
    assert saved["believed_master"] == "sim-3"
    assert lstate.get_believed_master(saved) == "sim-3"
    lstate.set_believed_master(None, saved)
    assert lstate.get_believed_master(saved) is None


# ─────────────────────────────────────────────────────────────────
#  Casting vote (2-node witness-loss rescue) — the split-brain proof.
#  Scenario: 2 nodes, the only witness is gone (n_configured_witnesses=0 →
#  total=200, majority=101), the two nodes are PARTITIONED from each other,
#  and the saga has armed casting_vote_node = the master. The master must STAY
#  (101), the follower must HALT (100). The +1 is credited ONLY in the
#  steady-state-master branch — never promote/follower — which is the proof.
# ─────────────────────────────────────────────────────────────────

def test_casting_vote_keeps_partitioned_master_sticky():
    # master sim-1, peer sim-2 unreachable, no witness, casting armed to sim-1.
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={"sim-2": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        casting_vote_node="sim-1",
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.total_votes == 200 and r.majority == 101
    assert r.my_votes == 101                      # 100 self + 1 casting
    assert r.outcome is election.Outcome.LEADER


def test_without_casting_partitioned_master_loses_quorum():
    r = election.compute(
        self_name="sim-1", self_loopback="100.64.0.1",
        peer_liveness={"sim-2": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        casting_vote_node=None,                   # not armed
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.my_votes == 100
    assert r.outcome is election.Outcome.NO_QUORUM


def test_GUARD_partitioned_follower_never_credits_casting():
    # THE load-bearing guard: the follower sees the master (sim-1) gone, and the
    # casting vote is armed to that peer. It must compute EXACTLY 100 → NoQuorum,
    # NEVER borrow the casting +1 to promote. (promote branch, casting==peer.)
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        casting_vote_node="sim-1",
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.my_votes == 100
    assert r.outcome is election.Outcome.NO_QUORUM


def test_GUARD_casting_never_helps_a_promoting_candidate_even_if_self():
    # Even if casting were (mis)armed to the promoting node itself, the promote
    # branch must NOT credit it — casting rescues only the STEADY-STATE master.
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": False}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",            # master gone → promote branch
        n_configured_witnesses=0, n_valid_witnesses=0,
        casting_vote_node="sim-2",              # armed to self, but promoting
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.my_votes == 100                    # NOT 101
    assert r.outcome is election.Outcome.NO_QUORUM


def test_casting_not_credited_when_following_a_live_master():
    # follower with a LIVE master: casting must not inflate the follower's tally.
    r = election.compute(
        self_name="sim-2", self_loopback="100.64.0.2",
        peer_liveness={"sim-1": True}, node_loopbacks=_loops("sim-1", "sim-2"),
        current_mgmt_master="sim-1",
        n_configured_witnesses=0, n_valid_witnesses=0,
        casting_vote_node="sim-1",
        no_quorum_marker_path=NO_MARKER,
    )
    assert r.outcome is election.Outcome.FOLLOWER
    assert r.my_votes == 200                    # 100 self + 100 reachable peer; no casting term
