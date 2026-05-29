# bedrock_d/orchestrator/sagas/executor.py

Crash-safe multi-step orchestration engine. A *saga* is a class of ordered,
idempotent steps; this module is the registry that collects saga classes and the
executor that runs one to completion, recording each step's progress in rqlite so
an interrupted saga can resume after a bedrock-d restart. The orchestrator loop
inside bedrock-d owns a `SagaExecutor` and drives it: it calls `submit` to queue
work, `execute_one` per queued operation, and `resume_in_flight` once at startup.
Persistence is abstracted behind the `SagaBackend` protocol — rqlite in
production, an in-memory dict in tests.

## Functions / Classes

### `saga(kind: str)`
Class decorator that registers a saga handler under a string `kind`.
- **In:** `kind` — the saga-kind label (e.g. `"cluster_init"`); must be a
  non-empty string.
- **Out:** a decorator that sets `cls._saga_kind = kind`, stores `cls` in the
  module-global `SAGAS` dict, and returns the class. Raises `ValueError` on an
  empty kind, or if the same kind is already registered to a *different* class.

### `step(name: str)`
Method decorator that marks a saga method as an ordered step.
- **In:** `name` — the `operation_steps.step_name` value persisted to rqlite and
  matched on resume; must be a non-empty string.
- **Out:** a decorator that sets `fn._saga_step_name = name` and returns the
  function. Raises `ValueError` on an empty name.

### `class SagaState(str, Enum)`
The four operation states: `PENDING`, `IN_PROGRESS`, `COMPLETED`, `FAILED`
(string values `"pending"` / `"in_progress"` / `"completed"` / `"failed"`).

### `class SagaResult` (frozen dataclass)
Return shape of an execution: `op_id: int`, `state: SagaState`,
`last_step: Optional[str]`, `error: Optional[str]`.

### `class SagaBackend(Protocol)`
The synchronous persistence interface the executor depends on. Production wires a
thin adapter over `installer.lib.rqlite_client`; tests pass an in-memory dict.
Methods: `insert_operation(*, kind, target_node, params, requested_by) -> int`,
`get_operation(op_id) -> Optional[dict]`,
`update_operation_state(op_id, state, *, error=None)`,
`list_inflight_for(node) -> list[dict]`,
`get_completed_steps(op_id) -> set[str]`,
`record_step_done(op_id, step_name, *, started_at, finished_at)`,
`record_step_failed(op_id, step_name, *, error, started_at, finished_at)`.

### `class SagaExecutor`
Runs a saga to completion (or failure), one step at a time, single-thread /
single-node.

#### `__init__(self, backend: SagaBackend, this_node: str)`
- **In:** `backend` — a `SagaBackend`; `this_node` — this node's name (used to
  filter which operations resume here); must be non-empty.
- **Out:** stores both on the instance. Raises `ValueError` on empty `this_node`.

#### `submit(self, *, kind, target_node, params, requested_by="") -> int`
Persist a new saga as a pending operation.
- **In:** `kind` — a registered saga kind; `target_node` — the node that should
  pick it up (`None` is treated as "this node"); `params` — a dict (becomes the
  step `context`); `requested_by` — optional audit string.
- **Out:** the new operation id (`int`). Side effect: one `insert_operation`
  backend call (an `operations` row in state `pending`). Raises `ValueError` for
  an unknown kind, `TypeError` if `params` is not a dict.

#### `execute_one(self, op_id: int) -> SagaResult`
Run or resume the operation identified by `op_id`. `COMPLETED` and `FAILED` are
terminal — re-calling on them returns the stored result without re-running.
- **In:** `op_id` — the operation id.
- **Out:** the final `SagaResult`. Side effects: reads the op, sets state to
  `IN_PROGRESS`, calls each not-yet-`done` step's function, and records each step
  (`record_step_done` / `record_step_failed`) plus the final
  `update_operation_state` (`COMPLETED` or `FAILED`). Raises `KeyError` if no op
  has that id; step exceptions are caught and returned as a `FAILED` result, not
  raised.

#### `retry(self, op_id: int) -> SagaResult`
Explicit "I reviewed the failure, run it again" knob.
- **In:** `op_id` — the operation id.
- **Out:** a `SagaResult`. Resets the op state to `IN_PROGRESS` (clearing
  `error`) and delegates to `execute_one`, which skips already-`done` steps. A
  `COMPLETED` op is returned untouched. Raises `KeyError` for an unknown id.

#### `resume_in_flight(self) -> list[SagaResult]`
Re-run every `pending` + `in_progress` operation targeted at this node.
- **In:** none (uses `self.this_node`).
- **Out:** the list of `SagaResult`s in processing order. Called once on
  bedrock-d startup. An executor-internal exception on one op is logged and
  recorded as a `FAILED` result rather than aborting the loop.

### `_ordered_steps(saga_cls) -> list[tuple[str, Callable]]`
Returns `[(step_name, unbound_method), …]` sorted by `__code__.co_firstlineno`,
i.e. source-declaration order. Reaches each method via the class so the
`_saga_step_name` annotation set by `@step` survives; skips non-callables and
unannotated attributes.

### `known_sagas() -> Iterable[str]`
Diagnostic helper; yields the registered kinds in sorted order.

## How it works

A saga class is registered at import time by `@saga("kind")`, and each
`@step("name")` method is collected by `_ordered_steps` in source order. Every
step body is itself idempotent — its first lines check "is this already done?"
and return early — so re-running a step is safe.

An operation is a row in rqlite (table `operations`) carrying its `kind`,
`target_node`, JSON `params`, and `state`; completed steps are rows in
`operation_steps`. The executor never touches that schema directly — all reads
and writes go through the injected `SagaBackend`.

`execute_one` flow:

```
get_operation(op_id)
  state == COMPLETED  ─► return stored result (terminal)
  state == FAILED     ─► return stored error  (terminal)
  unknown kind        ─► mark FAILED, return
  no steps            ─► mark COMPLETED, return   (degenerate "always succeed")
       │
       ▼
done = get_completed_steps(op_id)
ctx  = json.loads(params); ctx["_op_id"], ctx["_kind"] set
update_operation_state(IN_PROGRESS)
       │
       ▼   for (step_name, fn) in declaration order:
   step_name in done ? ── yes ─► skip (debug-log)
       │ no
       ▼
   started = now
   fn(instance, ctx) ── raises ─► record_step_failed
       │ ok                        update_operation_state(FAILED)
       ▼                           return FAILED(last_step, error)
   record_step_done(started, finished)
       │
       ▼  (all steps done)
update_operation_state(COMPLETED) ─► return COMPLETED
```

Resume after a restart: `context` is rebuilt fresh from `operations.params` each
run — mutations a step makes to `ctx` live only for the current run and are never
persisted. A later step that needs a value an earlier step derived must
re-derive it from rqlite; that re-derivation is the same logic as the step's
idempotency check.

Terminal `FAILED` is deliberate: `resume_in_flight` will not silently re-run a
saga that failed on a real bug. Re-running a failed saga is only possible through
the explicit `retry`, which flips the state back to `IN_PROGRESS`; previously
succeeded steps remain `done` and are skipped, so retry picks up at the first
incomplete step.

Concurrency is coarse: one node runs its sagas serially (the orchestrator calls
`execute_one` one at a time), and cross-node isolation comes from
`operations.target_node` — only the named node resumes its own rows via
`resume_in_flight`.

## Why

The shared `context` dict (rebuilt from `params`, never persisted between runs)
keeps the durable source of truth in rqlite alone: there is no separately-saved
intermediate state to drift from the database, so resume correctness reduces to
each step being idempotent. Keeping `FAILED` terminal unless an
operator/orchestrator explicitly retries avoids a crash-loop silently re-applying
a buggy step.
