"""Central cluster-state event loop for bedrock-d.

ONE responsibility, running on EVERY node: DETECT cluster-state changes
(rqlite is the source of truth) and fire the single dispatcher that drives
every reaction — snapshot refresh, the state reactors, and task/saga/backup
kicks. This is the one loop that replaces the scattered per-concern pollers
in mgmt/orchestrator.py.

This module owns ONLY detection + the read-consistency policy. The dispatcher
(the `on_change` coroutine the orchestrator injects) owns building the snapshot
at the level we hand it and running the reactors. Keeping detection separate
means the loop stays tiny and the reactor logic stays where it already lives,
proven.

READ STRATEGY — master-first, local fallback (REALLY handled, never silent):
  * DEFAULT: read the revision via the Raft LEADER (rqlite level='strong',
    linearizable). All reads go through the node-local rqlite at
    127.0.0.1:4001, which forwards strong reads to the leader, so "read from
    the one master" needs no leader-address discovery here.
  * FALLBACK: when the leader is unreachable a strong read fails. We do NOT
    swallow it — log loud, flip to LOCAL reads (level='weak', this node's own
    replica), keep converging, and periodically re-probe the leader; flip back
    when it returns. The node keeps reacting during a leader outage instead of
    going blind or spinning on a dead read. The level in force is passed to
    the dispatcher so the snapshot BUILD uses the same consistency as the
    detection (no detect-strong / build-stale-local skew).

  * SCOPE: this strong->local fallback is for CONVERGENCE reads ONLY (this loop
    + the idempotent reactors, which self-correct next tick). It must NEVER
    back a definitive/safety-critical decision — e.g. whether this node may
    take over a per-VM DRBD disk and PROMOTE it. Those reads must be
    strict-leader (level='strong', NO fallback) at their own call site and FAIL
    LOUD / defer if the leader is unreachable; deciding a takeover from a stale
    local replica risks split-brain. (The arbiter/cluster-singleton disk is
    gated separately by the witness + weighted-vote election, not here.)

CHANGE DETECTION — poll floor now, CDC fast-path ready (NOT deferred):
  * poll bedrock_meta.revision. Worst-case react latency = the poll interval;
    an acceptable transition-time floor, not the steady-state default.
  * check_now(): wake the loop immediately. rqlite v10 CDC on the leader (a
    webhook the leader POSTs per applied commit; every node registers with the
    leader) calls this for near-instant detection — layered ON TOP of the poll
    floor, which still covers followers + leader outage. CDC is the speed-up;
    poll is the correctness floor.

A fresh RqliteClient is created per read, so a client made before rqlite was
listening can never wedge the loop (the 2026-05-29 dead-client bug). A crash
in run() escalates to the orchestrator's supervise() wrapper — loud, not a
silently vanished task.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))
from lib import rqlite_client  # type: ignore  # noqa: E402

log = logging.getLogger("bedrock.cluster_loop")

# rqlite read-consistency (see installer/lib/rqlite_client.py):
#   'strong' = linearizable, routed to the leader  -> the "master" read
#   'weak'   = this node's local replica, sub-second stale -> the fallback
_READ_MASTER = "strong"
_READ_LOCAL = "weak"

# Dispatcher: (revision, read_level) -> awaitable. Injected by the orchestrator.
OnChange = Callable[[int, str], Awaitable[None]]


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
        self._mode = _READ_MASTER
        self._last_rev = -1
        self._local_ticks = 0

    # ── public ────────────────────────────────────────────────────────
    def check_now(self) -> None:
        """Wake the loop immediately (CDC webhook / manual trigger)."""
        self._wake.set()

    async def run(self) -> None:
        """The central loop. Runs forever; wrap in the orchestrator's
        supervise() so a crash is loud + restarts."""
        log.info("cluster_loop: starting (poll=%.2fs, %s-first reads)",
                 self._poll_interval_s, _READ_MASTER)
        await self._tick(force=True)   # converge current state at startup
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=self._poll_interval_s)
            except asyncio.TimeoutError:
                pass  # poll floor fired — fall through and check
            self._wake.clear()
            await self._tick()

    # ── internals ─────────────────────────────────────────────────────
    async def _tick(self, *, force: bool = False) -> None:
        rev, level = await asyncio.to_thread(self._read_revision)
        if rev is None:
            return  # rqlite unreachable at leader AND local (logged loud)
        if not force and rev == self._last_rev:
            return
        self._last_rev = rev
        # Single fan-out. on_change builds the snapshot at `level` and runs
        # the reactors. NOT wrapped in a blanket catch here — a crash
        # escalates to supervise(), it is never silently swallowed.
        await self._on_change(rev, level)

    def _read_revision(self):
        """(revision, level) at the current read level, with a master->local
        fallback that is REALLY handled. Returns (None, level) only if rqlite
        is unreachable at BOTH the leader and the local replica."""
        # Periodically re-probe the leader once we've fallen back to local.
        if self._mode == _READ_LOCAL:
            self._local_ticks += 1
            if self._local_ticks * self._poll_interval_s >= self._leader_retry_s:
                self._local_ticks = 0
                if self._try_revision(_READ_MASTER) is not None:
                    log.warning("cluster_loop: leader reachable again — "
                                "resuming master (strong) reads")
                    self._mode = _READ_MASTER

        rev = self._try_revision(self._mode)
        if rev is not None:
            return rev, self._mode

        if self._mode == _READ_MASTER:
            log.warning("cluster_loop: leader unreachable for strong read — "
                        "falling back to LOCAL replica (weak); will keep "
                        "converging on local state and re-probe the leader")
            self._mode = _READ_LOCAL
            self._local_ticks = 0
            rev = self._try_revision(_READ_LOCAL)
            if rev is not None:
                return rev, self._mode
        log.error("cluster_loop: rqlite unreachable at BOTH leader and local "
                  "replica — no cluster-state read this tick")
        return None, self._mode

    def _try_revision(self, level: str) -> Optional[int]:
        """One revision read at `level` with a FRESH client. Returns None on
        any rqlite error (caller decides fallback)."""
        try:
            with rqlite_client.RqliteClient() as rc:
                row = rc.query_one("SELECT revision FROM bedrock_meta",
                                   level=level)
            return int(row["revision"]) if row else None
        except Exception:
            return None
