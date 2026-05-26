"""Cluster-wide state — read directly from the local rqlited replica.

`load_cluster()` returns the same dict shape consumers have been
expecting from `/etc/bedrock/cluster.json`, but sourced from the
per-node rqlite at consistency level `none`. That means:

  * Reads work *without* cluster quorum. Every node has a full
    Raft-replicated copy of the cluster-state tables in its local
    rqlite store; `level="none"` reads from that store without
    consulting the leader. A node that's been partitioned away
    from the rest of the cluster can still answer "what are this
    cluster's witnesses / what vm_type is VM X" — the per-node
    isolation-correctness argument that motivated keeping rqlite
    over SQLite-on-DRBD.
  * No projection layer. `cluster.json` and `view_builder.rebuild()`
    used to write the dict to disk on every rqlite revision change;
    that layer is gone. Stale-projection bug class (e.g. the
    cluster CA cert columns not being projected) becomes
    structurally impossible.

`build_snapshot()` (in `view_builder.py`) is the actual SQL that
assembles the dict. This module is a one-liner that wraps it; kept
separate so the import name reflects intent ("cluster state") rather
than implementation ("view builder").

Per-node state (this node's cluster_uuid, node_name, loopback_ip,
bootstrap_done, etc.) stays in `/etc/bedrock/state.json` — that's
per-node truth, never derived from anything.
"""
from __future__ import annotations

from typing import Optional

from . import rqlite_client, view_builder


def load_cluster(
    client: Optional[rqlite_client.RqliteClient] = None,
) -> dict:
    """Return the cluster-wide state as a dict (cluster.json shape).

    `client` may be supplied to share a connection; otherwise a fresh
    one is opened and closed for this call. Reads use level='none'
    so the call succeeds even when the cluster has lost quorum, as
    long as this node's local rqlite store is readable.
    """
    return view_builder._cluster_view(
        view_builder.build_snapshot(client=client, level="none")
    )
