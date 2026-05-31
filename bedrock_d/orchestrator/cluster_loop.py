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
from typing import Awaitable, Callable, Optional

# Route rqlite I/O through the canonical state surface (bedrock_d.state owns it
# + sets up the lib path shim internally), not lib.rqlite_client directly — see
# tests/test_state_source_lint. This loop is a pure rqlite READER (it polls
# bedrock_meta.revision), so it only needs the client constructor.
from bedrock_d.state import RqliteClient  # noqa: E402

log = logging.getLogger("bedrock.cluster_loop")

# rqlite read-consistency (see installer/lib/rqlite_client.py):
#   'none' = this node's LOCAL replica, no leader round-trip, NO Raft barrier.
# Change DETECTION uses 'none': it only needs to NOTICE a new committed revision,
# which the local replica applies via Raft within ms (the CDC webhook + the poll
# floor catch it). Reading at 'strong' here was a self-sustaining Raft-BARRIER
# storm (RCA L55, 2026-05-31): a strong read appends a no-op to the leader's log →
# that is an applied commit → the rqlite-v10 CDC webhook fires to every node →
# wakes them to strong-read again → ~64 barriers/s, ~30 MB/s of pointless fsync,
# zero data changing. Critical reactors (DRBD takeover/promote) still read
# 'strong' at THEIR OWN call site, so linearizable decisions are unaffected.
_READ_LOCAL = "none"

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
        dispatch_timeout_s: float = 45.0,
    ) -> None:
        self._on_change = on_change
        self._poll_interval_s = poll_interval_s
        self._leader_retry_s = leader_retry_s
        # Upper bound on how long the loop will WAIT for a reactor dispatch
        # before declaring it hung and carrying on. Generous: a normal tick's
        # reactors (obs/drbd/arbiter/iso) do a handful of subprocess calls and
        # finish in well under a second; 45s only trips on a genuinely wedged
        # drbdadm/virsh/systemctl. The dispatch keeps running (shielded) — we
        # just stop blocking the loop on it.
        self._dispatch_timeout_s = dispatch_timeout_s
        self._wake = asyncio.Event()
        self._last_rev = -1
        # Single-flight: the in-flight reactor-dispatch task, if any. Detection
        # keeps running while it does; a new trigger never piles a second
        # dispatch on top of a slow/hung one.
        self._dispatch_task: Optional[asyncio.Task] = None

    # ── public ────────────────────────────────────────────────────────
    def check_now(self) -> None:
        """Wake the loop immediately (CDC webhook / manual trigger)."""
        self._wake.set()

    async def run(self) -> None:
        """The central loop. Runs forever; wrap in the orchestrator's
        supervise() so a crash is loud + restarts."""
        log.info("cluster_loop: starting (poll=%.2fs, local '%s' detection reads "
                 "— no Raft barrier; critical reactors read 'strong' at their "
                 "call site)", self._poll_interval_s, _READ_LOCAL)
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
        # Single-flight: if a prior dispatch is still running (a slow or hung
        # reactor), do NOT pile a second one on top. Skip this trigger without
        # advancing _last_rev, so the loop keeps polling/detecting and we
        # reconverge the instant the dispatch frees. (The dispatch reads the
        # CURRENT rqlite state when it runs, so skipping intermediate revisions
        # loses nothing — it converges to the latest.)
        if self._dispatch_task is not None and not self._dispatch_task.done():
            log.warning("cluster_loop: prior reactor dispatch still running — "
                        "skipping rev %d trigger (reconverges when it frees); "
                        "detection/CDC keep running meanwhile", rev)
            return
        # on_change builds the snapshot at `level` and runs the reactors. We
        # BOUND how long the loop blocks on it: a genuinely hung reactor (a
        # wedged drbdadm/virsh) must never freeze detection, CDC fan-out, or
        # leader re-probe. shield() keeps the dispatch running after a timeout
        # (it is not cancelled) — we just stop waiting on it; the single-flight
        # guard above then defers new triggers until it completes. A real
        # reactor *exception* is NOT swallowed here: shield re-raises it and it
        # escalates to supervise() (fail loud). _last_rev advances only after a
        # completed dispatch, so a timeout retries next tick.
        self._dispatch_task = asyncio.ensure_future(self._on_change(rev, level))
        try:
            await asyncio.wait_for(asyncio.shield(self._dispatch_task),
                                   timeout=self._dispatch_timeout_s)
        except asyncio.TimeoutError:
            log.error("cluster_loop: reactor dispatch for rev %d exceeded %.0fs "
                      "— a reactor is HUNG. Loop stays alive (detection/CDC/"
                      "leader re-probe continue); last_rev held at %d, dispatch "
                      "still running, retries once it frees.",
                      rev, self._dispatch_timeout_s, self._last_rev)
            return
        self._last_rev = rev

    def _read_revision(self):
        """(revision, 'none'). Reads the LOCAL replica only — change DETECTION
        does not need the leader (and reading the leader at 'strong' was the
        Raft-barrier storm, see _READ_LOCAL). The local replica applies committed
        revisions via Raft within ms; the CDC webhook + 500ms poll floor catch
        every change. The reactors get level='none' too — convergence reads may
        run on local state (the two-read-classes rule); DRBD-takeover/promote
        reactors re-read 'strong' at their OWN call site, so nothing that needs
        linearizability loses it. Returns (None, 'none') only if THIS node's
        local rqlite replica is unreachable (logged loud; retried next tick)."""
        rev = self._try_revision(_READ_LOCAL)
        if rev is not None:
            return rev, _READ_LOCAL
        log.error("cluster_loop: local rqlite replica unreachable — no "
                  "cluster-state read this tick (retries next poll)")
        return None, _READ_LOCAL

    def _try_revision(self, level: str) -> Optional[int]:
        """One revision read at `level` with a FRESH client. Returns None on
        any rqlite error (caller decides fallback)."""
        try:
            with RqliteClient() as rc:
                row = rc.query_one("SELECT revision FROM bedrock_meta",
                                   level=level)
            return int(row["revision"]) if row else None
        except Exception:
            return None
