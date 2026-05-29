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

from typing import Optional

from . import rqlite_client, view_builder


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
    """
    return view_builder._cluster_view(
        view_builder.build_snapshot(client=client, level=level)
    )
