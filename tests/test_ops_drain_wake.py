"""Step 4 regression: the central loop nudges operations_drain on change.

_cluster_change (the dispatcher the central loop fires per revision advance)
must set _OPS_WAKE after the reactor dispatch, so a node-dispatched saga op
(vm_backup / vm_restore) the leader just queued for this node runs near-instant
instead of waiting out operations_drain's poll floor. The drain keeps its floor
as the backstop; this only locks in the fast-path wiring.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

import mgmt.orchestrator as orch  # noqa: E402


def test_cluster_change_sets_ops_wake():
    async def go():
        orch._OPS_WAKE = asyncio.Event()
        # Stub the heavy reactor dispatch — we only assert the nudge fires.
        orig = orch._apply_at_level
        orch._apply_at_level = lambda rev, level: None
        try:
            assert not orch._OPS_WAKE.is_set()
            await orch._cluster_change(5, "weak")
            assert orch._OPS_WAKE.is_set(), \
                "_cluster_change must nudge operations_drain (_OPS_WAKE)"
        finally:
            orch._apply_at_level = orig

    asyncio.run(go())


def test_cluster_change_safe_when_wake_absent():
    # Before start_all creates _OPS_WAKE, a change must not crash — it just
    # skips the nudge (the drain's poll floor backstops).
    async def go():
        orch._OPS_WAKE = None
        orig = orch._apply_at_level
        orch._apply_at_level = lambda rev, level: None
        try:
            await orch._cluster_change(7, "weak")  # must not raise
        finally:
            orch._apply_at_level = orig

    asyncio.run(go())


if __name__ == "__main__":
    test_cluster_change_sets_ops_wake()
    test_cluster_change_safe_when_wake_absent()
    print("✓ ops-drain wake wiring tests passed")
