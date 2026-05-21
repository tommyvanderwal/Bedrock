"""bedrock_d.state — canonical state I/O for the rewritten codebase.

# Rule (from docs/codebase-rewrite-plan.md §3.3)

**ONE module owns rqlite reads/writes.** New code imports from
``bedrock_d.state`` (this file). The legacy ``installer/lib/bedrock_state``
+ ``installer/lib/rqlite_client`` modules continue to back this
re-export until Stage 7 (the directory move).

# Why re-export instead of move-now

Moving the implementations now would break in-flight imports across
~6 legacy modules in installer/lib/ + mgmt/. The two-step approach:

1. **Now** — create this thin re-export so new code points here
   from day one and the eventual move is mechanical.
2. **Stage 7** — physically relocate ``bedrock_state.py`` to
   ``bedrock_d/state.py`` and delete the re-exports.

# What's exposed

- ``rqlite_client`` — the HTTP transport (RqliteClient, AsyncRqliteClient,
  apply_schema, bump_revision, RqliteError, RqliteRowError).
- ``cluster_init``, ``node_register``, ``node_loopback``,
  ``node_unregister``, ``node_maintenance``, ``operator_set``,
  ``obs_backends_set``, ``set_mgmt_master``, ``tier_state``,
  ``drbd_node_id_assigned``, ``drbd_node_id_freed``, etc. —
  high-level cluster-state mutators (typed columns, transaction-
  safe). All of these write the typed rows + bump the revision
  counter atomically so the subscriber sees consistent snapshots.
- ``load_local_state`` / ``save_local_state`` — bootstrap-only
  state.json read/write. NOT rqlite; this is the per-node
  bootstrap material (cluster_uuid, cluster_key path, role).

# What's NOT exposed

- View-building (``view_builder``) — that's the snapshot side,
  not a state mutator. Stays in installer/lib/view_builder.py for
  now; Stage 7 will move it to ``bedrock_d/snapshot.py``.
- Direct SQL. If you find yourself wanting raw SQL, write a typed
  helper in installer/lib/bedrock_state.py and re-export it here.
  Inline SQL across the codebase is exactly what the
  "single-module" rule prevents.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Path-shim while installer/lib/ still owns the implementation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

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
    node_loopback,
    node_unregister,
    node_maintenance,
    operator_set,
    obs_backends_set,
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

    Lives next to ``bedrock_state.py`` in the legacy layout. After
    Stage 7 moves both into bedrock_d/, this helper returns the
    relocated path without callers having to change."""
    from lib import bedrock_state as _bs
    return Path(_bs.__file__).parent / "bedrock_schema.sql"


__all__ = [
    # rqlite transport
    "RqliteClient", "AsyncRqliteClient",
    "RqliteError", "RqliteRowError",
    "apply_schema", "bump_revision",
    # cluster-state mutators
    "cluster_init", "node_register", "node_loopback", "node_unregister",
    "node_maintenance", "operator_set", "obs_backends_set",
    "set_mgmt_master", "tier_state",
    "drbd_node_id_assigned", "drbd_node_id_freed",
    # local state.json
    "load_local_state", "save_local_state",
    # helpers
    "schema_path",
]
