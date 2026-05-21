"""Tests for FileSagaBackend.

Covers:
- protocol round-trip via the SagaExecutor (same test surface as
  MemoryBackend in test_saga_executor.py)
- atomic writes survive crash mid-write (we don't simulate crash;
  we just verify the tmp+rename pattern doesn't leave debris)
- persistence: reopen the file from a fresh backend instance and
  see the same state
- the JSON-on-disk shape matches the documented format
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bedrock_d.orchestrator.sagas import (  # noqa: E402
    SAGAS,
    FileSagaBackend,
    SagaExecutor,
    SagaState,
    saga,
    step,
)


@pytest.fixture(autouse=True)
def _clear_registry():
    saved = dict(SAGAS)
    SAGAS.clear()
    yield
    SAGAS.clear()
    SAGAS.update(saved)


@pytest.fixture
def backend(tmp_path):
    return FileSagaBackend(path=tmp_path / "saga.json")


@pytest.fixture
def executor(backend):
    return SagaExecutor(backend=backend, this_node="n1")


# ───────────────────────────────────────────────────────────────────
# Protocol parity with MemoryBackend
# ───────────────────────────────────────────────────────────────────


def test_file_backend_can_run_a_saga_end_to_end(executor, backend):
    @saga("fb_happy")
    class Happy:
        @step("a")
        def s_a(self, ctx):
            ctx["a_ran"] = True

        @step("b")
        def s_b(self, ctx):
            assert ctx.get("a_ran") is True

    op_id = executor.submit(kind="fb_happy", target_node="n1",
                             params={"seed": "x"})
    r = executor.execute_one(op_id)

    assert r.state == SagaState.COMPLETED
    assert backend.get_completed_steps(op_id) == {"a", "b"}
    op = backend.get_operation(op_id)
    assert op["state"] == "completed"


def test_file_backend_failure_recorded(executor, backend):
    @saga("fb_boom")
    class B:
        @step("only")
        def s(self, ctx):
            raise RuntimeError("nope")

    op_id = executor.submit(kind="fb_boom", target_node="n1", params={})
    r = executor.execute_one(op_id)
    assert r.state == SagaState.FAILED
    op = backend.get_operation(op_id)
    assert op["state"] == "failed"
    assert "nope" in (op["error"] or "")


# ───────────────────────────────────────────────────────────────────
# Persistence: state survives reopening the file
# ───────────────────────────────────────────────────────────────────


def test_state_survives_reopen(tmp_path):
    path = tmp_path / "persist.json"
    @saga("fb_persist")
    class P:
        @step("a")
        def a(self, ctx):
            pass

    b1 = FileSagaBackend(path=path)
    e1 = SagaExecutor(backend=b1, this_node="n1")
    op_id = e1.submit(kind="fb_persist", target_node="n1", params={"k": 1})
    e1.execute_one(op_id)

    # Reopen — simulates bedrock-d restart
    b2 = FileSagaBackend(path=path)
    op = b2.get_operation(op_id)
    assert op is not None
    assert op["state"] == "completed"
    assert op["params"] == {"k": 1}
    assert b2.get_completed_steps(op_id) == {"a"}


def test_resume_after_simulated_crash(tmp_path):
    """Submit a saga; have it complete step a; pretend bedrock-d
    crashed; reopen the file; resume saga; step a is skipped, step
    b runs."""
    path = tmp_path / "crash.json"
    trace: list[str] = []

    @saga("fb_crash")
    class C:
        @step("a")
        def a(self, ctx):
            trace.append("a")

        @step("b")
        def b(self, ctx):
            trace.append("b")

    b1 = FileSagaBackend(path=path)
    e1 = SagaExecutor(backend=b1, this_node="n1")
    op_id = e1.submit(kind="fb_crash", target_node="n1", params={})

    # Simulate crash AFTER step a (record it manually, leave op in_progress)
    b1.record_step_done(op_id, "a", started_at=0, finished_at=1)
    b1.update_operation_state(op_id, SagaState.IN_PROGRESS)

    # bedrock-d restart: new backend instance, new executor
    b2 = FileSagaBackend(path=path)
    e2 = SagaExecutor(backend=b2, this_node="n1")
    # resume_in_flight finds the op and resumes
    results = e2.resume_in_flight()
    assert len(results) == 1
    assert results[0].state == SagaState.COMPLETED
    assert trace == ["b"]   # a was skipped


# ───────────────────────────────────────────────────────────────────
# On-disk shape — load the JSON and assert documented structure
# ───────────────────────────────────────────────────────────────────


def test_on_disk_shape(tmp_path):
    path = tmp_path / "shape.json"
    @saga("fb_shape")
    class S:
        @step("only")
        def s(self, ctx):
            pass

    b = FileSagaBackend(path=path)
    e = SagaExecutor(backend=b, this_node="n1")
    op_id = e.submit(kind="fb_shape", target_node="n1", params={"foo": "bar"})
    e.execute_one(op_id)

    on_disk = json.loads(path.read_text())
    assert set(on_disk.keys()) == {"next_id", "ops", "steps"}
    assert on_disk["next_id"] == op_id + 1
    assert str(op_id) in on_disk["ops"]
    op = on_disk["ops"][str(op_id)]
    assert op["kind"] == "fb_shape"
    assert op["target_node"] == "n1"
    assert op["state"] == "completed"
    assert op["params"] == {"foo": "bar"}
    assert isinstance(op["created_at"], int)
    assert isinstance(op["updated_at"], int)

    steps = on_disk["steps"][str(op_id)]
    assert len(steps) == 1
    assert steps[0]["step_name"] == "only"
    assert steps[0]["state"] == "done"


def test_atomic_write_leaves_no_tmp_debris(tmp_path):
    """After many mutations, the directory should have only the
    .json file — no leftover .tmp files."""
    path = tmp_path / "atomic.json"

    @saga("fb_atomic")
    class A:
        @step("only")
        def s(self, ctx):
            pass

    b = FileSagaBackend(path=path)
    e = SagaExecutor(backend=b, this_node="n1")
    for _ in range(5):
        op_id = e.submit(kind="fb_atomic", target_node="n1", params={})
        e.execute_one(op_id)

    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["atomic.json"], f"unexpected debris: {files}"


def test_inflight_filter_targets_only_this_node(tmp_path):
    path = tmp_path / "filter.json"
    @saga("fb_inflight")
    class I:
        @step("only")
        def s(self, ctx):
            pass

    b = FileSagaBackend(path=path)
    # Two pending ops, one for n1 and one for n2 (manually inserted
    # to keep them pending — submit-then-execute would complete them)
    n1_id = b.insert_operation(
        kind="fb_inflight", target_node="n1", params={})
    n2_id = b.insert_operation(
        kind="fb_inflight", target_node="n2", params={})

    e = SagaExecutor(backend=b, this_node="n1")
    inflight = b.list_inflight_for("n1")
    assert len(inflight) == 1
    assert inflight[0]["id"] == n1_id
    inflight_n2 = b.list_inflight_for("n2")
    assert len(inflight_n2) == 1
    assert inflight_n2[0]["id"] == n2_id
