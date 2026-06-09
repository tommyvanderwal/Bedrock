"""bedrock_d.state — canonical state I/O.

**ONE module owns rqlite reads/writes.** Code imports state helpers from
``bedrock_d.state`` (this file), which re-exports the implementations from
``lib/bedrock_state``, ``lib/rqlite_client``, and
``lib/state``.

# What's exposed

- ``rqlite_client`` — the HTTP transport (RqliteClient, AsyncRqliteClient,
  apply_schema, bump_revision, RqliteError, RqliteRowError).
- ``cluster_init``, ``node_register``, ``node_set_active``,
  ``node_loopback``, ``node_unregister``, ``node_maintenance``,
  ``operator_set``, ``obs_backends_set``, ``set_cluster_name``,
  ``set_mgmt_master``, ``tier_state``, ``drbd_node_id_assigned``,
  ``drbd_node_id_freed`` —
  high-level cluster-state mutators (typed columns, transaction-
  safe). All of these write the typed rows + bump the revision
  counter atomically so the subscriber sees consistent snapshots.
- ``load_local_state`` / ``save_local_state`` — bootstrap-only
  state.json read/write. NOT rqlite; this is the per-node
  bootstrap material (cluster_uuid, cluster_key path, role).

# What's NOT exposed

- View-building (``view_builder``) — that's the snapshot side,
  not a state mutator.
- Direct SQL. If you find yourself wanting raw SQL, write a typed
  helper in lib/bedrock_state.py and re-export it here.
  Inline SQL across the codebase is exactly what the
  "single-module" rule prevents.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Path-shim so ``lib.*`` (lib/, repo root) resolves as an import root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── rqlite transport ─────────────────────────────────────────────────
from lib.rqlite_client import (  # noqa: F401, E402
    RqliteClient,
    AsyncRqliteClient,
    RqliteError,
    RqliteRowError,
    apply_schema,
    bump_revision,
)

# ── typed cluster-state mutators ─────────────────────────────────────
from lib.bedrock_state import (  # noqa: F401, E402
    cluster_init,
    node_register,
    node_set_active,
    node_loopback,
    node_unregister,
    node_maintenance,
    operator_set,
    obs_backends_set,
    set_cluster_name,
    set_mgmt_master,
    tier_state,
    drbd_node_id_assigned,
    drbd_node_id_freed,
)

# ── per-node bootstrap state (state.json) ────────────────────────────
from lib.state import load as load_local_state  # noqa: F401, E402
from lib.state import save as save_local_state  # noqa: F401, E402


def schema_path() -> Path:
    """Return the on-disk path to bedrock_schema.sql.

    Resolved next to ``bedrock_state.py`` so it tracks that module's
    location regardless of where the package is installed."""
    from lib import bedrock_state as _bs
    return Path(_bs.__file__).parent / "bedrock_schema.sql"


__all__ = [
    # rqlite transport
    "RqliteClient", "AsyncRqliteClient",
    "RqliteError", "RqliteRowError",
    "apply_schema", "bump_revision",
    # cluster-state mutators
    "cluster_init", "node_register", "node_set_active", "node_loopback",
    "node_unregister",
    "node_maintenance", "operator_set", "obs_backends_set",
    "set_cluster_name", "set_mgmt_master", "tier_state",
    "drbd_node_id_assigned", "drbd_node_id_freed",
    # local state.json
    "load_local_state", "save_local_state",
    # helpers
    "schema_path",
]
