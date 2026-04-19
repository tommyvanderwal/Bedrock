"""In-process task registry for any operation that takes more than a second.

Purpose: give the dashboard a single view of everything the orchestrator is
currently doing (convert, migrate, import convert, delete, etc.), without
turning every long-running REST endpoint into a 2-minute spinning wheel.

Design:
  - REST action endpoints return 202 Accepted + {task_id} immediately and
    schedule the real work as an asyncio task.
  - Every state change broadcasts on WS 'task' channel so browsers react
    live. /api/tasks returns a snapshot for a fresh-join client.
  - Live state is in-memory (lost on mgmt restart — acceptable; anything
    truly in flight at that moment should fail the task on restart, but
    for now restart ≈ "all live tasks orphaned" and the next state tick
    reconciles reality anyway).
  - Finished tasks age out after `RETAIN_FINISHED_S` seconds so the active
    list stays short. History lives in VictoriaLogs via push_log.

Atomicity: Task exposes a `.rollback(fn)` so a multi-step operation can
register reverse actions as it goes; on failure, rollback runs in reverse
so partial work is unwound.
"""

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional

log = logging.getLogger("bedrock.tasks")

# How long finished tasks remain in /api/tasks (active+recent view)
RETAIN_FINISHED_S = 900  # 15 minutes

# Upper bound on log_tail per task (keeps broadcast payloads bounded)
LOG_TAIL_MAX = 4000


@dataclass
class TaskStep:
    name: str
    state: str = "pending"          # pending | running | done | failed | skipped
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[int] = None
    progress: Optional[int] = None  # 0-100 where meaningful
    error: Optional[str] = None


@dataclass
class Task:
    id: str
    type: str                        # vm.convert, vm.migrate, import.convert, ...
    subject: str                     # human one-liner ("VM foo: cattle → pet")
    state: str = "running"           # pending | running | succeeded | failed | cancelled
    started_at: str = ""
    updated_at: str = ""
    ended_at: Optional[str] = None
    progress: Optional[int] = None
    error: Optional[str] = None
    steps: list[TaskStep] = field(default_factory=list)
    log_tail: str = ""
    # index fields for the UI to filter on
    vm_name: Optional[str] = None
    import_id: Optional[str] = None
    node: Optional[str] = None
    # internal — not serialized to clients
    _rollback_stack: list[Callable] = field(default_factory=list, repr=False)

    # Public helpers the background worker calls. These are threadsafe
    # (registry lock) and auto-broadcast on every change.

    def step_start(self, name: str) -> "TaskStep":
        return _registry._step_start(self, name)

    def step_done(self, name: str, progress: Optional[int] = None):
        _registry._step_set(self, name, state="done", progress=progress)

    def step_fail(self, name: str, error: str):
        _registry._step_set(self, name, state="failed", error=error)

    def step_progress(self, name: str, progress: int):
        _registry._step_set(self, name, progress=progress)

    def set_progress(self, progress: int):
        _registry._task_update(self, progress=progress)

    def log(self, line: str):
        _registry._task_log(self, line)

    def rollback(self, fn: Callable):
        """Register a compensating action. Runs in reverse on failure."""
        self._rollback_stack.append(fn)

    def succeed(self):
        _registry._complete(self, error=None)

    def fail(self, error: str):
        _registry._complete(self, error=error)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _serialize(t: Task) -> dict:
    d = asdict(t)
    d.pop("_rollback_stack", None)
    d["steps"] = [asdict(s) for s in t.steps]
    return d


class TaskRegistry:
    """In-memory store + WS broadcaster for tasks.

    Thread-safe: background workers run in executor threads and call into
    registry methods; the internal Lock serialises mutation. Broadcasts
    are marshalled onto the main asyncio loop via
    asyncio.run_coroutine_threadsafe, same pattern as push_log.
    """

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        # Set by app.py on startup — we can't capture the event loop here
        # because it doesn't exist yet at import time.
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._hub_broadcast: Optional[Callable] = None

    def wire(self, main_loop: asyncio.AbstractEventLoop, hub_broadcast: Callable):
        self._main_loop = main_loop
        self._hub_broadcast = hub_broadcast

    # Public factory
    def create(self, type: str, subject: str, **index) -> Task:
        task_id = f"t-{int(time.time())}-{secrets.token_hex(3)}"
        now = _now()
        task = Task(
            id=task_id, type=type, subject=subject, state="running",
            started_at=now, updated_at=now,
            vm_name=index.get("vm_name"),
            import_id=index.get("import_id"),
            node=index.get("node"),
        )
        with self._lock:
            self._tasks[task_id] = task
        self._broadcast("task.create", task)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[dict]:
        """Snapshot: active + recently-finished. Ages out stale entries."""
        cutoff = time.time() - RETAIN_FINISHED_S
        out = []
        expired = []
        with self._lock:
            for tid, t in self._tasks.items():
                if t.state in ("succeeded", "failed", "cancelled"):
                    ended = t.ended_at or t.updated_at
                    try:
                        ended_ts = time.mktime(time.strptime(ended, "%Y-%m-%dT%H:%M:%SZ"))
                    except Exception:
                        ended_ts = 0
                    if ended_ts and ended_ts < cutoff:
                        expired.append(tid)
                        continue
                out.append(_serialize(t))
            for tid in expired:
                self._tasks.pop(tid, None)
        out.sort(key=lambda t: t["started_at"], reverse=True)
        return out

    # ── mutators called by Task helpers ─────────────────────────────────

    def _step_start(self, task: Task, name: str) -> TaskStep:
        with self._lock:
            # If a step with this name already exists (retry), reset it.
            existing = next((s for s in task.steps if s.name == name), None)
            if existing:
                existing.state = "running"
                existing.started_at = _now()
                existing.ended_at = None
                existing.duration_ms = None
                existing.progress = None
                existing.error = None
                step = existing
            else:
                step = TaskStep(name=name, state="running", started_at=_now())
                task.steps.append(step)
            task.updated_at = _now()
        self._broadcast("task.update", task)
        return step

    def _step_set(self, task: Task, name: str,
                  state: Optional[str] = None, progress: Optional[int] = None,
                  error: Optional[str] = None):
        with self._lock:
            step = next((s for s in task.steps if s.name == name), None)
            if not step:
                return
            if state is not None:
                step.state = state
                if state in ("done", "failed", "skipped"):
                    step.ended_at = _now()
                    if step.started_at:
                        try:
                            s = time.mktime(time.strptime(step.started_at, "%Y-%m-%dT%H:%M:%SZ"))
                            e = time.mktime(time.strptime(step.ended_at, "%Y-%m-%dT%H:%M:%SZ"))
                            step.duration_ms = int((e - s) * 1000)
                        except Exception: pass
            if progress is not None:
                step.progress = progress
            if error is not None:
                step.error = error
            task.updated_at = _now()
        self._broadcast("task.update", task)

    def _task_update(self, task: Task, progress: Optional[int] = None):
        with self._lock:
            if progress is not None:
                task.progress = progress
            task.updated_at = _now()
        self._broadcast("task.update", task)

    def _task_log(self, task: Task, line: str):
        with self._lock:
            task.log_tail = (task.log_tail + line + "\n")[-LOG_TAIL_MAX:]
            task.updated_at = _now()
        self._broadcast("task.update", task)

    def _complete(self, task: Task, error: Optional[str]):
        # Run rollback stack in reverse if we failed.
        if error and task._rollback_stack:
            for fn in reversed(task._rollback_stack):
                try:
                    fn()
                except Exception as e:
                    log.warning("rollback step failed on task %s: %s", task.id, e)
        with self._lock:
            task.state = "failed" if error else "succeeded"
            task.error = error
            task.ended_at = _now()
            task.updated_at = task.ended_at
            task.progress = 100 if not error else task.progress
            task._rollback_stack = []
        self._broadcast("task.update", task)

    # ── WS plumbing ────────────────────────────────────────────────────

    def _broadcast(self, event: str, task: Task):
        if self._main_loop is None or self._hub_broadcast is None:
            return
        payload = {"event": event, "task": _serialize(task)}
        try:
            asyncio.run_coroutine_threadsafe(
                self._hub_broadcast("task", payload), self._main_loop)
        except Exception as e:
            log.warning("task broadcast failed: %s", e)


_registry = TaskRegistry()


def registry() -> TaskRegistry:
    return _registry
