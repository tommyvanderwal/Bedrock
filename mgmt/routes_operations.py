"""Generic saga submission API (Stage 8 + Stage 11 foundation).

Three endpoints:

- ``POST /api/operations``                 — submit a saga
- ``GET  /api/operations/{op_id}``         — fetch state + step log
- ``GET  /api/operations?kind=&state=``    — list

This is the single surface the future ``bedrock`` CLI (Stage 11)
talks to. Today's ``POST /api/vms`` etc. endpoints can
gradually delegate here; the legacy bodies stay for backward
compat until they're empty shims.

Auth: operator-token gated (same as the rest of the mutating API).

Sync vs async: POST blocks until the saga completes OR returns
202 + op_id when ``wait=false`` is passed. Default ``wait=true``
keeps the CLI's UX simple; long-running ops should poll on the
GET endpoint.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

# Make sure bedrock_d package is importable when this module loads
# inside bedrock-d's running process.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

log = logging.getLogger(__name__)


class OpSubmit(BaseModel):
    kind: str
    target_node: Optional[str] = None
    params: dict = {}
    wait: bool = True


def register_routes(app: FastAPI, *,
                    require_operator: Callable) -> None:
    """Attach the three /api/operations endpoints to ``app``.

    ``require_operator`` is FastAPI's auth dependency from app.py;
    we inject it so the saga API enforces the same operator-token
    gate as the rest of the mutating surface."""

    # Lazy imports so test environments without bedrock_d on
    # sys.path don't pay the cost.
    def _executor():
        from bedrock_d.orchestrator.sagas import SagaExecutor
        from bedrock_d.orchestrator.sagas.rqlite_backend import (
            RqliteSagaBackend,
        )
        from bedrock_d import state as _st
        import socket
        backend = RqliteSagaBackend(_st.RqliteClient())
        return SagaExecutor(backend=backend, this_node=socket.gethostname())

    def _load_all_vm_sagas():
        """Importing these modules registers the sagas in SAGAS."""
        from bedrock_d.vm import create  # noqa: F401
        from bedrock_d.vm import destroy  # noqa: F401
        from bedrock_d.vm import grow  # noqa: F401
        from bedrock_d.vm import migrate  # noqa: F401
        from bedrock_d.install import cluster_init  # noqa: F401
        from bedrock_d.install import node_join  # noqa: F401
        from bedrock_d.install import node_leave  # noqa: F401
        from bedrock_d.install import cluster_tier  # noqa: F401
        from bedrock_d.cluster import rename as _cluster_rename  # noqa: F401

    @app.post("/api/operations")
    def api_op_submit(req: OpSubmit,
                       _user: str = Depends(require_operator)):
        """Submit (and optionally wait on) a saga. Returns the
        ``operation_id`` either way; if ``wait=true`` (default) the
        response also includes the final state + last step."""
        _load_all_vm_sagas()
        from bedrock_d.orchestrator.sagas import SAGAS, SagaState
        if req.kind not in SAGAS:
            raise HTTPException(
                400,
                f"unknown saga kind {req.kind!r}; "
                f"known: {sorted(SAGAS)}")
        try:
            ex = _executor()
        except Exception as e:
            log.exception("operations: executor init failed")
            raise HTTPException(503, f"saga executor unavailable: {e}")

        import socket
        target = req.target_node or socket.gethostname()
        try:
            op_id = ex.submit(
                kind=req.kind, target_node=target,
                params=dict(req.params), requested_by=_user,
            )
        except (TypeError, ValueError) as e:
            raise HTTPException(400, f"submit rejected: {e}")

        if not req.wait:
            return {"op_id": op_id, "state": "pending"}

        result = ex.execute_one(op_id)
        return {
            "op_id":     op_id,
            "kind":      req.kind,
            "state":     result.state.value,
            "last_step": result.last_step,
            "error":     result.error,
        }

    @app.get("/api/operations/{op_id}")
    def api_op_get(op_id: int,
                    _user: str = Depends(require_operator)):
        """Fetch one operation's state + step log."""
        from bedrock_d.orchestrator.sagas.rqlite_backend import (
            RqliteSagaBackend,
        )
        from bedrock_d import state as _st
        backend = RqliteSagaBackend(_st.RqliteClient())
        op = backend.get_operation(int(op_id))
        if op is None:
            raise HTTPException(404, f"no operation with id {op_id}")
        # Step log
        with _st.RqliteClient() as client:
            steps = client.query(
                "SELECT step_name, state, error, started_at, finished_at "
                "FROM operation_steps WHERE op_id = ? "
                "ORDER BY started_at ASC, step_name ASC",
                params=[int(op_id)],
            )
        # The stored params field is JSON-encoded TEXT; surface it
        # as a real dict in the response so the caller doesn't
        # need to double-decode.
        params_raw = op.get("params") or "{}"
        try:
            op["params"] = (json.loads(params_raw)
                            if isinstance(params_raw, str) else params_raw)
        except Exception:
            pass
        return {"op": op, "steps": list(steps)}

    @app.get("/api/operations")
    def api_op_list(kind: Optional[str] = None,
                     state: Optional[str] = None,
                     limit: int = 50,
                     _user: str = Depends(require_operator)):
        """List operations. Filter by kind and/or state.
        ``state`` ∈ {pending, in_progress, completed, failed}."""
        from bedrock_d import state as _st
        wh, params = [], []
        if kind:
            wh.append("kind = ?")
            params.append(kind)
        if state:
            wh.append("state = ?")
            params.append(state)
        sql = "SELECT id, kind, target_node, state, error, " \
              "created_at, updated_at, completed_at, requested_by " \
              "FROM operations"
        if wh:
            sql += " WHERE " + " AND ".join(wh)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with _st.RqliteClient() as client:
            rows = client.query(sql, params=params)
        return list(rows)
