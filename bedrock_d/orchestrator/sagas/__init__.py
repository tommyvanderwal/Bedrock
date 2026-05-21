"""Sagas — crash-safe multi-step cluster operations.

Name from Garcia-Molina '87: long-running ops that can't fit in one
atomic commit. See docs/codebase-rewrite-plan.md §3.2 for the why.
Pattern, briefly:

- Every long-running operation (cluster_init, node_join, vm_create,
  …) is a saga class with one or more `@step("name")`-decorated
  methods.
- A saga is submitted by writing an `operations` row to rqlite.
- The executor runs the saga's steps in declaration order. Each
  step is idempotent (its body checks "already done? → return").
- Each step's success/failure is recorded in `operation_steps`.
- On crash + restart, the executor finds in-flight sagas for this
  node and resumes from the first step that doesn't have a
  `operation_steps(op_id, step_name) state='done'` row.

The executor is the only thing in this package that talks to rqlite
(via the `SagaBackend` protocol). Individual saga handlers are pure
methods that mutate a shared `context` dict — no rqlite calls
inside step bodies, no global state.
"""
from .executor import (
    SAGAS,
    SagaBackend,
    SagaExecutor,
    SagaResult,
    SagaState,
    saga,
    step,
)
from .file_backend import FileSagaBackend

__all__ = [
    "SAGAS",
    "FileSagaBackend",
    "SagaBackend",
    "SagaExecutor",
    "SagaResult",
    "SagaState",
    "saga",
    "step",
]
