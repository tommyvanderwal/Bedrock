"""Fileshare witness backend (installer/lib/witness_file.py) — the slot
protocol over a shared directory. These cover the TRANSPORT module only
(read/write/decode + the validity reuse); netd integration is separate."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

import types                   # noqa: E402

from lib import witness        # noqa: E402
from lib import witness_file   # noqa: E402
from lib import netd           # noqa: E402


_KEY = b"k" * 32
_UUID = "u" * 16


def _ws(my_id, *, members=None, marker=b""):
    ws = witness.WitnessState(
        cluster_uuid=_UUID, cluster_key=_KEY,
        my_node_id=my_id, member_ids=members,
    )
    ws.own_marker = marker
    return ws


# ── write/read round trip ────────────────────────────────────────────────

def test_write_then_read_round_trip(tmp_path):
    """Two nodes write their own slot into the shared dir; either node reads
    BOTH slots back, decoded, with the right node_id + marker."""
    d = str(tmp_path)
    ws1 = _ws(1, members={1, 2}, marker=b"m1")
    ws2 = _ws(2, members={1, 2}, marker=b"m2")
    witness_file.write_own_slot(ws1, d)
    witness_file.write_own_slot(ws2, d)

    slots = witness_file.read_slots(ws2, d)
    assert set(slots) == {1, 2}
    assert slots[1].marker == b"m1"
    assert slots[2].marker == b"m2"
    assert slots[1].node_id == 1 and slots[2].node_id == 2


def test_write_is_atomic_no_tmp_left(tmp_path):
    """tmp+rename leaves exactly slot-<id>.bin and no .tmp turd."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1}, marker=b"m1"), d)
    names = os.listdir(d)
    assert names == [witness_file.slot_filename(1)]
    assert not any(".tmp" in n for n in names)


def test_write_overwrites_in_place(tmp_path):
    """A second write of the same node replaces its slot (new marker)."""
    d = str(tmp_path)
    ws = _ws(1, members={1}, marker=b"gen-a")
    witness_file.write_own_slot(ws, d)
    ws.own_marker = b"gen-b"
    witness_file.write_own_slot(ws, d)
    slots = witness_file.read_slots(ws, d)
    assert slots[1].marker == b"gen-b"
    assert os.listdir(d) == [witness_file.slot_filename(1)]


# ── read filtering / robustness ──────────────────────────────────────────

def test_read_drops_non_member_slot(tmp_path):
    """A slot for a node not in member_ids is dropped (INV-7: decommissioned
    node's stale slot must not count)."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1, 2}, marker=b"m1"), d)
    witness_file.write_own_slot(_ws(9, members={9}, marker=b"m9"), d)  # stray
    reader = _ws(1, members={1, 2})
    slots = witness_file.read_slots(reader, d)
    assert set(slots) == {1}          # node 9 filtered out


def test_read_with_no_member_set_keeps_all(tmp_path):
    """member_ids=None ⇒ no membership filter (early boot)."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1}, marker=b"m1"), d)
    witness_file.write_own_slot(_ws(7, members={7}, marker=b"m7"), d)
    slots = witness_file.read_slots(_ws(1, members=None), d)
    assert set(slots) == {1, 7}


def test_read_drops_undecryptable_blob(tmp_path):
    """A slot-*.bin that fails AEAD (wrong key / junk) is silently dropped,
    no crash — mirrors drain_replies."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1, 2}, marker=b"m1"), d)
    (tmp_path / "slot-2.bin").write_bytes(b"not a valid sealed slot")
    slots = witness_file.read_slots(_ws(1, members={1, 2}), d)
    assert set(slots) == {1}          # garbage slot-2 ignored


def test_read_ignores_non_slot_files(tmp_path):
    """Only slot-*.bin is parsed; tmp turds and other files are skipped."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1}, marker=b"m1"), d)
    (tmp_path / "README.txt").write_bytes(b"hello")
    (tmp_path / "slot-2.bin.tmp.999").write_bytes(b"partial")
    slots = witness_file.read_slots(_ws(1, members={1}), d)
    assert set(slots) == {1}


def test_read_missing_dir_returns_empty(tmp_path):
    """An unreachable/nonexistent share yields no slots, never raises."""
    gone = str(tmp_path / "does-not-exist")
    assert witness_file.read_slots(_ws(1, members={1}), gone) == {}


# ── is_valid_confirmed (reuses the Echo predicates) ──────────────────────

def test_valid_confirmed_when_all_members_and_own_readback(tmp_path):
    d = str(tmp_path)
    ws1 = _ws(1, members={1, 2}, marker=b"m1")
    ws2 = _ws(2, members={1, 2}, marker=b"m2")
    witness_file.write_own_slot(ws1, d)
    witness_file.write_own_slot(ws2, d)
    assert witness_file.is_valid_confirmed(ws2, d) is True


def test_not_valid_when_a_member_slot_missing(tmp_path):
    d = str(tmp_path)
    ws2 = _ws(2, members={1, 2}, marker=b"m2")
    witness_file.write_own_slot(ws2, d)         # node 1 never wrote
    assert witness_file.is_valid_confirmed(ws2, d) is False


def test_not_confirmed_when_own_marker_mismatches(tmp_path):
    """Our slot present but carrying a stale generation marker ⇒ not confirmed
    (the readback proof failed)."""
    d = str(tmp_path)
    ws1 = _ws(1, members={1, 2}, marker=b"m1")
    ws2 = _ws(2, members={1, 2}, marker=b"old-gen")
    witness_file.write_own_slot(ws1, d)
    witness_file.write_own_slot(ws2, d)
    ws2.own_marker = b"new-gen"                 # rotated AFTER writing
    assert witness_file.is_valid_confirmed(ws2, d) is False


def test_not_confirmed_when_own_slot_stale(tmp_path):
    """Our slot's ts_writer is ancient ⇒ stale ⇒ not confirmed."""
    d = str(tmp_path)
    ws1 = _ws(1, members={1, 2}, marker=b"m1")
    ws2 = _ws(2, members={1, 2}, marker=b"m2")
    witness_file.write_own_slot(ws1, d)
    old = int(time.time() * 1000) - 3_600_000   # 1h old
    witness_file.write_own_slot(ws2, d, now_ms=old)
    assert witness_file.is_valid_confirmed(ws2, d) is False


def test_valid_even_when_other_member_slot_is_stale(tmp_path):
    """Validity is presence-not-freshness for OTHER members; only our own
    readback must be fresh (matches the Echo is_valid contract)."""
    d = str(tmp_path)
    ws1 = _ws(1, members={1, 2}, marker=b"m1")
    ws2 = _ws(2, members={1, 2}, marker=b"m2")
    old = int(time.time() * 1000) - 3_600_000
    witness_file.write_own_slot(ws1, d, now_ms=old)   # peer slot ancient
    witness_file.write_own_slot(ws2, d)               # own slot fresh
    assert witness_file.is_valid_confirmed(ws2, d) is True


# ── run_io_cycle (the off-hot-path worker's per-pass logic) ──────────────

def test_run_io_cycle_populates_fresh_valid_verdict(tmp_path):
    """Peer wrote its slot; our cycle writes ours + reads back → fresh
    valid_confirmed verdict stamped with the given monotonic."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1, 2}, marker=b"m1"), d)
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.configured_file_witnesses = [("fs1", d)]
    witness_file.run_io_cycle(ws, now_mono=1000.0)
    v = ws.file_witnesses["fs1"]
    assert v.valid_confirmed is True
    assert v.evaluated_monotonic == 1000.0


def test_run_io_cycle_fresh_false_when_member_missing(tmp_path):
    """No peer slot → a real 'not certifying' → FRESH False (counted as 0 at
    once, not aged out)."""
    d = str(tmp_path)
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.configured_file_witnesses = [("fs1", d)]
    witness_file.run_io_cycle(ws, now_mono=1000.0)
    v = ws.file_witnesses["fs1"]
    assert v.valid_confirmed is False
    assert v.evaluated_monotonic == 1000.0


def test_run_io_cycle_io_error_leaves_prior_verdict_to_age_out(tmp_path):
    """A share that can't be written (dir gone) is logged + skipped; the prior
    verdict is untouched so it ages out over the freshness window rather than
    flipping on a single transient blip (Echo-equivalent)."""
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.file_witnesses["fs1"] = witness.FileWitnessVerdict("fs1", True, 500.0)
    ws.configured_file_witnesses = [("fs1", "/nonexistent/share/dir")]
    logged = []
    witness_file.run_io_cycle(ws, now_mono=1000.0, log=logged.append)
    assert ws.file_witnesses["fs1"].evaluated_monotonic == 500.0   # untouched
    assert ws.file_witnesses["fs1"].valid_confirmed is True
    assert logged and "fs1" in logged[0]                           # fail-loud


def test_run_io_cycle_prunes_unconfigured_verdict(tmp_path):
    """A verdict for a witness no longer configured is dropped at once."""
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.file_witnesses["old"] = witness.FileWitnessVerdict("old", True, 500.0)
    ws.configured_file_witnesses = []
    witness_file.run_io_cycle(ws, now_mono=1000.0)
    assert "old" not in ws.file_witnesses


def test_run_io_cycle_isolates_a_bad_witness_from_a_good_one(tmp_path):
    """One unreachable share must not stop a healthy one in the same pass."""
    good = str(tmp_path / "good")
    os.makedirs(good)
    witness_file.write_own_slot(_ws(1, members={1, 2}, marker=b"m1"), good)
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.configured_file_witnesses = [("bad", "/nonexistent/x"), ("good", good)]
    witness_file.run_io_cycle(ws, now_mono=1000.0, log=lambda m: None)
    assert "bad" not in ws.file_witnesses
    assert ws.file_witnesses["good"].valid_confirmed is True


# ── probe_writable (the add-time UX guard) ───────────────────────────────

def test_probe_writable_ok_for_writable_dir(tmp_path):
    assert witness_file.probe_writable(str(tmp_path)) == ""
    # and it left no probe turd behind
    assert os.listdir(str(tmp_path)) == []


def test_probe_writable_reason_for_missing_dir(tmp_path):
    reason = witness_file.probe_writable(str(tmp_path / "not-mounted"))
    assert reason and "directory" in reason


def test_probe_writable_reason_for_a_file_not_dir(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    reason = witness_file.probe_writable(str(f))
    assert reason and "directory" in reason


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses dir perms")
def test_probe_writable_reason_for_readonly_dir(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)
    try:
        reason = witness_file.probe_writable(str(ro))
        assert reason and "write failed" in reason
    finally:
        os.chmod(ro, 0o755)   # let pytest clean up


# ── netd._witness_file_worker (the background-thread body) ────────────────

def test_witness_file_worker_runs_real_cycle_then_stops(tmp_path):
    """The worker drives the REAL run_io_cycle and produces a verdict, then
    honours should_stop. Exercised inline (not in a Thread) for determinism."""
    d = str(tmp_path)
    witness_file.write_own_slot(_ws(1, members={1, 2}, marker=b"m1"), d)
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.configured_file_witnesses = [("fs1", d)]
    stop = {"v": False}

    def cycle_then_stop(w, **kw):
        witness_file.run_io_cycle(w, **kw)   # the real thing
        stop["v"] = True                     # one pass, then ask to stop

    netd._witness_file_worker(
        ws, types.SimpleNamespace(run_io_cycle=cycle_then_stop),
        lambda: stop["v"], interval=0.01)
    assert ws.file_witnesses["fs1"].valid_confirmed is True


def test_witness_file_worker_survives_a_cycle_exception(tmp_path):
    """A raise inside run_io_cycle must NOT kill the worker (fail-loud): it
    logs and loops again."""
    ws = _ws(2, members={1, 2}, marker=b"m2")
    ws.configured_file_witnesses = [("fs1", "/x")]
    calls = {"n": 0}
    stop = {"v": False}

    def flaky(w, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")       # first pass blows up
        stop["v"] = True                     # second pass: survived → stop

    netd._witness_file_worker(
        ws, types.SimpleNamespace(run_io_cycle=flaky),
        lambda: stop["v"], interval=0.001)
    assert calls["n"] >= 2                    # ran again after the exception
