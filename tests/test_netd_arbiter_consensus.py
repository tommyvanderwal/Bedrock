"""Unit tests for the BAD-1 consensus core (Phase 3).

Covers the netd election heartbeat + the cluster_arbiter takeover
decision matrix:

  * lib.netd.encode/decode_heartbeat — the protocol-4 election heartbeat
    codec (believed-master / transitioning / arbiter-UUID / ack-target),
    including tamper rejection.
  * lib.netd._failover_ack_target — the UUID-eligibility-gated vote: a
    superseded candidate is refused (split-brain guard), the lowest-octet
    eligible contender wins.
  * lib.cluster_arbiter._cold_boot_uuid_ok — the cold-boot DRBD-UUID-vs-
    own-slot check (refuse a stale local generation).
  * lib.cluster_arbiter._run_takeover_protocol — the failover decision
    matrix: stale+lms0 (proceed), stale+lms1 (refuse), fresh (refuse),
    cold-boot-older (refuse).

Pure functions / mocked side-effects; no live rqlite / witness / DRBD.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

from lib import netd            # noqa: E402
from lib import cluster_arbiter as ca  # noqa: E402
from lib import witness         # noqa: E402
from lib import state as lstate  # noqa: E402


KEY = b"k" * 32
CU = "u" * 16


# ────────────────────────────────────────────────────────────────────
#  election heartbeat codec (protocol 4)
# ────────────────────────────────────────────────────────────────────

def test_heartbeat_roundtrip_carries_all_fields():
    buf = netd.encode_heartbeat(
        cluster_uuid=CU, node="sim-2", ts=12.5,
        believed_master="sim-1", transitioning=True,
        arbiter_uuid="deadbeef", ack_target="sim-2", key=KEY,
    )
    body = netd.decode_heartbeat(buf, key=KEY)
    assert body["node"] == "sim-2"
    assert body["believed_master"] == "sim-1"
    assert body["transitioning"] is True
    assert body["arbiter_uuid"] == "deadbeef"
    assert body["ack_target"] == "sim-2"


def test_heartbeat_rejected_under_wrong_key():
    buf = netd.encode_heartbeat(
        cluster_uuid=CU, node="sim-2", ts=1.0, believed_master="",
        transitioning=False, arbiter_uuid="", ack_target="", key=KEY,
    )
    assert netd.decode_heartbeat(buf, key=b"x" * 32) is None


def test_heartbeat_rejected_on_tamper():
    buf = bytearray(netd.encode_heartbeat(
        cluster_uuid=CU, node="sim-2", ts=1.0, believed_master="",
        transitioning=False, arbiter_uuid="", ack_target="", key=KEY,
    ))
    buf[-1] ^= 0xFF  # flip a byte
    assert netd.decode_heartbeat(bytes(buf), key=KEY) is None


# ────────────────────────────────────────────────────────────────────
#  _failover_ack_target — UUID-eligibility-gated vote
# ────────────────────────────────────────────────────────────────────

def _daemon(my_node, my_loopback, peer_hb=None, arbiter_uuid=""):
    d = netd.Daemon(cluster_key=KEY, cluster_uuid=CU,
                    my_node=my_node, my_loopback=my_loopback)
    d.peer_hb = peer_hb or {}
    d.hb_arbiter_uuid = arbiter_uuid
    return d


def _hb(transitioning, arbiter_uuid, seen=None):
    return {"transitioning": transitioning, "arbiter_uuid": arbiter_uuid,
            "believed_master": "", "ack_target": "",
            "seen_at_monotonic": seen if seen is not None else time.monotonic()}


def test_ack_target_picks_lowest_octet_eligible_contender():
    loops = {"sim-2": "100.64.0.2", "sim-3": "100.64.0.3"}
    # self sim-3 (octet 3), peer sim-2 (octet 2) is transitioning + eligible.
    d = _daemon("sim-3", "100.64.0.3",
                peer_hb={"sim-2": _hb(True, "abcd")}, arbiter_uuid="ef01")
    liveness = {"sim-2": True}
    with mock.patch.object(lstate, "is_uuid_eligible", return_value=True):
        assert netd._failover_ack_target(d, loops, liveness) == "sim-2"


def test_ack_target_skips_superseded_candidate():
    loops = {"sim-2": "100.64.0.2", "sim-3": "100.64.0.3"}
    d = _daemon("sim-3", "100.64.0.3",
                peer_hb={"sim-2": _hb(True, "stale")}, arbiter_uuid="fresh")
    liveness = {"sim-2": True}
    # sim-2's UUID is superseded (refused) → fall through to self sim-3.
    def elig(uuid, *a, **k):
        return uuid != "stale"
    with mock.patch.object(lstate, "is_uuid_eligible", side_effect=elig):
        assert netd._failover_ack_target(d, loops, liveness) == "sim-3"


def test_ack_target_abstains_when_no_candidate_eligible():
    loops = {"sim-3": "100.64.0.3"}
    d = _daemon("sim-3", "100.64.0.3", peer_hb={}, arbiter_uuid="stale")
    with mock.patch.object(lstate, "is_uuid_eligible", return_value=False):
        assert netd._failover_ack_target(d, loops, {}) == ""


def test_ack_target_ignores_non_transitioning_peers():
    loops = {"sim-2": "100.64.0.2", "sim-3": "100.64.0.3"}
    # sim-2 is reachable but NOT transitioning → not a candidate; self wins.
    d = _daemon("sim-3", "100.64.0.3",
                peer_hb={"sim-2": _hb(False, "abcd")}, arbiter_uuid="ef01")
    with mock.patch.object(lstate, "is_uuid_eligible", return_value=True):
        assert netd._failover_ack_target(d, loops, {"sim-2": True}) == "sim-3"


# ────────────────────────────────────────────────────────────────────
#  _cold_boot_uuid_ok — cold-boot DRBD-UUID-vs-own-slot check
# ────────────────────────────────────────────────────────────────────

def _ws(my_id=1):
    return witness.WitnessState(cluster_uuid=CU, cluster_key=KEY,
                                my_node_id=my_id)


def _own_slot(marker):
    return witness.Slot(node_id=1, ts_writer_ms=int(time.time() * 1000),
                        tag=0, marker=marker)


def test_cold_boot_ok_when_no_own_slot():
    ws = _ws()
    with mock.patch.object(witness, "own_slot", return_value=None):
        assert ca._cold_boot_uuid_ok(ws, witness) is True


def test_cold_boot_ok_when_marker_matches_local():
    ws = _ws()
    with mock.patch.object(witness, "own_slot",
                           return_value=_own_slot(b"abcd")), \
         mock.patch.object(ca, "_read_local_drbd_uuid", return_value="abcd"):
        assert ca._cold_boot_uuid_ok(ws, witness) is True


def test_cold_boot_refuses_when_local_is_superseded():
    # Witness holds our old slot marker 'newgen'; local DRBD is 'oldgen'
    # which our own history classifies SUPERSEDED → cluster advanced
    # without us → refuse.
    ws = _ws()
    with mock.patch.object(witness, "own_slot",
                           return_value=_own_slot(b"newgen")), \
         mock.patch.object(ca, "_read_local_drbd_uuid", return_value="oldgen"), \
         mock.patch.object(lstate, "classify_arbiter_uuid",
                           return_value=lstate.UUID_SUPERSEDED):
        assert ca._cold_boot_uuid_ok(ws, witness) is False


def test_cold_boot_ok_when_local_unseen_despite_marker_mismatch():
    # Markers differ but local is UNSEEN in our history (not provably
    # stale) → allow the legitimate first promote.
    ws = _ws()
    with mock.patch.object(witness, "own_slot",
                           return_value=_own_slot(b"newgen")), \
         mock.patch.object(ca, "_read_local_drbd_uuid", return_value="other"), \
         mock.patch.object(lstate, "classify_arbiter_uuid",
                           return_value=lstate.UUID_UNSEEN):
        assert ca._cold_boot_uuid_ok(ws, witness) is True


# ────────────────────────────────────────────────────────────────────
#  _run_takeover_protocol — failover decision matrix
# ────────────────────────────────────────────────────────────────────

def _shared_state(my_id=2):
    st = types.SimpleNamespace()
    st.netd_ws = witness.WitnessState(cluster_uuid=CU, cluster_key=KEY,
                                      my_node_id=my_id)
    return st


def _slot(marker, *, stale, lms):
    ts = int(time.time() * 1000)
    if stale:
        ts -= witness.SLOT_STALE_MS + 1000
    tag = witness.TAG_LMS if lms else 0
    return witness.Slot(node_id=1, ts_writer_ms=ts, tag=tag, marker=marker)


@pytest.fixture
def takeover_env(monkeypatch):
    """Wire _run_takeover_protocol so the last master is node 1 (a peer),
    the witness is alive, and the cluster is N=2. Tests override the
    witness slot + local UUID per case."""
    monkeypatch.setattr(ca, "SHARED_STATE", _shared_state(my_id=2))
    monkeypatch.setattr(ca, "_last_known_master_node_id", lambda: 1)
    monkeypatch.setattr(ca, "_cluster_size", lambda: 2)
    monkeypatch.setattr(witness, "is_alive", lambda ws: True)
    return monkeypatch


def test_takeover_stale_lms0_proceeds(takeover_env):
    # Master slot stale, lms=0, local DRBD UUID == slot marker, and the
    # own-slot readback reflects lms=1 → proceed.
    takeover_env.setattr(witness, "read_slot",
                         lambda ws, nid: _slot(b"gen1", stale=True, lms=False))
    takeover_env.setattr(ca, "_read_local_drbd_uuid", lambda: "gen1")
    takeover_env.setattr(witness, "set_own_slot",
                         lambda ws, **k: None)
    takeover_env.setattr(witness, "own_slot",
                         lambda ws: witness.Slot(node_id=2,
                            ts_writer_ms=int(time.time() * 1000),
                            tag=witness.TAG_LMS, marker=b"gen1"))
    takeover_env.setattr(time, "sleep", lambda *_: None)  # no real waits
    assert ca._run_takeover_protocol() is True


def test_takeover_stale_lms1_refuses(takeover_env):
    # Stale but lms=1 — previous master died holding LMS; never times out.
    takeover_env.setattr(witness, "read_slot",
                         lambda ws, nid: _slot(b"gen1", stale=True, lms=True))
    assert ca._run_takeover_protocol() is False


def test_takeover_fresh_master_slot_refuses(takeover_env):
    # Master slot is FRESH → cluster healthy elsewhere → do not take over.
    takeover_env.setattr(witness, "read_slot",
                         lambda ws, nid: _slot(b"gen1", stale=False, lms=False))
    assert ca._run_takeover_protocol() is False


def test_takeover_missing_slot_refuses(takeover_env):
    # Missing slot = worst case assumed (could have held lms=1) → refuse.
    takeover_env.setattr(witness, "read_slot", lambda ws, nid: None)
    assert ca._run_takeover_protocol() is False


def test_takeover_drbd_divergence_refuses(takeover_env):
    # Stale + lms=0 but local DRBD UUID != slot marker → divergence, refuse.
    takeover_env.setattr(witness, "read_slot",
                         lambda ws, nid: _slot(b"gen1", stale=True, lms=False))
    takeover_env.setattr(ca, "_read_local_drbd_uuid", lambda: "gen2")
    assert ca._run_takeover_protocol() is False


# ────────────────────────────────────────────────────────────────────
#  C2/M12 — steal-back guard (peer-claims-master cross-check)
# ────────────────────────────────────────────────────────────────────

def _state_with_peer_hb(my_id, peer_hb):
    """A SHARED_STATE whose .netd.peer_hb is the given dict + a netd_ws."""
    st = types.SimpleNamespace()
    st.netd = types.SimpleNamespace(peer_hb=peer_hb)
    st.netd_ws = witness.WitnessState(cluster_uuid=CU, cluster_key=KEY,
                                      my_node_id=my_id, my_node_name="sim-2")
    return st


def _claim_hb(believed_master, seen=None):
    return {"believed_master": believed_master, "transitioning": False,
            "arbiter_uuid": "", "ack_target": "",
            "seen_at_monotonic": seen if seen is not None else time.monotonic()}


def test_peer_claims_master_now_detects_live_master():
    st = _state_with_peer_hb(2, {"sim-1": _claim_hb("sim-1")})
    with mock.patch.object(ca, "SHARED_STATE", st):
        assert ca._peer_claims_master_now(st.netd_ws) == "sim-1"


def test_peer_claims_master_now_ignores_stale_heartbeat():
    stale = time.monotonic() - ca.PEER_HB_FRESH_S - 1
    st = _state_with_peer_hb(2, {"sim-1": _claim_hb("sim-1", seen=stale)})
    with mock.patch.object(ca, "SHARED_STATE", st):
        assert ca._peer_claims_master_now(st.netd_ws) is None


def test_peer_claims_master_now_ignores_peer_following_someone_else():
    # Peer is fresh but believes a THIRD node is master (not itself) →
    # it's not claiming the role, so no steal-back deferral.
    st = _state_with_peer_hb(2, {"sim-1": _claim_hb("sim-3")})
    with mock.patch.object(ca, "SHARED_STATE", st):
        assert ca._peer_claims_master_now(st.netd_ws) is None


def test_takeover_defers_when_peer_claims_master(monkeypatch):
    # C2/M12: returning old master (last_master_id == my_id) would hit
    # the fast path and steal the role; the steal-back guard refuses
    # because the live survivor sim-1 is freshly claiming master.
    st = _state_with_peer_hb(2, {"sim-1": _claim_hb("sim-1")})
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    monkeypatch.setattr(ca, "_last_known_master_node_id", lambda: 2)  # == my_id
    assert ca._run_takeover_protocol() is False


def test_takeover_first_promote_proceeds_when_no_peer_claims(monkeypatch):
    # Legitimate first-takeover: no peer claims master → guard lets the
    # fast path proceed (no prior master, cold-boot ok, N=1 no patience).
    st = _state_with_peer_hb(2, {})  # no peers claiming
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    monkeypatch.setattr(ca, "_last_known_master_node_id", lambda: None)
    monkeypatch.setattr(ca, "_cluster_size", lambda: 1)
    monkeypatch.setattr(ca, "_cold_boot_uuid_ok", lambda ws, w: True)
    assert ca._run_takeover_protocol() is True


# ────────────────────────────────────────────────────────────────────
#  H6 — ensure_lms_if_last_standing (LMS Scenario B)
# ────────────────────────────────────────────────────────────────────

def test_ensure_lms_sets_bit_when_alone_and_hosting(monkeypatch):
    # Leader, hosting (N=1: ip_present), no reachable peer, witness
    # valid+confirmed, own slot lms=0 → set LMS + readback confirms.
    st = _state_with_peer_hb(2, {})  # no fresh peer
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    monkeypatch.setattr(ca, "_drbd_resource_exists", lambda: False)  # N=1
    monkeypatch.setattr(ca, "arbiter_status",
                        lambda: {"ip_present": True, "service_active": False,
                                 "drbd_role": "Unknown"})
    monkeypatch.setattr(witness, "is_valid", lambda ws: True)
    monkeypatch.setattr(witness, "is_confirmed", lambda ws: True)
    monkeypatch.setattr(ca, "_read_local_drbd_uuid", lambda: "gen1")
    sets = {}

    def fake_set(ws, *, marker, tag, **k):
        sets["marker"], sets["tag"] = marker, tag

    monkeypatch.setattr(witness, "set_own_slot", fake_set)
    # own_slot: first None (lms=0 → must set), then reflects lms=1.
    seq = [None, witness.Slot(node_id=2, ts_writer_ms=int(time.time() * 1000),
                              tag=witness.TAG_LMS, marker=b"gen1")]
    monkeypatch.setattr(witness, "own_slot", lambda ws: seq.pop(0) if seq else seq[-1])
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    assert ca.ensure_lms_if_last_standing(st.netd_ws) is True
    assert sets["tag"] == witness.TAG_LMS
    assert sets["marker"] == b"gen1"


def test_ensure_lms_noop_when_peer_reachable(monkeypatch):
    # A fresh peer heartbeat exists → we are NOT last-standing → never set LMS.
    st = _state_with_peer_hb(2, {"sim-1": _claim_hb("sim-1")})
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    called = {"set": False}
    monkeypatch.setattr(witness, "set_own_slot",
                        lambda *a, **k: called.__setitem__("set", True))
    assert ca.ensure_lms_if_last_standing(st.netd_ws) is False
    assert called["set"] is False


def test_ensure_lms_noop_when_not_hosting(monkeypatch):
    # Alone but not actually hosting the arbiter → don't claim LMS.
    st = _state_with_peer_hb(2, {})
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    monkeypatch.setattr(ca, "_drbd_resource_exists", lambda: True)
    monkeypatch.setattr(ca, "arbiter_status",
                        lambda: {"ip_present": False, "service_active": False,
                                 "drbd_role": "Secondary"})
    called = {"set": False}
    monkeypatch.setattr(witness, "set_own_slot",
                        lambda *a, **k: called.__setitem__("set", True))
    assert ca.ensure_lms_if_last_standing(st.netd_ws) is False
    assert called["set"] is False


def test_ensure_lms_noop_when_already_lms(monkeypatch):
    # Already last-standing (own slot lms=1) → no flip needed.
    st = _state_with_peer_hb(2, {})
    monkeypatch.setattr(ca, "SHARED_STATE", st)
    monkeypatch.setattr(ca, "_drbd_resource_exists", lambda: False)
    monkeypatch.setattr(ca, "arbiter_status",
                        lambda: {"ip_present": True, "service_active": False,
                                 "drbd_role": "Unknown"})
    monkeypatch.setattr(witness, "is_valid", lambda ws: True)
    monkeypatch.setattr(witness, "is_confirmed", lambda ws: True)
    monkeypatch.setattr(witness, "own_slot",
                        lambda ws: witness.Slot(node_id=2,
                            ts_writer_ms=int(time.time() * 1000),
                            tag=witness.TAG_LMS, marker=b"gen1"))
    called = {"set": False}
    monkeypatch.setattr(witness, "set_own_slot",
                        lambda *a, **k: called.__setitem__("set", True))
    assert ca.ensure_lms_if_last_standing(st.netd_ws) is False
    assert called["set"] is False
