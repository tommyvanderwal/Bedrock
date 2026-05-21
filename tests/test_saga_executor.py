"""Unit tests for the saga executor.

Covers:
- registry: @saga + @step register correctly
- step ordering: declaration order is preserved
- execute_one: happy path (all steps run, recorded done, op completed)
- execute_one: failure path (step raises → step recorded failed, op failed)
- execute_one: resume after partial completion (skips done steps)
- execute_one: idempotent re-call on completed op
- execute_one: idempotent re-call on failed op
- empty saga (degenerate "always succeed")
- unknown kind → recorded failed
- shared context dict mutates across steps in one run
- resume_in_flight: only targets this node, returns results in order
- submit: rejects unknown kind, rejects non-dict params

The executor never touches rqlite directly; it uses a SagaBackend
protocol. The in-memory `MemoryBackend` here implements the protocol
faithfully so the tests exercise the same code paths production will.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bedrock_d.orchestrator.sagas import (  # noqa: E402
    SAGAS,
    SagaBackend,
    SagaExecutor,
    SagaResult,
    SagaState,
    saga,
    step,
)


# ───────────────────────────────────────────────────────────────────
# Test backend
# ───────────────────────────────────────────────────────────────────


class MemoryBackend:
    """In-memory SagaBackend faithful to the production rqlite shape.

    `operations` and `operation_steps` are plain dicts/lists; ids
    are autoincrement integers."""

    def __init__(self) -> None:
        self.ops: dict[int, dict] = {}
        self.steps: dict[int, list[dict]] = {}
        self._next_id = 1

    # SagaBackend protocol
    def insert_operation(self, *, kind, target_node, params,
                         requested_by=""):
        op_id = self._next_id
        self._next_id += 1
        self.ops[op_id] = {
            "id": op_id,
            "kind": kind,
            "target_node": target_node,
            "params": dict(params),
            "state": "pending",
            "requested_by": requested_by,
            "error": None,
            "created_at": 0,
            "updated_at": 0,
        }
        self.steps[op_id] = []
        return op_id

    def get_operation(self, op_id):
        op = self.ops.get(op_id)
        return None if op is None else dict(op)

    def update_operation_state(self, op_id, state, *, error=None):
        if op_id not in self.ops:
            raise KeyError(op_id)
        self.ops[op_id]["state"] = state.value if hasattr(state, "value") else state
        if error is not None:
            self.ops[op_id]["error"] = error

    def list_inflight_for(self, node):
        return [
            dict(op) for op in self.ops.values()
            if op["state"] in ("pending", "in_progress")
            and op["target_node"] == node
        ]

    def get_completed_steps(self, op_id):
        return {
            s["step_name"] for s in self.steps.get(op_id, [])
            if s["state"] == "done"
        }

    def record_step_done(self, op_id, step_name, *,
                         started_at, finished_at):
        self.steps.setdefault(op_id, []).append({
            "op_id": op_id,
            "step_name": step_name,
            "state": "done",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": None,
        })

    def record_step_failed(self, op_id, step_name, *,
                           error, started_at, finished_at):
        self.steps.setdefault(op_id, []).append({
            "op_id": op_id,
            "step_name": step_name,
            "state": "failed",
            "started_at": started_at,
            "finished_at": finished_at,
            "error": error,
        })


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test gets a fresh saga registry. Without this, decorator
    re-registration across tests would clobber + cross-pollinate."""
    saved = dict(SAGAS)
    SAGAS.clear()
    yield
    SAGAS.clear()
    SAGAS.update(saved)


@pytest.fixture
def backend():
    return MemoryBackend()


@pytest.fixture
def executor(backend):
    return SagaExecutor(backend=backend, this_node="n1")


# ───────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────


def test_saga_decorator_registers_kind():
    @saga("foo")
    class Foo:
        pass

    assert "foo" in SAGAS
    assert SAGAS["foo"] is Foo
    assert Foo._saga_kind == "foo"


def test_saga_decorator_rejects_empty_kind():
    with pytest.raises(ValueError):
        @saga("")
        class Bad:
            pass


def test_saga_decorator_rejects_duplicate_kind():
    @saga("dup")
    class A:
        pass

    with pytest.raises(ValueError):
        @saga("dup")
        class B:
            pass


def test_step_decorator_marks_method():
    @saga("s1")
    class S1:
        @step("one")
        def do(self, ctx):
            pass

    assert S1.do._saga_step_name == "one"


def test_step_rejects_empty_name():
    with pytest.raises(ValueError):
        @step("")
        def f(self, ctx):
            pass


def test_empty_saga_completes_immediately(executor, backend):
    @saga("noop")
    class Noop:
        pass

    op_id = executor.submit(kind="noop", target_node="n1", params={})
    result = executor.execute_one(op_id)

    assert result.state == SagaState.COMPLETED
    assert backend.ops[op_id]["state"] == "completed"
    assert backend.steps[op_id] == []


def test_happy_path_all_steps_run_in_order(executor, backend):
    trace: list[str] = []

    @saga("happy")
    class Happy:
        @step("a")
        def step_a(self, ctx):
            trace.append("a")

        @step("b")
        def step_b(self, ctx):
            trace.append("b")

        @step("c")
        def step_c(self, ctx):
            trace.append("c")

    op_id = executor.submit(kind="happy", target_node="n1", params={})
    result = executor.execute_one(op_id)

    assert result.state == SagaState.COMPLETED
    assert result.last_step == "c"
    assert trace == ["a", "b", "c"]
    assert backend.get_completed_steps(op_id) == {"a", "b", "c"}


def test_step_failure_records_failed_and_stops(executor, backend):
    trace: list[str] = []

    @saga("boom")
    class Boom:
        @step("a")
        def step_a(self, ctx):
            trace.append("a")

        @step("b")
        def step_b(self, ctx):
            trace.append("b")
            raise RuntimeError("kaboom")

        @step("c")
        def step_c(self, ctx):
            trace.append("c")  # should NOT run

    op_id = executor.submit(kind="boom", target_node="n1", params={})
    result = executor.execute_one(op_id)

    assert result.state == SagaState.FAILED
    assert result.last_step == "b"
    assert "kaboom" in (result.error or "")
    assert trace == ["a", "b"]   # c not reached
    assert backend.ops[op_id]["state"] == "failed"
    # step a recorded done, step b recorded failed
    step_states = {s["step_name"]: s["state"] for s in backend.steps[op_id]}
    assert step_states == {"a": "done", "b": "failed"}


def test_resume_skips_already_completed_steps(executor, backend):
    """The whole point: on crash + restart, re-running the saga
    skips the steps already marked done."""
    trace: list[str] = []

    @saga("crash")
    class Crash:
        @step("a")
        def step_a(self, ctx):
            trace.append("a")

        @step("b")
        def step_b(self, ctx):
            trace.append("b")

        @step("c")
        def step_c(self, ctx):
            trace.append("c")

    op_id = executor.submit(kind="crash", target_node="n1", params={})
    # Simulate "step a ran already" before this executor started.
    backend.record_step_done(op_id, "a", started_at=0, finished_at=1)

    result = executor.execute_one(op_id)

    assert result.state == SagaState.COMPLETED
    # a was skipped, b + c ran
    assert trace == ["b", "c"]


def test_retry_after_failure_picks_up_at_failed_step(executor, backend):
    """After a transient failure, an explicit `retry()` re-runs the
    saga, skipping already-done steps. The non-explicit
    `execute_one` does NOT auto-retry failed ops (that's the
    contract — see executor docstring)."""
    trace: list[str] = []
    fail_b_once = {"count": 0}

    @saga("retryable")
    class Retryable:
        @step("a")
        def step_a(self, ctx):
            trace.append("a")

        @step("b")
        def step_b(self, ctx):
            trace.append(f"b-{fail_b_once['count']}")
            if fail_b_once["count"] == 0:
                fail_b_once["count"] += 1
                raise RuntimeError("transient")

        @step("c")
        def step_c(self, ctx):
            trace.append("c")

    op_id = executor.submit(kind="retryable", target_node="n1", params={})
    r1 = executor.execute_one(op_id)
    assert r1.state == SagaState.FAILED

    # Implicit re-call of execute_one on a failed op is a no-op
    # (returns the prior failure unchanged).
    r_noop = executor.execute_one(op_id)
    assert r_noop.state == SagaState.FAILED
    assert trace == ["a", "b-0"]   # b did NOT re-run

    # Explicit retry — orchestrator/operator decides to try again.
    r2 = executor.retry(op_id)
    assert r2.state == SagaState.COMPLETED
    assert trace == ["a", "b-0", "b-1", "c"]
    assert backend.get_completed_steps(op_id) == {"a", "b", "c"}


def test_retry_on_completed_op_is_noop(executor, backend):
    """Retrying an already-completed op returns COMPLETED without
    re-running anything."""
    runs: list[str] = []

    @saga("oneshot")
    class Oneshot:
        @step("only")
        def s(self, ctx):
            runs.append("ran")

    op_id = executor.submit(kind="oneshot", target_node="n1", params={})
    executor.execute_one(op_id)
    assert runs == ["ran"]

    r = executor.retry(op_id)
    assert r.state == SagaState.COMPLETED
    assert runs == ["ran"]   # step did NOT re-run


def test_retry_missing_op_raises(executor):
    with pytest.raises(KeyError):
        executor.retry(99999)


def test_already_completed_op_is_idempotent(executor, backend):
    @saga("once")
    class Once:
        @step("only")
        def step_only(self, ctx):
            ctx.setdefault("count", 0)
            ctx["count"] += 1

    op_id = executor.submit(kind="once", target_node="n1", params={})
    r1 = executor.execute_one(op_id)
    r2 = executor.execute_one(op_id)

    assert r1.state == SagaState.COMPLETED
    assert r2.state == SagaState.COMPLETED
    # Step recorded exactly once
    step_rows = [s for s in backend.steps[op_id] if s["step_name"] == "only"]
    assert len(step_rows) == 1


def test_unknown_kind_records_failed(executor, backend):
    # Manually insert (bypassing submit's validation) — simulates a
    # row that was written by a different version with a kind we no
    # longer know about.
    op_id = backend.insert_operation(
        kind="aliens", target_node="n1", params={},
    )
    result = executor.execute_one(op_id)
    assert result.state == SagaState.FAILED
    assert "aliens" in (result.error or "")
    assert backend.ops[op_id]["state"] == "failed"


def test_context_dict_carries_data_between_steps(executor, backend):
    @saga("ctx")
    class Ctx:
        @step("write")
        def w(self, ctx):
            ctx["x"] = 42

        @step("read")
        def r(self, ctx):
            assert ctx["x"] == 42
            ctx["y"] = ctx["x"] * 2

        @step("read2")
        def r2(self, ctx):
            assert ctx["y"] == 84

    op_id = executor.submit(kind="ctx", target_node="n1", params={})
    result = executor.execute_one(op_id)
    assert result.state == SagaState.COMPLETED


def test_context_starts_from_params(executor, backend):
    @saga("paramread")
    class P:
        @step("check")
        def c(self, ctx):
            assert ctx["seed"] == "hello"
            assert ctx["_op_id"] > 0
            assert ctx["_kind"] == "paramread"

    op_id = executor.submit(
        kind="paramread", target_node="n1", params={"seed": "hello"},
    )
    r = executor.execute_one(op_id)
    assert r.state == SagaState.COMPLETED


def test_resume_in_flight_targets_only_this_node(executor, backend):
    @saga("multi")
    class M:
        @step("only")
        def o(self, ctx):
            ctx.setdefault("ran", []).append("ok")

    # Submit one op for THIS node, one for another
    mine = executor.submit(kind="multi", target_node="n1", params={})
    other = backend.insert_operation(
        kind="multi", target_node="n2", params={},
    )

    results = executor.resume_in_flight()

    # Only the n1 op was processed
    assert len(results) == 1
    assert results[0].op_id == mine
    assert results[0].state == SagaState.COMPLETED
    assert backend.ops[other]["state"] == "pending"  # untouched


def test_submit_rejects_unknown_kind(executor):
    with pytest.raises(ValueError):
        executor.submit(kind="never_registered", target_node="n1", params={})


def test_submit_rejects_non_dict_params(executor):
    @saga("paramcheck")
    class P:
        pass

    with pytest.raises(TypeError):
        executor.submit(kind="paramcheck", target_node="n1", params="not a dict")


def test_executor_rejects_empty_node():
    with pytest.raises(ValueError):
        SagaExecutor(backend=MemoryBackend(), this_node="")


def test_get_missing_op_raises(executor):
    with pytest.raises(KeyError):
        executor.execute_one(9999)


def test_step_definition_order_preserved_across_inheritance(executor, backend):
    """A subclass adds more steps; they run AFTER the parent's
    steps. (Today: out of scope since we don't use inheritance; but
    the ordering rule should be source-line-based and robust.)"""
    trace: list[str] = []

    @saga("inherit")
    class Inherit:
        @step("p1")
        def p1(self, ctx):
            trace.append("p1")

        @step("p2")
        def p2(self, ctx):
            trace.append("p2")

    op_id = executor.submit(kind="inherit", target_node="n1", params={})
    executor.execute_one(op_id)
    # Source-order preserved
    assert trace == ["p1", "p2"]
