# bedrock_d/orchestrator/sagas/file_backend.py

`FileSagaBackend` is the saga-persistence backend used by the bootstrap sagas
that bring rqlite up — `cluster_init` (`bedrock init`) and `node_join`
(`bedrock join`). Those sagas can't store their progress in rqlite because
rqlite is the service they're starting, so this backend persists saga operation
and step state to a single local JSON file at
`/var/lib/bedrock/init-progress.json`. The `SagaExecutor` (in `executor.py`)
drives it through the `SagaBackend` protocol; on a CLI crash and restart, the
executor reloads the file and resumes from the first not-`done` step. Every
other saga uses `RqliteSagaBackend`, since rqlite is up by then.

## Functions / Classes

### `class FileSagaBackend`
A `SagaBackend` implementation persisting all state to one JSON file. Implements
the protocol the executor calls: insert/get/update operations, list in-flight
work for a node, and record per-step done/failed.

#### `__init__(self, path: Path = DEFAULT_PATH)`
Open (or create) the backing file.
- **In:** `path` — JSON file location; defaults to `DEFAULT_PATH`
  (`/var/lib/bedrock/init-progress.json`). Tests point it at a tmp dir.
- **Out:** sets `self.path`. Side effect: if the file does not exist, writes a
  fresh empty state `{"next_id": 1, "ops": {}, "steps": {}}` (creating parent
  dirs).

#### `insert_operation(self, *, kind, target_node, params, requested_by="") -> int`
Register a new saga operation as `pending`.
- **In:** `kind` — saga type (e.g. `cluster_init`, `node_join`); `target_node` —
  node the saga runs against (may be `None`); `params` — saga input dict
  (copied); `requested_by` — operator/caller label.
- **Out:** the new integer op id (taken from `next_id`). Side effect: writes the
  file with a new entry under `ops[op_id]` (state `pending`, `error` null,
  `created_at`/`updated_at` set to now) and an empty `steps[op_id]` list; bumps
  `next_id`.

#### `get_operation(self, op_id: int) -> Optional[dict]`
Fetch one operation row.
- **In:** `op_id`.
- **Out:** a copy of the op dict, or `None` if absent. No side effects.

#### `update_operation_state(self, op_id, state: SagaState, *, error=None) -> None`
Set an operation's state (and optionally its error).
- **In:** `op_id`; `state` — a `SagaState` (its `.value` is stored, else
  `str(state)`); `error` — optional error string.
- **Out:** none. Side effects: writes the file with the new `state` and a
  refreshed `updated_at`. If `error` is given it is stored; if `error` is `None`
  and the new state is `COMPLETED`, any stale error is cleared to `None`. Raises
  `KeyError(op_id)` if the op is unknown.

#### `list_inflight_for(self, node: str) -> list[dict]`
List unfinished operations relevant to a node.
- **In:** `node` — node name.
- **Out:** copies of ops whose state is `pending` or `in_progress` and whose
  `target_node` is `node` or `None`, sorted ascending by `id`. No side effects.

#### `get_completed_steps(self, op_id: int) -> set[str]`
Return the names of steps already finished for an op.
- **In:** `op_id`.
- **Out:** set of `step_name` values whose step state is `done` (empty if the op
  has no recorded steps). No side effects. This is the resume key — the executor
  skips these on a re-run.

#### `record_step_done(self, op_id, step_name, *, started_at, finished_at) -> None`
Mark a step as `done`.
- **In:** `op_id`; `step_name`; `started_at`/`finished_at` — unix timestamps.
- **Out:** none. Side effect: writes the file (delegates to `_record_step`).

#### `record_step_failed(self, op_id, step_name, *, error, started_at, finished_at) -> None`
Mark a step as `failed` with its error.
- **In:** `op_id`; `step_name`; `error` — failure message; `started_at`/`finished_at`.
- **Out:** none. Side effect: writes the file (delegates to `_record_step`).

### Private helpers
- `_load() -> dict` — `json.loads` of the file's text.
- `_write(state) -> None` — atomic full rewrite (see How it works).
- `_record_step(op_id, step_name, state, error, started_at, finished_at)` —
  upsert a step row: overwrite the existing row with matching `step_name`, else
  append; then `_write`.

## How it works

The whole state is one JSON object rewritten in full on every mutation —
acceptable because bootstrap sagas are rare (one init per cluster, one join per
node). Two top-level maps key everything off the operation id (stored as the
string of the integer):

```
{
  "next_id": 2,
  "ops":   { "1": { id, kind, target_node, params, state,
                    requested_by, error, created_at, updated_at } },
  "steps": { "1": [ { step_name, state, error,
                      started_at, finished_at }, ... ] }
}
```

Operation lifecycle, as driven by the executor:

```
insert_operation        -> ops[id].state = "pending"
update_operation_state  -> "in_progress"
   per step:  record_step_done / record_step_failed -> steps[id][*]
update_operation_state  -> "completed"  (clears stale error)
```

Two properties make this crash-safe:

- **Atomic writes.** `_write` serializes to a temp file in the same directory
  (`mkstemp` with the file's name as prefix), then `os.replace` swaps it into
  place — a single atomic rename, so a crash mid-write can never leave a
  half-written `init-progress.json`. On any write error the temp file is
  unlinked and the exception re-raised.
- **Idempotent resume.** `get_completed_steps` returns the `done` steps; the
  executor only runs steps not in that set. `_record_step` upserts by
  `step_name` rather than appending, so a crash after a step ran but before its
  row was durably written produces an overwrite (not a duplicate) on retry.

`list_inflight_for` lets the executor find work to resume after a restart:
`pending`/`in_progress` ops targeted at this node (or untargeted, where
`target_node` is `None`), oldest first.

## Why

The bootstrap sagas need durable, resumable progress before rqlite exists, so a
plain local JSON file with atomic rename gives crash-durability without any
running cluster service.
