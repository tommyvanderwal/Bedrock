# mgmt/routes_operations.py

The generic saga-submission HTTP surface. It attaches the `/api/operations`
endpoints to the mgmt FastAPI app so an operator (via the CLI or dashboard) can
submit any registered saga by name, poll its state and step log, list past
operations, and retry a failed one. It is the uniform front door to the
`SagaExecutor`; `app.py` calls `register_routes(app, require_operator=…)` once
at startup and injects the operator-auth dependency. All endpoints are
operator-token gated.

## Functions / Classes

### `OpSubmit(BaseModel)`
Request body for a saga submission.
- **Fields:** `kind: str` (saga name), `target_node: Optional[str]` (defaults to
  this host), `params: dict` (saga inputs), `wait: bool = True` (block for
  completion vs. fire-and-return).

### `register_routes(app, *, require_operator) -> None`
Attaches the four `/api/operations` endpoints to the FastAPI app.
- **In:** `app` — the mgmt FastAPI instance; `require_operator` — FastAPI auth
  dependency from `app.py`, injected so this surface enforces the same
  operator-token gate as the rest of the mutating API.
- **Out:** none (registers routes as a side effect). Defines two nested helpers
  (`_executor`, `_load_all_vm_sagas`) and the four route handlers below.

The four registered endpoints (all `Depends(require_operator)`):

### `POST /api/operations` — `api_op_submit(req: OpSubmit) -> dict`
Submit, and by default wait on, a saga.
- **In:** `req: OpSubmit`. `_user` comes from the auth dependency.
- **Out:** if `wait=false`, `{op_id, state: "pending"}` (HTTP 202 semantics — the
  saga is queued only). If `wait=true`, runs it inline and returns
  `{op_id, kind, state, last_step, error}` where `state` is the executor result's
  `.state.value`. Side effects: writes `operations` / `operation_steps` rqlite
  rows via the executor, and (when waiting) runs the saga's steps, which fan out
  to whatever subprocess/file/service work that saga does.
- **Errors:** 400 if `req.kind` is not in `SAGAS` (lists known kinds), or if the
  executor rejects the submit (`TypeError`/`ValueError`); 503 if the executor
  fails to initialize.

### `POST /api/operations/{op_id}/retry` — `api_op_retry(op_id: int) -> dict`
Re-run a failed or stuck saga from its first not-`done` step.
- **In:** `op_id` path param.
- **Out:** `{op_id, state, last_step, error}`. Calls `ex.retry(op_id)`, which
  resets the op to `in_progress` and re-runs remaining steps; already-`done`
  steps are skipped, and an already-completed op is returned unchanged.
- **Errors:** 404 if `op_id` is unknown (`KeyError`); 503 if the executor fails
  to initialize.

### `GET /api/operations/{op_id}` — `api_op_get(op_id: int) -> dict`
Fetch one operation's record plus its step log.
- **In:** `op_id` path param.
- **Out:** `{op: <operation row>, steps: [<step rows>]}`. The op's `params`
  field (stored as JSON TEXT) is decoded into a real dict before returning.
  Steps come from `operation_steps` ordered by `started_at, step_name`. Reads
  rqlite only.
- **Errors:** 404 if no operation with that id.

### `GET /api/operations?kind=&state=&limit=` — `api_op_list(...) -> list`
List operations, newest first.
- **In:** optional `kind` and `state` filters (`state` ∈ {pending, in_progress,
  completed, failed}); `limit` defaults to 50.
- **Out:** list of operation rows (`id, kind, target_node, state, error,
  created_at, updated_at, completed_at, requested_by`). Reads rqlite only.

### Private helpers
- `_executor()` — builds a `SagaExecutor` over an `RqliteSagaBackend` wrapping a
  fresh `RqliteClient`, with `this_node = socket.gethostname()`. Lazily imports
  `bedrock_d` so test environments without it on `sys.path` don't pay the cost.
- `_load_all_vm_sagas()` — imports every saga module (`bedrock_d.vm.{create,
  destroy,grow,migrate}`, `bedrock_d.install.{cluster_init,node_join,node_leave,
  cluster_tier}`, `bedrock_d.cluster.rename`, `bedrock_d.orchestrator.replica_repair`)
  purely for the import side effect: each registers itself in the `SAGAS`
  registry. Called before submit and retry so the registry is populated.

## How it works

The module owns no saga logic of its own — it is a thin, uniform HTTP adapter in
front of the `SagaExecutor`. The load-bearing detail is import ordering and the
sync/async split.

```
POST /api/operations  (req: kind, target_node?, params, wait)
        │
        ├─ _load_all_vm_sagas()      # populate SAGAS registry (import side effect)
        ├─ kind in SAGAS ?           # else 400, lists known kinds
        ├─ ex = _executor()          # else 503
        ├─ target = target_node or gethostname()
        ├─ op_id = ex.submit(kind, target, params, requested_by=_user)
        │                            #   → writes operations row (state=pending)
        │
        ├─ wait == false ──────────► return {op_id, state: "pending"}   (queued only)
        │
        └─ wait == true
              result = ex.execute_one(op_id)     # runs steps inline
              return {op_id, kind, state, last_step, error}
```

Because saga modules register themselves in `SAGAS` at import time,
`_load_all_vm_sagas()` must run before any `kind`-validity check or retry; the
imports are lazy so importing this route module never drags in `bedrock_d`. The
top-of-module `sys.path.insert(0, parents[1])` makes `bedrock_d` importable when
this file is loaded inside the running bedrock-d process.

`submit` only records the operation; `execute_one` is what actually runs the
steps. So `wait=false` returns immediately with a `pending` op for the caller to
poll via the GET endpoint, while `wait=true` blocks through completion — keeping
the CLI's common case a single synchronous call. `retry` re-enters an existing
op rather than creating a new one: it resets to `in_progress` and runs from the
first step that is not yet `done`, so previously-succeeded steps are not redone.

The two GET endpoints read rqlite directly through fresh `RqliteClient`s (the
list query builds a parameterized `WHERE` from the optional filters and orders
by `id DESC`); the single-op GET additionally JSON-decodes the stored `params`
TEXT so callers get a real dict without double-decoding.

## Why

One generic submit/poll/list/retry surface over the executor means every saga —
VM create, node join/leave, tier create, rename, replica repair — is driven the
same way, so the CLI and dashboard need only one code path instead of a bespoke
endpoint per operation. Default `wait=true` keeps simple operator commands
synchronous; `wait=false` exists for long-running ops that should poll instead.
