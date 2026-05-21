"""FileSagaBackend — pre-rqlite SagaBackend, persists to a JSON file.

Used during ``bedrock init`` and ``bedrock join`` — the sagas that
BRING UP rqlite. They can't persist to rqlite (rqlite is what
they're starting), so they persist their saga state to a local
JSON file at ``/var/lib/bedrock/init-progress.json``. On crash +
restart of the CLI, the executor reads the file and resumes from
the first not-``done`` step.

All other sagas (vm_create, node_leave, …) use ``RqliteSagaBackend``
because rqlite is up by then.

# File shape

```
{
  "next_id": 2,
  "ops": {
    "1": {
      "id": 1,
      "kind": "cluster_init",
      "target_node": "node1",
      "params": {"cluster_name": "test-fix", ...},
      "state": "in_progress",
      "requested_by": "operator",
      "error": null,
      "created_at": 1716234567,
      "updated_at": 1716234580
    }
  },
  "steps": {
    "1": [
      {"step_name": "prepare_dirs", "state": "done", "started_at": 1716234567, "finished_at": 1716234568, "error": null},
      {"step_name": "allocate_identity", "state": "done", "started_at": 1716234568, "finished_at": 1716234570, "error": null}
    ]
  }
}
```

# Atomicity

Every state-mutating method writes to a tmpfile + ``os.replace`` so
a crash mid-write never leaves a half-written file. The whole
state object is rewritten on each mutation — acceptable because
sagas are infrequent (1 init per cluster, 1 join per node).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from .executor import SagaState

DEFAULT_PATH = Path("/var/lib/bedrock/init-progress.json")


class FileSagaBackend:
    """SagaBackend that persists to a single JSON file. Suitable
    for pre-rqlite bootstrap sagas (cluster_init, node_join).

    Construct with ``path=`` to override the default location; the
    test fixtures point this at a tmp directory."""

    def __init__(self, path: Path = DEFAULT_PATH):
        self.path = Path(path)
        if not self.path.exists():
            self._write({"next_id": 1, "ops": {}, "steps": {}})

    # ─── Internal: load + atomic write ────────────────────────────

    def _load(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(state, indent=2, sort_keys=True))
            os.replace(tmp, str(self.path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ─── SagaBackend protocol ─────────────────────────────────────

    def insert_operation(self, *, kind, target_node, params,
                         requested_by="") -> int:
        st = self._load()
        op_id = int(st["next_id"])
        st["next_id"] = op_id + 1
        now = int(time.time())
        st["ops"][str(op_id)] = {
            "id": op_id,
            "kind": kind,
            "target_node": target_node,
            "params": dict(params),
            "state": "pending",
            "requested_by": requested_by,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        st["steps"][str(op_id)] = []
        self._write(st)
        return op_id

    def get_operation(self, op_id: int) -> Optional[dict]:
        st = self._load()
        row = st["ops"].get(str(int(op_id)))
        return dict(row) if row is not None else None

    def update_operation_state(self, op_id: int, state: SagaState, *,
                               error: Optional[str] = None) -> None:
        st = self._load()
        key = str(int(op_id))
        if key not in st["ops"]:
            raise KeyError(op_id)
        st["ops"][key]["state"] = (
            state.value if hasattr(state, "value") else str(state))
        st["ops"][key]["updated_at"] = int(time.time())
        if error is not None:
            st["ops"][key]["error"] = error
        elif state == SagaState.COMPLETED:
            # Clear stale error on successful completion.
            st["ops"][key]["error"] = None
        self._write(st)

    def list_inflight_for(self, node: str) -> list[dict]:
        st = self._load()
        out: list[dict] = []
        for row in st["ops"].values():
            if row["state"] not in ("pending", "in_progress"):
                continue
            if row.get("target_node") not in (node, None):
                continue
            out.append(dict(row))
        out.sort(key=lambda r: r["id"])
        return out

    def get_completed_steps(self, op_id: int) -> set[str]:
        st = self._load()
        return {
            s["step_name"]
            for s in st["steps"].get(str(int(op_id)), [])
            if s["state"] == "done"
        }

    def record_step_done(self, op_id: int, step_name: str, *,
                         started_at: int, finished_at: int) -> None:
        self._record_step(op_id, step_name, "done", None,
                          started_at, finished_at)

    def record_step_failed(self, op_id: int, step_name: str, *,
                           error: str, started_at: int,
                           finished_at: int) -> None:
        self._record_step(op_id, step_name, "failed", error,
                          started_at, finished_at)

    def _record_step(self, op_id, step_name, state, error,
                     started_at, finished_at) -> None:
        st = self._load()
        key = str(int(op_id))
        steps = st["steps"].setdefault(key, [])
        # Idempotent: if a row already exists for this step (e.g.
        # crash mid-record), overwrite rather than duplicate.
        for s in steps:
            if s["step_name"] == step_name:
                s["state"] = state
                s["error"] = error
                s["started_at"] = started_at
                s["finished_at"] = finished_at
                break
        else:
            steps.append({
                "step_name": step_name,
                "state": state,
                "error": error,
                "started_at": started_at,
                "finished_at": finished_at,
            })
        self._write(st)
