# bedrock_d/orchestrator/sagas/rqlite_backend.py

The production storage adapter for the saga executor. It exposes a single class,
`RqliteSagaBackend`, that wraps `installer/lib/rqlite_client.RqliteClient` to
satisfy the `SagaBackend` protocol declared in `executor.py`. This is where a
saga's durable bookkeeping lives: the executor calls into this backend to create
an operation row, advance its state, record which steps have finished, and — on
restart — discover which operations are still in flight so they can be resumed.
One instance is constructed per `bedrock-d` process and handed to the executor;
thread safety comes from the underlying `RqliteClient`'s connection pool. Every
statement is parameterised (no string interpolation of values). It depends on
the `operations` and `operation_steps` tables defined in
`installer/lib/bedrock_schema.sql`, which must be applied before use.

## Functions / Classes

### `class RqliteSagaBackend`
SagaBackend implementation backed by rqlite. Stores the injected client and
issues SQL against the `operations` / `operation_steps` tables.
- **In:** `__init__(client)` — a `rqlite_client.RqliteClient`.
- **Out:** an adapter object the executor uses for all saga persistence.

### `insert_operation(*, kind, target_node, params, requested_by="") -> int`
Create a new pending operation row and return its id.
- **In:** `kind` — saga type string; `target_node` — node that should run it (may
  be NULL-able by caller); `params` — dict of saga inputs; `requested_by` —
  optional originator string.
- **Out:** the integer `operations.id`. Side effect: one INSERT row in
  `operations` with `state='pending'`, `params` stored as sorted-key JSON, and
  `created_at`/`updated_at` set to the current Unix second.

### `get_operation(op_id) -> Optional[dict]`
Fetch one operation by id.
- **In:** `op_id` — operation id.
- **Out:** a dict of the full row (`id, kind, target_node, params, state,
  requested_by, error, created_at, updated_at, completed_at`) or `None`. `params`
  is left as the raw TEXT string; the executor does the `json.loads()`.

### `update_operation_state(op_id, state, *, error=None) -> None`
Advance an operation's state.
- **In:** `op_id`; `state` — a `SagaState` (or anything with `.value` / `str()`);
  `error` — optional error text.
- **Out:** none. Side effect: one UPDATE on `operations`. Branches on state —
  `completed` clears `error` and stamps `completed_at`; otherwise sets `error`
  only when supplied. `updated_at` always refreshed.

### `list_inflight_for(node) -> list[dict]`
Return operations not yet finished that this node should run.
- **In:** `node` — node name.
- **Out:** list of full operation row dicts with `state IN ('pending',
  'in_progress')` and `target_node` equal to `node` or NULL, ordered by id
  ascending. Read-only.

### `get_completed_steps(op_id) -> set[str]`
Return the names of steps already done for an operation.
- **In:** `op_id`.
- **Out:** a set of `step_name` strings from `operation_steps` rows with
  `state='done'`. Read-only.

### `record_step_done(op_id, step_name, *, started_at, finished_at) -> None`
Mark a step finished.
- **In:** `op_id`; `step_name`; `started_at` / `finished_at` — Unix timestamps.
- **Out:** none. Side effect: one `INSERT OR REPLACE` into `operation_steps` with
  `state='done'` and `error` cleared.

### `record_step_failed(op_id, step_name, *, error, started_at, finished_at) -> None`
Mark a step failed.
- **In:** `op_id`; `step_name`; `error` — failure text; `started_at` /
  `finished_at` — Unix timestamps.
- **Out:** none. Side effect: one `INSERT OR REPLACE` into `operation_steps` with
  `state='failed'` and `error` stored.

## How it works

The backend is pure persistence — no orchestration logic of its own. The
executor drives the lifecycle and calls these methods at the right moments:

```
insert_operation        → operations row, state='pending'
   │
update_operation_state  → 'in_progress'
   │
   for each step:
     record_step_done / record_step_failed → operation_steps row
   │
update_operation_state  → 'completed' (clears error, sets completed_at)
                          or 'failed' (stores error)
```

Two pieces carry the load-bearing weight:

**Getting the new id without RETURNING.** rqlite has no `RETURNING`, so
`insert_operation` issues the INSERT and reads `last_insert_id` from the first
per-statement result dict returned by `RqliteClient.execute`. If that value is
missing/falsy, it falls back to a `SELECT id ... WHERE kind=? AND created_at=?
AND requested_by=? ORDER BY id DESC LIMIT 1`, taking the highest id if more than
one row matches within the same second. If even the fallback finds nothing it
raises `RuntimeError`.

**Crash-resume.** State and step rows are the saga's durable memory. On restart
the executor calls `list_inflight_for(node)` to find every `pending` /
`in_progress` operation for this node, then `get_completed_steps(op_id)` to learn
which steps it can skip and where to pick up. `record_step_done` /
`record_step_failed` use `INSERT OR REPLACE` so a retry after a crash — where the
step row was already written but the executor died before refreshing its
in-memory done-set — overwrites cleanly rather than colliding on the
`(op_id, step_name)` key.

`list_inflight_for` deliberately includes rows where `target_node IS NULL`
("any node") in every node's list, so the first node to pick one up runs it.

## Why

rqlite is the single durable record of saga progress, so resumability is just
"read the rows back and skip the done steps" — no separate journal. Parameterised
statements throughout keep saga params (arbitrary operator/caller input) out of
the SQL text.
