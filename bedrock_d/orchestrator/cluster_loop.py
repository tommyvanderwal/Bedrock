"""Central cluster-state event loop for bedrock-d.

ONE responsibility, running on EVERY node: detect cluster-state changes
(rqlite is the source of truth) and drive every reaction off them — refresh
the in-memory/on-disk snapshot, run the state reactors, and kick the
tasks / sagas / backups a change implies. This is the single dispatcher that
replaces the scattered per-concern poll loops in mgmt/orchestrator.py.

READ STRATEGY — master-first, local fallback (REALLY handled, never silent):
  * DEFAULT: read via the Raft LEADER (rqlite level='strong', linearizable).
    Every read goes through the node-local rqlite at 127.0.0.1:4001, which
    forwards strong reads to the leader — so "read from the one master" needs
    no leader-address discovery here; rqlite does the routing.
  * FALLBACK: when the leader is unreachable a strong read raises. We do NOT
    swallow it — we log it loudly and flip to LOCAL reads (level='weak', this
    node's own Raft replica, possibly a few hundred ms stale), keep converging
    on local state, and periodically re-attempt the leader. When the leader
    returns we flip back. The node keeps reacting during a leader outage
    instead of going blind or spinning on a dead read.

CHANGE DETECTION — poll floor now, CDC fast-path ready (NOT deferred):
  * poll bedrock_meta.revision. Worst-case react latency = the poll interval;
    that is an acceptable transition-time floor, not the steady-state default.
  * check_now(): wake the loop immediately. rqlite CDC on the leader (a
    webhook the leader POSTs per applied commit; every node registers with the
    leader) calls this to make detection near-instant — layered ON TOP of the
    poll floor, which still covers followers and leader outages. CDC is the
    speed-up; the poll is the correctness floor.

The dispatcher (`on_change`) is supplied by the orchestrator and owns
per-reactor isolation (each reactor its own try/except + timeout) so one slow
or failing reactor cannot stall the others. This module does NOT blanket-catch
around it: a crash propagates to the supervise() wrapper, loudly.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))
from lib import rqlite_client, view_builder  # type: ignore  # noqa: E402

log = logging.getLogger("bedrock.cluster_loop")

# rqlite read-consistency levels (see installer/lib/rqlite_client.py):
#   'strong' = linearizable, routed to the leader  -> the "master" read
#   'weak'   = this node's local replica, sub-second stale -> the fallback
_READ_MASTER = "strong"
_READ_LOCAL = "weak"

# Type of the dispatcher the orchestrator registers.
OnChange = Callable[[dict, dict, int], Awaitable[None]]


class ClusterStateSource:
    """Single source of cluster-state-change events for this node."""

    def __init__(
        self,
        on_change: OnChange,
        *,
        poll_interval_s: float = 0.5,
        leader_retry_s: float = 5.0,
    ) -> None:
        self._on_change = on_change
        self._poll_interval_s = poll_interval_s
        self._leader_retry_s = leader_retry_s
        self._wake = asyncio.Event()
        self._mode = _READ_MASTER            # current read consistency
        self._last_rev = 0
        self._prev_snapshot: dict = view_builder.empty_snapshot()
        # monotonic-ish counter of consecutive local-mode ticks, so we
        # re-probe the leader roughly every leader_retry_s.
        self._local_ticks = 0

    # ── public ────────────────────────────────────────────────────────
    def check_now(self) -> None:
        """Wake the loop immediately (CDC webhook / manual trigger)."""
        self._wake.set()

    async def run(self) -> None:
        """The central loop. Run forever; meant to be wrapped by the
        orchestrator's supervise() so a crash is loud + restarts."""
        log.info("cluster_loop: starting (poll=%.2fs, read=%s-first)",
                 self._poll_interval_s, _READ_MASTER)
        # Converge current state once at startup — a node already caught
        # up to HEAD must still react to state set before it started.
        await self._tick(force=True)
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass  # poll floor fired (expected) — fall through and check
            self._wake.clear()
            await self._tick()

    # ── internals ─────────────────────────────────────────────────────
    async def _tick(self, *, force: bool = False) -> None:
        rev = await asyncio.to_thread(self._read_revision)
        if rev is None:
            return  # rqlite fully unreachable this tick (logged loudly)
        if not force and rev == self._last_rev:
            return
        snap = await asyncio.to_thread(self._build_snapshot)
        if snap is None:
            return
        self._last_rev = rev
        prev = self._prev_snapshot
        self._prev_snapshot = snap
        # Single fan-out point. on_change owns per-reactor isolation; a
        # crash here is intentionally NOT caught — it escalates to the
        # supervise() wrapper (fail loud), it is not silently swallowed.
        await self._on_change(snap, prev, rev)

    def _read_revision(self) -> Optional[int]:
        """bedrock_meta.revision at the current read level, with a
        master->local fallback that is REALLY handled, not swallowed.

        Returns the revision int, or None if rqlite is unreachable at
        BOTH the leader and the local replica (genuinely down)."""
        # Periodically re-probe the leader after we've fallen back local.
        if self._mode == _READ_LOCAL:
            self._local_ticks += 1
            if self._local_ticks * self._poll_interval_s >= self._leader_retry_s:
                self._local_ticks = 0
                if self._try_revision(_READ_MASTER) is not None:
                    log.warning("cluster_loop: leader reachable again — "
                                "back to master (strong) reads")
                    self._mode = _READ_MASTER

        rev = self._try_revision(self._mode)
        if rev is not None:
            return rev

        # Current mode failed. If we were reading the master, fall back
        # to the local replica — loudly. If local also fails, rqlite is
        # genuinely down on this node.
        if self._mode == _READ_MASTER:
            log.warning("cluster_loop: leader unreachable for strong read — "
                        "falling back to LOCAL replica (weak); will keep "
                        "converging on local state and re-probe the leader")
            self._mode = _READ_LOCAL
            self._local_ticks = 0
            rev = self._try_revision(_READ_LOCAL)
            if rev is not None:
                return rev
        log.error("cluster_loop: rqlite unreachable at BOTH leader and local "
                  "replica — no cluster-state read this tick")
        return None

    def _try_revision(self, level: str) -> Optional[int]:
        """One revision read at `level`. Returns None on any rqlite error
        (the caller decides fallback); does not log — the caller logs the
        meaningful transition."""
        try:
            with rqlite_client.RqliteClient() as rc:
                row = rc.query_one("SELECT revision FROM bedrock_meta",
                                   level=level)
            if row is None:
                return None
            return int(row.get("revision", 0))
        except Exception:
            return None

    def _build_snapshot(self) -> Optional[dict]:
        """Full snapshot at the current read level. None on read failure
        (logged loud) so the tick is skipped rather than acting on a
        half-built view."""
        try:
            with rqlite_client.RqliteClient() as rc:
                return view_builder.build_snapshot(client=rc, level=self._mode)
        except Exception:
            log.exception("cluster_loop: snapshot build failed at level=%s "
                          "(skipping this tick)", self._mode)
            return None
