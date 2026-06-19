"""Shared in-memory state for the unified `bedrock-d` daemon.

All Bedrock-owned Python (netd, mgmt FastAPI, orchestrator tasks)
runs in one process and reads/writes this single state object,
replacing the file-based IPC (/run/bedrock/*.json) and global module
state that the split-daemon design needed.

Locking discipline:

  * netd_lock — held by the netd thread while it mutates Daemon
    (neighbours, ever_seen_peers). asyncio readers take a snapshot
    copy under the lock then process outside.
  * cluster_lock — held by the cluster thread while it mutates
    ClusterDaemon (peer_hb, election stance) and publishes election
    outcomes, fence_view, and netd_ws (witness state). The fence
    endpoint writes drbd_down_peers under cluster_lock too.
  * snapshot_lock — held by rqlite_subscriber while replacing the
    snapshot dict. FastAPI handlers take read snapshots.

Never hold a lock across an `await` point. RLock means re-entrant
within a single thread (asyncio task = main thread) which is what
we want.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BedrockState:
    """The single source of truth for in-process Bedrock state.

    Composed of subsystem state objects (netd.Daemon, mgmt snapshot
    dicts) plus cross-cutting fields (no-quorum marker, stop signal).

    Construction order: state = BedrockState(stop_event=Event()),
    then subsystems attach their state on startup:
        state.netd = netd.init_daemon(...)
    """

    # ── Cross-cutting / lifecycle ─────────────────────────────────
    stop_event: threading.Event = field(default_factory=threading.Event)

    # Per-node identity — populated at startup, never changes.
    # Convenience copies of state.json values so other subsystems
    # don't have to re-read the file.
    self_node_name: str = ""
    self_loopback_ip: str = ""
    cluster_uuid: str = ""

    # No-quorum marker semaphore. True = node is in sticky no-quorum
    # state. The cluster thread sets True on NoQuorum + holddown;
    # orchestrator's no_quorum_responder sets False after cleanup
    # completes AND quorum is back. The on-disk /run/bedrock-no-quorum
    # file is written/cleared in lockstep so external debug tooling
    # sees it.
    no_quorum_marker_present: bool = False

    # ── netd-owned state ──────────────────────────────────────────
    # The Daemon object lives here. Single-writer = netd thread.
    # asyncio readers must copy-out under netd_lock.
    netd: Optional[Any] = None  # netd.Daemon; typed as Any to avoid circular import
    netd_lock: threading.RLock = field(default_factory=threading.RLock)

    # ── cluster-owned state (election / witness / quorum) ───────────
    # ClusterDaemon lives here. Single-writer = cluster thread.
    cluster: Optional[Any] = None  # cluster_daemon.ClusterDaemon
    cluster_lock: threading.RLock = field(default_factory=threading.RLock)

    # Last election outcome (string) — written by the cluster thread;
    # read by the dashboard and cluster_arbiter.
    last_election_outcome: str = ""

    # WitnessState (sock + discovered Echo endpoints + passive slot cache +
    # own_marker/own_tag). Published by cluster_daemon.run_cluster_daemon so
    # cluster_arbiter's takeover protocol can read peers' slots and set its
    # own witness-claim slot at promote time. cluster_arbiter OWNS own_tag;
    # the cluster thread refreshes own_marker each tick (Q-01/BAD-4).
    netd_ws: Optional[Any] = None

    # ── DRBD fence-peer coordination (replaces /run/bedrock/fence-verdict.json) ──
    # DRBD detects a lost peer FAST (~3-6 s). On a fence callout the
    # /internal/fence-decision endpoint feeds DRBD's AUTHORITATIVE per-peer
    # "down" evidence here; the cluster election forces those peers' liveness
    # False, collapsing detection lag. Keyed by loopback octet -> monotonic ts;
    # the cluster thread expires entries (DRBD_DOWN_TTL_S) so a healed peer
    # is re-counted. Written by the endpoint, read+expired by cluster — both
    # under cluster_lock.
    drbd_down_peers: dict = field(default_factory=dict)
    # The cluster thread publishes the live fence verdict each election tick:
    #   {outcome: str, down_acked: list[int octet], stable_since: float(mono),
    #    updated: float(mono), self_octet: int}
    # Read by the fence-decision endpoint under cluster_lock.
    fence_view: dict = field(default_factory=dict)

    # ── orchestrator-owned state (rqlite_subscriber etc.) ─────────
    # Live snapshot of cluster state, projected from rqlite by the
    # subscriber task. FastAPI handlers read this directly.
    snapshot: dict = field(default_factory=dict)
    prev_snapshot: dict = field(default_factory=dict)
    last_log_idx: int = 0
    snapshot_lock: threading.RLock = field(default_factory=threading.RLock)

    # boot_orchestrator + no_quorum_responder rendezvous flag.
    services_started: bool = False

    # ── per-task transient state (no lock needed; touched only by
    #    that task) ───────────────────────────────────────────────
    scheduled_inflight: set = field(default_factory=set)


def snapshot_copy(state: BedrockState) -> dict:
    """Return a deep-enough copy of the current cluster snapshot for
    a reader that's about to do work outside the lock. Cheap: most
    callers only need .get on top-level keys."""
    import copy
    with state.snapshot_lock:
        return copy.deepcopy(state.snapshot)


def netd_status_view(state: BedrockState) -> dict:
    """Return a JSON-serialisable view of netd's current state. Used by
    the dashboard /api/mesh and /api/witness endpoints to replace
    reading /run/bedrock/mesh_neighbors.json from disk."""
    with state.netd_lock:
        d = state.netd
        if d is None:
            return {"running": False}
        # Pull what the old /run/bedrock/mesh_neighbors.json contained.
        # Defensive: handle a half-initialised Daemon mid-startup.
        out: dict = {
            "running": True,
            "me": getattr(d, "my_node", ""),
            "loopback_ip": getattr(d, "my_loopback", ""),
            "cluster_uuid": getattr(d, "cluster_uuid", ""),
            "nics": {},
            "election_outcome": state.last_election_outcome,
        }
        for nic, addr in getattr(d, "nic_addrs", {}).items():
            ns = []
            for (peer_node, peer_nic, my_nic), n in getattr(d, "neighbours", {}).items():
                if my_nic != nic:
                    continue
                ns.append({
                    "peer_node": peer_node,
                    "peer_nic": peer_nic,
                    "peer_loopback": getattr(n, "peer_loopback", ""),
                    "peer_link_addr": getattr(n, "peer_link_addr", ""),
                    "logged_up": getattr(n, "logged_up", False),
                    "rtt_us": getattr(n, "rtt_us", 0),
                    "first_seen": getattr(n, "first_seen", 0.0),
                    "last_seen": getattr(n, "last_seen", 0.0),
                })
            out["nics"][nic] = {"addr": addr, "neighbours": ns}
        return out
