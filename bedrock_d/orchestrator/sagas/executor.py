"""Saga executor — crash-safe multi-step orchestration.

# Contract

A saga is a class decorated with ``@saga("kind")`` whose
``@step("name")``-decorated methods run in declaration order. Each
step body is idempotent: its first lines check "is this work
already done?" and return early if so.

A submitted saga is an ``operations`` row in rqlite (state =
``pending``). The executor picks it up, runs each step, records
each step's completion in ``operation_steps``, and transitions
``operations.state`` to ``completed`` or ``failed``.

On bedrock-d restart, ``resume_in_flight`` queries for any
``pending`` / ``in_progress`` rows targeted at this node and
re-runs them. Steps already recorded ``done`` are skipped; the
first not-``done`` step runs.

# What this module is NOT responsible for

- The ``operations``/``operation_steps`` schema — that lives in
  ``installer/lib/bedrock_schema.sql``.
- The wire-format / rqlite client — those are injected via the
  ``SagaBackend`` protocol. Production wiring is a thin adapter
  around ``installer.lib.rqlite_client``; tests use an in-memory
  backend so the executor can be exercised without a running rqlite.
- Cross-saga concurrency. The executor runs sagas serially per
  node. Concurrency is post-rewrite work (see codebase-rewrite-plan
  §8 open questions).

# Why a shared ``context`` dict instead of step return values

Steps need to share data (a freshly-allocated DRBD minor, a
generated UUID, …). On resume, ``context`` is rebuilt from
``operations.params`` and any subsequent step that needs a derived
value MUST re-derive it from rqlite (steps are idempotent; the
re-derivation IS the idempotency check). No persistence of mutated
context between executor runs.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Optional, Protocol

log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────
# Public registry — populated by `@saga` decorator at import time
# ───────────────────────────────────────────────────────────────────

SAGAS: dict[str, type] = {}


def saga(kind: str):
    """Class decorator that registers a saga handler under ``kind``.

    Usage:

        @saga("cluster_init")
        class ClusterInit:
            @step("rqlite_start")
            def step_rqlite_start(self, ctx):
                ...
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError("saga kind must be a non-empty string")

    def deco(cls):
        if kind in SAGAS and SAGAS[kind] is not cls:
            raise ValueError(
                f"saga kind {kind!r} already registered to "
                f"{SAGAS[kind].__module__}.{SAGAS[kind].__name__}"
            )
        cls._saga_kind = kind
        SAGAS[kind] = cls
        return cls

    return deco


def step(name: str):
    """Method decorator that marks a saga method as an ordered step.

    Steps run in declaration order. The ``name`` is the
    ``operation_steps.step_name`` value persisted to rqlite and used
    for resume.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("step name must be a non-empty string")

    def deco(fn: Callable):
        fn._saga_step_name = name
        return fn

    return deco


# ───────────────────────────────────────────────────────────────────
# Storage protocol — backend = rqlite in production, dict in tests
# ───────────────────────────────────────────────────────────────────


class SagaState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class SagaResult:
    op_id: int
    state: SagaState
    last_step: Optional[str] = None
    error: Optional[str] = None


class SagaBackend(Protocol):
    """Persistence interface for sagas. Production = rqlite,
    tests = in-memory dict (see ``tests/test_saga_executor.py``).

    Every method is synchronous; the executor wraps it for asyncio
    callers via ``asyncio.to_thread`` if needed."""

    def insert_operation(self, *, kind: str, target_node: Optional[str],
                         params: dict, requested_by: str = "") -> int: ...

    def get_operation(self, op_id: int) -> Optional[dict]: ...

    def update_operation_state(self, op_id: int, state: SagaState, *,
                               error: Optional[str] = None) -> None: ...

    def list_inflight_for(self, node: str) -> list[dict]: ...

    def get_completed_steps(self, op_id: int) -> set[str]: ...

    def record_step_done(self, op_id: int, step_name: str, *,
                         started_at: int, finished_at: int) -> None: ...

    def record_step_failed(self, op_id: int, step_name: str, *,
                           error: str, started_at: int,
                           finished_at: int) -> None: ...


# ───────────────────────────────────────────────────────────────────
# Executor
# ───────────────────────────────────────────────────────────────────


class SagaExecutor:
    """Runs a saga to completion (or failure), one step at a time.

    Single-thread / single-node. Concurrency across nodes is via
    ``operations.target_node``: only the named node picks up its
    own rows. Concurrent sagas on the same node are serialised by
    the caller (the orchestrator loop only calls ``execute_one``
    one at a time per node)."""

    def __init__(self, backend: SagaBackend, this_node: str):
        if not this_node:
            raise ValueError("this_node must be a non-empty string")
        self.backend = backend
        self.this_node = this_node

    # ─── Submission ───────────────────────────────────────────────

    def submit(self, *, kind: str, target_node: Optional[str],
               params: dict, requested_by: str = "") -> int:
        """Persist a new saga as pending. Returns the operation id.

        ``target_node=None`` means "any node may pick it up"; today
        we treat ``None`` as "this node will pick it up" but the
        scheduling story is reserved for later."""
        if kind not in SAGAS:
            raise ValueError(f"unknown saga kind {kind!r}; known: {sorted(SAGAS)}")
        if not isinstance(params, dict):
            raise TypeError("params must be a dict")
        return self.backend.insert_operation(
            kind=kind,
            target_node=target_node,
            params=params,
            requested_by=requested_by,
        )

    # ─── Execution ────────────────────────────────────────────────

    def execute_one(self, op_id: int) -> SagaResult:
        """Run (or resume) the saga identified by ``op_id``.

        Returns the final ``SagaResult``. Raises only for executor-
        internal bugs (missing saga kind in registry, backend
        protocol violation). Step exceptions are caught, recorded,
        and returned as a ``FAILED`` result."""
        op = self.backend.get_operation(op_id)
        if op is None:
            raise KeyError(f"no operation with id {op_id}")

        state = SagaState(op["state"])
        if state == SagaState.COMPLETED:
            return SagaResult(op_id=op_id, state=state)
        if state == SagaState.FAILED:
            return SagaResult(op_id=op_id, state=state,
                              error=op.get("error"))

        kind = op["kind"]
        saga_cls = SAGAS.get(kind)
        if saga_cls is None:
            err = f"unknown saga kind {kind!r}"
            self.backend.update_operation_state(
                op_id, SagaState.FAILED, error=err)
            return SagaResult(op_id=op_id, state=SagaState.FAILED,
                              error=err)

        steps = _ordered_steps(saga_cls)
        if not steps:
            # Empty saga is a degenerate "always succeed" case —
            # useful for testing wiring.
            self.backend.update_operation_state(op_id, SagaState.COMPLETED)
            return SagaResult(op_id=op_id, state=SagaState.COMPLETED)

        done = self.backend.get_completed_steps(op_id)

        # Rebuild context from params. Steps may freely mutate the
        # local dict for use by later steps in this RUN; nothing is
        # persisted between runs — on resume we re-derive.
        raw = op.get("params") or "{}"
        ctx: dict = json.loads(raw) if isinstance(raw, str) else dict(raw)
        ctx["_op_id"] = op_id
        ctx["_kind"] = kind

        self.backend.update_operation_state(op_id, SagaState.IN_PROGRESS)
        instance = saga_cls()
        last_step: Optional[str] = None

        for step_name, step_fn in steps:
            last_step = step_name
            if step_name in done:
                log.debug("saga[%s] op=%d skip step=%s (already done)",
                          kind, op_id, step_name)
                continue
            started = int(time.time())
            try:
                log.info("saga[%s] op=%d run step=%s", kind, op_id, step_name)
                step_fn(instance, ctx)
            except Exception as e:
                finished = int(time.time())
                msg = f"{type(e).__name__}: {e}"
                log.error("saga[%s] op=%d step=%s FAILED: %s",
                          kind, op_id, step_name, msg)
                self.backend.record_step_failed(
                    op_id, step_name,
                    error=msg, started_at=started, finished_at=finished,
                )
                self.backend.update_operation_state(
                    op_id, SagaState.FAILED,
                    error=f"step {step_name}: {msg}",
                )
                return SagaResult(op_id=op_id, state=SagaState.FAILED,
                                  last_step=step_name, error=msg)
            finished = int(time.time())
            self.backend.record_step_done(
                op_id, step_name,
                started_at=started, finished_at=finished,
            )

        self.backend.update_operation_state(op_id, SagaState.COMPLETED)
        log.info("saga[%s] op=%d COMPLETED", kind, op_id)
        return SagaResult(op_id=op_id, state=SagaState.COMPLETED,
                          last_step=last_step)

    # ─── Retry — explicit "this failed but I want to try again" ───
    #
    # Why this is a separate method from ``execute_one``:
    #
    # ``execute_one`` has predictable semantics — completed and
    # failed are TERMINAL. Re-calling it on a failed op returns the
    # prior failure without re-running. This is intentional: if a
    # saga failed because of a real bug, we don't want a
    # ``resume_in_flight`` on bedrock-d restart to silently re-run
    # it (and possibly re-corrupt state).
    #
    # ``retry`` is the explicit knob the orchestrator (or operator
    # via CLI) uses to say "I've reviewed the failure and want to
    # try again". It resets the op state to ``in_progress`` and
    # runs from the first not-``done`` step.

    def retry(self, op_id: int) -> SagaResult:
        """Explicitly re-run a failed (or pending) saga. Resets
        state to ``in_progress`` so ``execute_one`` will pick it
        up. Steps that previously succeeded stay ``done`` and are
        skipped on the retry."""
        op = self.backend.get_operation(op_id)
        if op is None:
            raise KeyError(f"no operation with id {op_id}")
        if op["state"] == SagaState.COMPLETED.value:
            # Already done. Nothing to retry. Return current state.
            return SagaResult(op_id=op_id, state=SagaState.COMPLETED)
        self.backend.update_operation_state(op_id, SagaState.IN_PROGRESS,
                                            error=None)
        return self.execute_one(op_id)

    # ─── Resume — automatic on bedrock-d startup ───────────────────

    def resume_in_flight(self) -> list[SagaResult]:
        """Find pending+in_progress ops targeted at this node and
        run each. Returns the results in the order processed.

        Called once on bedrock-d startup. The orchestrator's main
        loop then handles new submissions as they arrive."""
        results: list[SagaResult] = []
        for op in self.backend.list_inflight_for(self.this_node):
            try:
                results.append(self.execute_one(op["id"]))
            except Exception as e:
                # Executor-internal bug — record and continue. A
                # broken executor mustn't take the whole orchestrator
                # down.
                log.exception("saga executor: internal error on op %s",
                              op.get("id"))
                results.append(SagaResult(
                    op_id=op.get("id", -1),
                    state=SagaState.FAILED,
                    error=f"executor: {type(e).__name__}: {e}",
                ))
        return results


# ───────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────


def _ordered_steps(saga_cls: type) -> list[tuple[str, Callable]]:
    """Return ``[(step_name, unbound_method), ...]`` in declaration
    order. Uses ``__code__.co_firstlineno`` as the sort key, which
    matches source-file order for normal class definitions."""
    out: list[tuple[str, Callable, int]] = []
    for attr_name in dir(saga_cls):
        # Reach the unbound function via the class dict so we keep
        # the `_saga_step_name` annotation we set in the decorator.
        attr = getattr(saga_cls, attr_name, None)
        if attr is None or not callable(attr):
            continue
        name = getattr(attr, "_saga_step_name", None)
        if name is None:
            continue
        code = getattr(attr, "__code__", None)
        if code is None:
            continue
        out.append((name, attr, code.co_firstlineno))
    out.sort(key=lambda t: t[2])
    return [(n, fn) for (n, fn, _) in out]


def known_sagas() -> Iterable[str]:
    """For diagnostics: yields the kinds the registry knows about."""
    return iter(sorted(SAGAS))
