"""Cluster-wide state — read directly from the local rqlited replica.

`load_cluster()` returns the cluster-wide state as a dict, sourced
from the per-node rqlite at consistency level `none`. That means:

  * Reads work *without* cluster quorum. Every node has a full
    Raft-replicated copy of the cluster-state tables in its local
    rqlite store; `level="none"` reads from that store without
    consulting the leader. A node that's been partitioned away
    from the rest of the cluster can still answer "what are this
    cluster's witnesses / what vm_type is VM X" — the per-node
    isolation-correctness argument for rqlite over SQLite-on-DRBD.
  * No projection layer: the dict is assembled on demand from rqlite,
    never written to disk. The stale-projection bug class (e.g. the
    cluster CA cert columns not being projected) is structurally
    impossible.

`build_snapshot()` (in `view_builder.py`) is the actual SQL that
assembles the dict. This module is a one-liner that wraps it, separate
so the import name reflects intent ("cluster state") rather than
implementation ("view builder").

Per-node state (this node's cluster_uuid, node_name, loopback_ip,
bootstrap_done, etc.) stays in `/etc/bedrock/state.json` — that's
per-node truth, never derived from anything.
"""
from __future__ import annotations

import threading
from typing import Optional

from . import rqlite_client, view_builder

# ── revision-keyed snapshot cache (level='none' only) ────────────────────
# The cluster-state tables change RARELY, but hot loops (netd's 4 Hz election
# tick, the arbiter, the witness worker) called load_cluster() — 16 SQL reads —
# every tick. That was ~135 local reads/sec/node of pure overhead (RCA L56).
# Cache the assembled snapshot keyed by bedrock_meta.revision: a call first does
# ONE cheap revision read; on a hit it returns the cached dict (no rebuild), so
# every consumer pays 1 read instead of 16 while nothing changes. The revision
# bumps on every cluster-state write (the same invariant the change-detection
# loop already relies on), so the cache can never serve state older than the
# local replica would — it's exactly a level='none' read, just 16x cheaper.
# Only level='none' is cached; 'strong'/'weak' callers always get a fresh read.
_CACHE_LOCK = threading.Lock()
_CACHE_REV: Optional[int] = None
_CACHE_SNAP: Optional[dict] = None


def current_revision(client: Optional[rqlite_client.RqliteClient] = None,
                     level: str = "none") -> Optional[int]:
    """The local replica's bedrock_meta.revision (ONE read). None on error."""
    try:
        owns = client is None
        c = client or rqlite_client.RqliteClient()
        try:
            row = c.query_one("SELECT revision FROM bedrock_meta WHERE id = 1",
                              level=level)
            return int(row["revision"]) if row else None
        finally:
            if owns:
                c.close()
    except Exception:
        return None


def load_cluster(
    client: Optional[rqlite_client.RqliteClient] = None,
    level: str = "none",
) -> dict:
    """Return the cluster-wide state as a dict (cluster.json shape).

    `client` may be supplied to share a connection; otherwise a fresh
    one is opened and closed for this call.

    `level` defaults to 'none' (local replica, works without quorum,
    can be stale by Raft replication lag). Pass `'strong'` after a
    network partition heals to force a Raft-leader round-trip — the
    no-quorum recovery path uses this so it doesn't make decisions
    (resume the local paused VM vs. destroy it because the peer has
    taken over) against a stale snapshot.

    level='none' is REVISION-CACHED (see the cache note above): a cache hit
    returns the shared dict and does only ONE revision read. Treat the result
    as READ-ONLY — mutating it corrupts the cache for every other caller.
    """
    global _CACHE_REV, _CACHE_SNAP
    if level == "none":
        rev = current_revision(client, level="none")
        if rev is not None:
            with _CACHE_LOCK:
                if _CACHE_REV == rev and _CACHE_SNAP is not None:
                    return _CACHE_SNAP
            snap = view_builder._cluster_view(
                view_builder.build_snapshot(client=client, level=level))
            with _CACHE_LOCK:
                _CACHE_REV = snap.get("log_index", rev)
                _CACHE_SNAP = snap
            return snap
        # revision read failed → fall through to a direct (uncached) read.
    return view_builder._cluster_view(
        view_builder.build_snapshot(client=client, level=level)
    )
