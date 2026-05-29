"""Regression tests for the central event loop's reactor-dispatch isolation.

The central loop (bedrock_d/orchestrator/cluster_loop.py, ClusterStateSource)
detects rqlite changes and fires a dispatch (the reactor sequence) per advance.
Step 2b made that dispatch HANG-SAFE: a wedged reactor (a stuck drbdadm/virsh/
systemctl) must never freeze the loop itself — detection, CDC fan-out, and
leader re-probe have to keep running. These tests lock that in:

  - happy path: dispatches run in order and last_rev advances (no regression).
  - hang isolation: a dispatch that never returns does NOT freeze the loop; it
    keeps polling, and single-flight holds (no second dispatch piles on top).
  - recovery: once the hung dispatch finally clears, the loop resumes and
    converges to the LATEST revision (skipped intermediates lose nothing).

Plain asyncio.run() — the repo has no pytest-asyncio.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

from bedrock_d.orchestrator.cluster_loop import ClusterStateSource  # noqa: E402


def _drain(revs):
    """A _read_revision stand-in: yields the given revs then sticks on the last."""
    seq = list(revs)
    i = {"n": 0}

    def read():
        v = seq[min(i["n"], len(seq) - 1)]
        i["n"] += 1
        return v, "strong"

    return read


def test_happy_path_dispatches_in_order_and_advances():
    async def go():
        seen = []

        async def on_change(rev, level):
            seen.append(rev)

        src = ClusterStateSource(on_change, poll_interval_s=0.02,
                                 dispatch_timeout_s=1.0)
        src._read_revision = _drain([10, 11, 12])
        t = asyncio.create_task(src.run())
        await asyncio.sleep(0.3)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert seen[:3] == [10, 11, 12], seen
        assert src._last_rev == 12, src._last_rev

    asyncio.run(go())


def test_hung_dispatch_keeps_loop_alive_and_single_flight():
    async def go():
        seen = []
        hung = asyncio.Event()

        async def on_change(rev, level):
            seen.append(rev)
            if rev == 100:
                hung.set()
                await asyncio.sleep(100)  # never returns within the test

        src = ClusterStateSource(on_change, poll_interval_s=0.02,
                                 dispatch_timeout_s=0.15)
        src._read_revision = _drain([100, 101, 102, 103, 104])
        t = asyncio.create_task(src.run())
        await asyncio.wait_for(hung.wait(), 2)
        # Let the loop run a while WHILE the first dispatch is hung.
        await asyncio.sleep(0.8)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        # Single-flight: exactly one dispatch ran; no pile-up onto the hung one.
        assert seen == [100], f"single-flight broken: {seen}"
        # Loop stayed alive: it must NOT have advanced past the un-dispatched
        # revision (we never completed a dispatch).
        assert src._last_rev == -1, src._last_rev

    asyncio.run(go())


def test_recovery_after_hang_converges_to_latest():
    async def go():
        seen = []

        async def on_change(rev, level):
            seen.append(rev)
            if rev == 20:
                await asyncio.sleep(0.3)  # hang > timeout, then clears

        src = ClusterStateSource(on_change, poll_interval_s=0.02,
                                 dispatch_timeout_s=0.15)
        src._read_revision = _drain([20, 21, 22, 23])
        t = asyncio.create_task(src.run())
        await asyncio.sleep(1.0)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert seen[0] == 20
        assert seen.count(20) == 1, f"20 dispatched more than once: {seen}"
        # After the hang cleared, the loop resumed and dispatched a later rev.
        assert any(r > 20 for r in seen[1:]), f"no resume after hang: {seen}"

    asyncio.run(go())


if __name__ == "__main__":
    test_happy_path_dispatches_in_order_and_advances()
    test_hung_dispatch_keeps_loop_alive_and_single_flight()
    test_recovery_after_hang_converges_to_latest()
    print("✓ all cluster_loop isolation tests passed")
