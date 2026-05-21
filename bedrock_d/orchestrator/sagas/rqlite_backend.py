"""RqliteSagaBackend — production storage adapter for the saga executor.

Wraps ``installer/lib/rqlite_client.RqliteClient`` to satisfy the
``SagaBackend`` protocol declared in ``executor.py``. All SQL
statements use parameterised execute/query — no string interpolation
of user-supplied values.

# Schema this depends on

``installer/lib/bedrock_schema.sql`` defines ``operations`` +
``operation_steps`` (see the SQL file's "Sagas" section). The
schema must be applied before this backend is used.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

# Import from legacy lib until module move (stage 7). The executor
# itself stays decoupled — we only import here in the production
# adapter.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "installer"))
from lib import rqlite_client  # noqa: E402

from .executor import SagaState  # noqa: E402


class RqliteSagaBackend:
    """SagaBackend backed by rqlite. One instance per bedrock-d
    process; thread-safe via the underlying RqliteClient's
    connection pool."""

    def __init__(self, client: rqlite_client.RqliteClient):
        self._c = client

    # ─── operations ────────────────────────────────────────────────

    def insert_operation(self, *, kind, target_node, params,
                         requested_by="") -> int:
        now = int(time.time())
        payload = json.dumps(params, sort_keys=True)
        # rqlite doesn't support RETURNING; we INSERT then SELECT the
        # row we just wrote. Identification by (created_at, kind,
        # requested_by, params_hash) would be cleaner, but since
        # operations.id is AUTOINCREMENT the simpler path is to ask
        # for last_insert_rowid() after the INSERT.
        results = self._c.execute(
            "INSERT INTO operations "
            "(kind, target_node, params, state, requested_by, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            params=[kind, target_node, payload, requested_by, now, now],
        )
        last_id = (results[0] or {}).get("last_insert_id")
        if not last_id:
            # Fallback: query by (kind, created_at, requested_by) —
            # should be unique within a millisecond. If multiple
            # match, prefer the highest id.
            rows = self._c.query(
                "SELECT id FROM operations "
                "WHERE kind=? AND created_at=? AND requested_by=? "
                "ORDER BY id DESC LIMIT 1",
                params=[kind, now, requested_by],
            )
            if not rows:
                raise RuntimeError(
                    "insert_operation: rqlite returned no id and the "
                    "fallback query found no row")
            last_id = rows[0]["id"]
        return int(last_id)

    def get_operation(self, op_id: int) -> Optional[dict]:
        rows = self._c.query(
            "SELECT id, kind, target_node, params, state, "
            "       requested_by, error, created_at, updated_at, "
            "       completed_at "
            "FROM operations WHERE id=? LIMIT 1",
            params=[int(op_id)],
        )
        if not rows:
            return None
        row = dict(rows[0])
        # rqlite returns TEXT params verbatim; keep it as a string
        # because the executor will json.loads() it.
        return row

    def update_operation_state(self, op_id: int, state: SagaState, *,
                               error: Optional[str] = None) -> None:
        now = int(time.time())
        state_val = state.value if hasattr(state, "value") else str(state)
        if state_val == "completed":
            self._c.execute(
                "UPDATE operations SET state=?, error=NULL, "
                "       updated_at=?, completed_at=? WHERE id=?",
                params=[state_val, now, now, int(op_id)],
            )
        elif error is None:
            self._c.execute(
                "UPDATE operations SET state=?, updated_at=? WHERE id=?",
                params=[state_val, now, int(op_id)],
            )
        else:
            self._c.execute(
                "UPDATE operations SET state=?, error=?, "
                "       updated_at=? WHERE id=?",
                params=[state_val, error, now, int(op_id)],
            )

    def list_inflight_for(self, node: str) -> list[dict]:
        # NULL target_node = "any node" — included in every node's
        # list so the first one to grab it wins (a future revision
        # will add a claim mechanism; for now sagas should target a
        # specific node).
        rows = self._c.query(
            "SELECT id, kind, target_node, params, state, "
            "       requested_by, error, created_at, updated_at, "
            "       completed_at "
            "FROM operations "
            "WHERE state IN ('pending', 'in_progress') "
            "  AND (target_node = ? OR target_node IS NULL) "
            "ORDER BY id ASC",
            params=[node],
        )
        return [dict(r) for r in rows]

    # ─── operation_steps ───────────────────────────────────────────

    def get_completed_steps(self, op_id: int) -> set[str]:
        rows = self._c.query(
            "SELECT step_name FROM operation_steps "
            "WHERE op_id=? AND state='done'",
            params=[int(op_id)],
        )
        return {r["step_name"] for r in rows}

    def record_step_done(self, op_id: int, step_name: str, *,
                         started_at: int, finished_at: int) -> None:
        # INSERT OR REPLACE — running the same step body twice in
        # one saga shouldn't be possible (executor skips on done set),
        # but the safety net handles a retry-after-crash where the
        # row was already written but the executor died before
        # updating its in-memory done set.
        self._c.execute(
            "INSERT OR REPLACE INTO operation_steps "
            "(op_id, step_name, state, error, started_at, finished_at) "
            "VALUES (?, ?, 'done', NULL, ?, ?)",
            params=[int(op_id), step_name, started_at, finished_at],
        )

    def record_step_failed(self, op_id: int, step_name: str, *,
                           error: str, started_at: int,
                           finished_at: int) -> None:
        self._c.execute(
            "INSERT OR REPLACE INTO operation_steps "
            "(op_id, step_name, state, error, started_at, finished_at) "
            "VALUES (?, ?, 'failed', ?, ?, ?)",
            params=[int(op_id), step_name, error, started_at, finished_at],
        )
