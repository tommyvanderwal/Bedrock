"""Unit tests for the self-heal repair planner + the VM-01 fixes.

Covers the load-bearing pure logic that must never act unsafely:
- the 80% disk gate (fits_under_gate / pick_target)
- compute_repair_plan ordering: singleton first, then pets, then
  vipets, high→normal→low; "degraded" when nothing fits
- replica_repair node-id helpers (stable ids + smallest free id)
- VM-01: drop_suspended removes resumed VMs; the kill task re-checks
  domstate and refuses to destroy a VM that is no longer paused
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

from bedrock_d.orchestrator import self_heal as sh  # noqa: E402
from bedrock_d.orchestrator import replica_repair as rr  # noqa: E402
from bedrock_d.orchestrator import vm_failover as vmf  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# Disk gate
# ─────────────────────────────────────────────────────────────────────

GB = 1024 ** 3


def test_fits_under_gate_basic():
    # 70% used, adding 5% → 75% ≤ 80% → fits
    assert sh.fits_under_gate(70 * GB, 100 * GB, 5 * GB)
    # 78% used, adding 5% → 83% > 80% → refuse
    assert not sh.fits_under_gate(78 * GB, 100 * GB, 5 * GB)
    # exactly 80% → still fits (≤, not <)
    assert sh.fits_under_gate(75 * GB, 100 * GB, 5 * GB)


def test_fits_under_gate_unknown_capacity_refuses():
    # total<=0 means we couldn't read the node's disk → never fits
    assert not sh.fits_under_gate(0, 0, 1 * GB)


def test_pick_target_skips_held_and_full_nodes():
    usage = {
        "n2": {"used_bytes": 79 * GB, "total_bytes": 100 * GB},  # too full
        "n3": {"used_bytes": 10 * GB, "total_bytes": 100 * GB},  # fits
    }
    tgt = sh.pick_target(current_peers=["n1"], candidates=["n1", "n2", "n3"],
                         add_bytes=5 * GB, usage=usage)
    assert tgt == "n3"   # n1 held, n2 too full, n3 fits


def test_pick_target_none_when_nothing_fits():
    usage = {"n2": {"used_bytes": 79 * GB, "total_bytes": 100 * GB}}
    assert sh.pick_target(current_peers=["n1"], candidates=["n1", "n2"],
                          add_bytes=5 * GB, usage=usage) is None


# ─────────────────────────────────────────────────────────────────────
# compute_repair_plan ordering + safety
# ─────────────────────────────────────────────────────────────────────


def _roomy_usage(nodes):
    return {n: {"used_bytes": 1 * GB, "total_bytes": 100 * GB} for n in nodes}


def test_singleton_repaired_first():
    # singleton is missing a peer AND a pet is single — singleton wins.
    plan = sh.compute_repair_plan(
        active_nodes=["n1", "n2", "n3"], lost_nodes=["n4"],
        singleton_peers=["n1", "n2"],           # 2-way, want 3
        vm_resources=[{"resource": "vm-a-disk0", "vm_name": "a",
                       "vm_type": "pet", "priority": "high",
                       "peers": ["n1"]}],        # single
        usage=_roomy_usage(["n1", "n2", "n3"]),
        resource_sizes={"cluster": 1 * GB, "vm-a-disk0": 5 * GB})
    assert plan["action"] == "repair"
    assert plan["resource"] == "cluster"
    assert plan["kind"] == "singleton"
    assert plan["target"] == "n3"


def test_pets_before_vipets_then_priority_order():
    # singleton already healthy; pets repaired before vipets, and among
    # pets high before normal before low.
    res = [
        {"resource": "vm-vip-disk0", "vm_name": "vip", "vm_type": "vipet",
         "priority": "high", "peers": ["n1", "n2"]},          # 2-way, want 3
        {"resource": "vm-low-disk0", "vm_name": "low", "vm_type": "pet",
         "priority": "low", "peers": ["n1"]},                 # single
        {"resource": "vm-hi-disk0", "vm_name": "hi", "vm_type": "pet",
         "priority": "high", "peers": ["n1"]},                # single
    ]
    plan = sh.compute_repair_plan(
        active_nodes=["n1", "n2", "n3"], lost_nodes=["n4"],
        singleton_peers=["n1", "n2", "n3"],
        vm_resources=res, usage=_roomy_usage(["n1", "n2", "n3"]),
        resource_sizes={r["resource"]: 1 * GB for r in res})
    # The high-priority PET is repaired first (pets before vipets).
    assert plan["action"] == "repair"
    assert plan["resource"] == "vm-hi-disk0"


def test_nothing_to_do_when_all_whole():
    plan = sh.compute_repair_plan(
        active_nodes=["n1", "n2", "n3"], lost_nodes=[],
        singleton_peers=["n1", "n2", "n3"],
        vm_resources=[{"resource": "vm-a-disk0", "vm_name": "a",
                       "vm_type": "pet", "priority": "normal",
                       "peers": ["n1", "n2"]}],
        usage=_roomy_usage(["n1", "n2", "n3"]),
        resource_sizes={})
    assert plan["action"] == "none"


def test_degraded_when_no_node_fits():
    # A pet is single, but the only candidate is too full → degraded,
    # never an unsafe placement.
    plan = sh.compute_repair_plan(
        active_nodes=["n1", "n2"], lost_nodes=["n3"],
        singleton_peers=["n1", "n2"],            # already at min(3, 2)=2
        vm_resources=[{"resource": "vm-a-disk0", "vm_name": "a",
                       "vm_type": "pet", "priority": "high",
                       "peers": ["n1"]}],
        usage={"n2": {"used_bytes": 79 * GB, "total_bytes": 100 * GB}},
        resource_sizes={"vm-a-disk0": 5 * GB})
    assert plan["action"] == "degraded"
    assert plan["resource"] == "vm-a-disk0"


def test_singleton_target_capped_by_cluster_size():
    # Only 2 active nodes: a 2-way singleton is already at min(3,2)=2,
    # so it is NOT flagged for repair.
    plan = sh.compute_repair_plan(
        active_nodes=["n1", "n2"], lost_nodes=["n3"],
        singleton_peers=["n1", "n2"],
        vm_resources=[],
        usage=_roomy_usage(["n1", "n2"]), resource_sizes={})
    assert plan["action"] == "none"


# ─────────────────────────────────────────────────────────────────────
# replica_repair node-id helpers
# ─────────────────────────────────────────────────────────────────────

_RES = """resource vm-a-disk0 {
    on n1 { device /dev/drbd1102 minor 1102; node-id 0; }
    on n2 { device /dev/drbd1102 minor 1102; node-id 1; }
}
"""


def test_parse_node_ids():
    ids = rr.parse_node_ids(_RES)
    assert ids == {"n1": 0, "n2": 1}


def test_next_free_node_id_is_smallest_gap():
    assert rr.next_free_node_id({"n1": 0, "n2": 1}) == 2
    assert rr.next_free_node_id({"n1": 1, "n2": 2}) == 0   # 0 is free
    assert rr.next_free_node_id({}) == 0


def test_next_free_node_id_exhausted_raises():
    full = {f"n{i}": i for i in range(7)}
    with pytest.raises(RuntimeError):
        rr.next_free_node_id(full, max_peers=7)


def test_vm_name_from_resource():
    assert sh._vm_name_from_resource("vm-web1-disk0") == "web1"
    assert sh._vm_name_from_resource("vm-web1-disk3") == "web1"
    assert sh._vm_name_from_resource("cluster") == ""


# ─────────────────────────────────────────────────────────────────────
# VM-01: resumed VM must not be killed at T+5min
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def suspended_file(tmp_path, monkeypatch):
    f = tmp_path / "suspended-vms.json"
    monkeypatch.setattr(vmf, "SUSPENDED_VMS_FILE", f)
    return f


def test_drop_suspended_removes_resumed_vm(suspended_file):
    suspended_file.write_text(json.dumps({"vm-a": 100.0, "vm-b": 100.0}))
    vmf.drop_suspended(["vm-a"])
    assert json.loads(suspended_file.read_text()) == {"vm-b": 100.0}


def test_drop_suspended_noop_for_unknown(suspended_file):
    suspended_file.write_text(json.dumps({"vm-a": 100.0}))
    vmf.drop_suspended(["not-there"])
    assert json.loads(suspended_file.read_text()) == {"vm-a": 100.0}


def test_kill_task_skips_recovered_vm(suspended_file, monkeypatch):
    """A VM whose record entry is stale-old but is actually RUNNING
    again (resume missed the record) must be dropped, not destroyed."""
    import asyncio

    suspended_file.write_text(json.dumps({"vm-running": 0.0,
                                          "vm-dead": 0.0}))
    destroyed: list[str] = []

    def fake_virsh(*args, timeout=30.0):
        if args[0] == "domstate":
            vm = args[1]
            return (0, "running" if vm == "vm-running" else "paused", "")
        if args[0] == "destroy":
            destroyed.append(args[1])
            return (0, "", "")
        return (1, "", "unexpected")

    monkeypatch.setattr(vmf, "_virsh", fake_virsh)
    monkeypatch.setattr(vmf, "KILL_AFTER_QUORUM_LOSS_S", 1)
    monkeypatch.setattr(vmf, "TICK_S", 0.01)

    async def run_one_tick():
        task = asyncio.ensure_future(vmf.kill_suspended_after_5min_task())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run_one_tick())

    # The recovered VM was NOT destroyed; the genuinely-suspended one was.
    assert "vm-running" not in destroyed
    assert "vm-dead" in destroyed
    # Both entries are gone from the record afterwards.
    assert not suspended_file.exists() or json.loads(
        suspended_file.read_text()) == {}
