# mgmt/tasks.py

In-process registry for any orchestrator operation that takes more than a second
(VM convert, migrate, import convert, delete, etc.). It is a process-singleton
living inside the mgmt asyncio process: REST action endpoints in `app.py` create
a `Task`, return `202 + {task_id}` immediately, and run the real work as an
asyncio task that calls the task's step/log/progress helpers from executor
threads. Every mutation broadcasts on the WebSocket `task` channel so dashboards
update live; `/api/tasks` (also in `app.py`) serves a snapshot to fresh-join
clients. State is in-memory only — nothing here is persisted, and a mgmt restart
drops all live tasks (the next state tick reconciles reality, and history lives
in VictoriaLogs via `push_log`).

## Functions / Classes

### `TaskStep` (dataclass)
One unit of work inside a task.
- **Fields:** `name`; `state` (`pending | running | done | failed | skipped`,
  default `pending`); `started_at` / `ended_at` (ISO-Z strings); `duration_ms`;
  `progress` (0–100); `error`. Pure data; mutated only through the registry.

### `Task` (dataclass)
A single tracked long-running operation plus the helpers a worker calls to drive it.
- **Fields:** `id`; `type` (e.g. `vm.convert`, `vm.migrate`, `import.convert`);
  `subject` (human one-liner); `state` (`pending | running | succeeded | failed |
  cancelled`, default `running`); `started_at` / `updated_at` / `ended_at`;
  `progress`; `error`; `steps` (list of `TaskStep`); `log_tail` (capped string);
  index fields `vm_name` / `import_id` / `node`; and private `_rollback_stack`
  (not serialized).
- **Methods** (all thread-safe via the registry lock; all auto-broadcast
  `task.update` except `rollback`):
  - `step_start(name) -> TaskStep` — start (or reset, on retry) a named step.
  - `step_done(name, progress=None)` — mark a step `done`.
  - `step_fail(name, error)` — mark a step `failed`.
  - `step_progress(name, progress)` — set a step's progress.
  - `set_progress(progress)` — set the task-level progress.
  - `log(line)` — append a line to `log_tail`.
  - `rollback(fn)` — register a compensating action (no broadcast); runs in
    reverse order if the task later fails.
  - `succeed()` — complete with no error.
  - `fail(error)` — complete with an error (triggers rollback).

### `class TaskRegistry`
In-memory task store and WS broadcaster. Module-singleton.
- `wire(main_loop, hub_broadcast)` — record the asyncio loop and the hub
  broadcast coroutine so worker threads can marshal broadcasts onto the loop.
  - **In:** `main_loop` (the mgmt event loop); `hub_broadcast` (async
    `(channel, payload)` callable).
  - **Out:** none; sets internal refs. Called by `app.py` at startup (the loop
    does not exist at import time).
- `create(type, subject, **index) -> Task` — make and register a task.
  - **In:** `type`, `subject`; optional `index` keys `vm_name`, `import_id`, `node`.
  - **Out:** a live `Task` with id `t-<unixsec>-<6 hex>`; stored under the lock;
    broadcasts `task.create`.
- `get(task_id) -> Task | None` — look up by id under the lock.
- `list() -> list[dict]` — snapshot of active + recently-finished tasks.
  - **Out:** list of serialized task dicts (steps inlined, `_rollback_stack`
    stripped), sorted newest-first by `started_at`. Side effect: ages out
    finished tasks whose end time is older than `RETAIN_FINISHED_S`, deleting
    them from the store.

### `registry() -> TaskRegistry`
Accessor for the module-singleton registry instance.

### Module-level
- `RETAIN_FINISHED_S = 900` — finished tasks stay visible 15 minutes.
- `LOG_TAIL_MAX = 4000` — `log_tail` is truncated to its last 4000 chars.
- `_now()`, `_serialize(t)` — private: UTC ISO-Z timestamp; dataclass→dict with
  `_rollback_stack` dropped and steps inlined.
- `_registry` — the singleton `TaskRegistry`.

## How it works

A worker never mutates a `Task` directly; the `Task` helper methods delegate to
`_registry` mutators that take the lock, change the field, stamp `updated_at`,
release the lock, then broadcast. Every mutator follows the same shape:

```
with self._lock:        # serialize against other worker threads
    mutate task / step
    task.updated_at = _now()
self._broadcast(...)     # outside the lock
```

**Steps.** `_step_start` is retry-safe: if a step of that name already exists it
is reset to `running` (clears `ended_at`, `duration_ms`, `progress`, `error`)
rather than appended again. `_step_set` finds the step by name and is a no-op if
absent; when it transitions a step to a terminal state (`done | failed |
skipped`) it stamps `ended_at` and computes `duration_ms` from the timestamp
pair (any parse error is swallowed, leaving `duration_ms` unset).

**Completion + rollback.** `_complete` runs the rollback stack *before* taking
the lock: if the task failed and a rollback stack exists, each registered `fn`
is called in reverse registration order, with any exception logged
(`bedrock.tasks` warning) and skipped so one failed compensation does not abort
the rest. Then under the lock it sets `state` to `succeeded`/`failed`, records
`error` and `ended_at`, sets `progress=100` on success (left as-is on failure),
and clears the rollback stack. One `task.update` broadcast follows.

```
worker: t = registry().create("vm.convert", "VM foo: cattle → pet", vm_name="foo")
        t.rollback(undo_lv); t.step_start("alloc-lv")
        ... work ...                          ── on exception ──┐
        t.step_done("alloc-lv"); t.succeed()                    │
                                                                ▼
                                          t.fail(err): run rollback stack
                                          reversed → mark failed → broadcast
```

**Broadcasting.** `_broadcast` is a no-op until `wire()` has set both
`_main_loop` and `_hub_broadcast`. It serializes the task and schedules
`hub_broadcast("task", {"event": ..., "task": ...})` on the main loop via
`asyncio.run_coroutine_threadsafe`, so executor-thread workers safely reach the
asyncio WS hub. Failures to schedule are logged and swallowed — a dropped
broadcast never breaks the work.

```
worker thread                          main asyncio loop
  t.step_done(...)                          hub.broadcast("task", payload)
        │                                          ▲
        ▼  (under _lock: mutate fields)            │
  _broadcast("task.update", t)                     │
        └── asyncio.run_coroutine_threadsafe(──────┘
```

**Ageing.** Liveness pruning happens lazily inside `list()`: each call walks the
store, and any terminal task whose end timestamp (`ended_at`, falling back to
`updated_at`) is older than the cutoff is removed. There is no background
sweeper, so a never-read `/api/tasks` leaves finished tasks resident until the
next read.

## Why

In-memory and unpersisted is deliberate: tasks track *in-flight* work for the
live UI, and durable history is already captured in VictoriaLogs, so a restart
orphaning live tasks (reconciled by the next state tick) is acceptable rather
than worth a persistence layer. Broadcasts are marshalled onto the main loop
because the registry is mutated from executor threads but the WS hub is
asyncio-only — the same pattern as `push_log`.
