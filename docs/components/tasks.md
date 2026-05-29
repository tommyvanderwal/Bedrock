# Task registry

A single in-process registry in the mgmt app tracks every operation that
takes more than a second. Long-running REST endpoints return **202
Accepted + `task_id`** immediately and do the real work in the
background; the dashboard reads a WebSocket `task` channel for live
progress so the UI stays snappy regardless of backend wall-clock.

**Source:** `mgmt/tasks.py` (registry) + the `/api/tasks` endpoints and
202-returning handlers in `mgmt/app.py`. WS framing is `mgmt/ws.py`.

## What is tracked

| Action | Endpoint | Task type | Typical duration |
|---|---|---|---|
| Convert cattle ↔ pet ↔ vipet | `POST /api/vms/{name}/ha-level` | `vm.set_ha_level` | 30 s – 2 min (per disk) |
| Create VM (cattle/pet/vipet) | `POST /api/vms` | `vm.create` | 5 – 120 s |
| Create VM from a finished import | `POST /api/imports/{id}/create-vm` | `vm.create_from_import` | 30 s – 5 min |
| Delete VM | `DELETE /api/vms/{name}` | `vm.delete` | 2 – 15 s |
| Back up a VM (Kopia) | `POST /api/vms/{name}/backup` | `vm.backup` | 10 s – minutes |
| Restore a VM (Kopia) | `POST /api/vms/{name}/restore` | `vm.restore` | 10 s – minutes |

Not task-backed:

- **Live migrate** (`POST /api/vms/{name}/migrate`) runs the `vm_migrate`
  saga **synchronously** and returns its result inline. The saga is its
  own crash-resumable unit, so it doesn't ride the registry.
- **Import disk-image convert** (`POST /api/imports/{id}/convert`,
  virt-v2v) runs in the background but reports through the import job's
  own `status` field (`uploaded → converting → ready/failed`), not the
  registry.
- Short actions — start / shutdown / poweroff / force-stop, cdrom eject —
  are synchronous RPC/REST calls; a ~100 ms round-trip is already fast
  enough and they gain nothing from step-level reporting.

## Lifecycle

```
  POST /api/vms/foo/ha-level { vm_type: "pet" }
       │
       │ validate + compute current type (synchronous, may 4xx)
       │
       │ task = registry.create("vm.set_ha_level", "VM foo: cattle → pet",
       │                         vm_name="foo", node=<src>)
       │ asyncio.create_task(_runner())       ← background
       │
       ▼
  202 Accepted { "status": "accepted",
                 "task_id": "t-1776614156-e3dd2d",
                 "from": "cattle", "to": "pet" }

  ── in the background ──────────────────────────────────
  _runner:
      run_in_executor(None, _vm_set_ha_level, ...)   (paramiko/subprocess)
        task.step_start("disk0 (vda): create meta LV on source")
        … do the work …
        task.step_done("disk0 (vda): create meta LV on source")
        task.step_start("disk0 (vda): blockcopy → /dev/drbd1000")
        … do the work …
        task.step_done("disk0 (vda): blockcopy → /dev/drbd1000")
        …
      task.succeed()   (or task.fail("500: blockcopy failed …"))
```

Every mutation (`step_start`, `step_done`, `step_fail`, `step_progress`,
`set_progress`, `log`, `succeed`, `fail`) broadcasts on the WS `task`
channel so the dashboard updates in real time.

## Task shape

`/api/tasks` and `/api/tasks/{id}` return `_serialize(task)` — the `Task`
dataclass minus its internal rollback stack:

```json
{
  "id": "t-1776614156-e3dd2d",
  "type": "vm.delete",
  "subject": "Delete VM md-test (2 disks)",
  "state": "succeeded",
  "progress": 100,
  "started_at": "2026-04-19T15:55:56Z",
  "updated_at": "2026-04-19T15:56:01Z",
  "ended_at":  "2026-04-19T15:56:01Z",
  "error": null,
  "steps": [
    { "name": "destroy VM",                                   "state": "done", "duration_ms": 0    },
    { "name": "undefine on bedrock-sim-1.bedrock.local",      "state": "done", "duration_ms": 1000 },
    { "name": "disk0 teardown on bedrock-sim-1.bedrock.local","state": "done", "duration_ms": 0    },
    { "name": "disk1 teardown on bedrock-sim-1.bedrock.local","state": "done", "duration_ms": 0    }
  ],
  "log_tail": "...",
  "vm_name": "md-test",
  "import_id": null,
  "node": null
}
```

- `state`: `pending | running | succeeded | failed | cancelled`.
- Each step has its own `state`: `pending | running | done | failed |
  skipped`, plus `started_at`, `ended_at`, `duration_ms`, `progress`,
  `error`.
- `vm_name` / `import_id` / `node` are index fields the UI filters on.
- `log_tail` is capped at `LOG_TAIL_MAX = 4000` chars (keeps broadcast
  payloads bounded).

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | Active + recently-finished tasks. Fresh-load snapshot. |
| GET | `/api/tasks/{id}` | One task (full `log_tail`). 404 if aged out or unknown. |

`id` is `t-<unix-timestamp>-<6 hex chars>`. Finished tasks
(`succeeded`/`failed`/`cancelled`) age out of the registry after
`RETAIN_FINISHED_S = 900` s (15 min); the durable history of the
underlying actions lives in VictoriaLogs via `push_log`.

Both endpoints sit on the operator-authed mgmt API (8443 HTTPS).

## WS channel

The mgmt WS is a single multiplexed stream: every connected client
receives every frame and filters on `channel`. Each task mutation emits
one frame on channel `task`:

```json
{ "channel": "task",
  "event":   "task.create" | "task.update",
  "task":    { ...full serialized task as above... } }
```

The dashboard merges these into a local `tasks` Svelte store keyed by
`id`, so the task drawer + per-VM banners re-render automatically.

## Rollback helper

`Task.rollback(fn)` lets a multi-step worker register a compensating
action. On failure, `registry._complete(task, error=…)` walks the
rollback stack in **reverse** before marking the task `failed`:

```python
task.rollback(lambda: ssh_cmd_rc(host, f"lvremove -f {lv_path}"))
```

A rollback that itself throws is logged and skipped, so one bad
compensation can't strand the rest of the unwind.

## Concurrency

- Handlers create the task on the main event loop, then push the
  blocking work through `loop.run_in_executor(None, …)` so
  paramiko/subprocess calls never freeze the loop.
- Registry internals use a `threading.Lock` because executor threads
  call back in with step updates.
- Broadcasts are marshalled onto the main loop via
  `asyncio.run_coroutine_threadsafe(hub.broadcast("task", …))` — same
  pattern as `push_log`. If the loop/hub aren't wired yet, the broadcast
  is a no-op.
- The registry is in-memory. If `bedrock-d` restarts mid-operation, its
  in-flight tasks are orphaned; the next state tick reconciles reality
  (the VM either did or didn't end up converted) and the underlying saga,
  where one exists, crash-resumes from its own operations row.

## UI placement

```
  Sidebar brand row   [ Bedrock  ⏳ 2  ● online ]   ← badge when active>0
                                │
                                ▼ click
  ┌──────────────── Task drawer (right-hand, 380 px) ───────────────┐
  │ TASKS                                                     ×      │
  │ ┌─────────────────────────────────────────────────────────────┐ │
  │ │ RUNNING  VM win2016: cattle → pet           12 s ago         │ │
  │ │   ● disk0 (vda): create meta LV on source      900 ms         │ │
  │ │   ● disk0 (vda): create-md + up                1.1 s          │ │
  │ │   ○ disk0 (vda): blockcopy → /dev/drbd1000    47 %            │ │
  │ └─────────────────────────────────────────────────────────────┘ │
  │ ┌─────────────────────────────────────────────────────────────┐ │
  │ │ DONE     Delete VM md-test (2 disks)         5 min ago       │ │
  │ │   ● destroy VM                                ok             │ │
  │ │   ● undefine on bedrock-sim-1.bedrock.local   1.0 s           │ │
  │ │   ● disk0 teardown on bedrock-sim-1…          ok              │ │
  │ │   ● disk1 teardown on bedrock-sim-1…          ok              │ │
  │ └─────────────────────────────────────────────────────────────┘ │
  └──────────────────────────────────────────────────────────────────┘
```

The badge count reflects only `running` + `pending` tasks. Completed
tasks stay in the registry (and drawer) for 15 minutes so the operator
sees "this just worked" after clicking away. A failed task's `error` is
shown inline.

## Extending

To add a new task-backed action:

```python
@app.post("/api/vms/{name}/do-something")
async def api_vm_do_something(name: str, req):
    # validate synchronously so bad input fails fast (4xx)
    ...
    task = task_registry().create(
        "vm.do_something", f"VM {name}: do something",
        vm_name=name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _do_something, name, req, task)
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id}


def _do_something(name, req, task):
    task.step_start("phase one")
    ssh_cmd(...); task.step_done("phase one")
    task.step_start("phase two")
    ssh_cmd(...); task.step_done("phase two")
    return {"result": ...}
```

Three rules:

1. **Validate up front, before creating the task.** The client gets a
   4xx for bad input; the task list doesn't fill with invalid garbage.
2. **`step_start` before a slow call, `step_done` after.** Keeps the
   drawer's "what is it doing right now?" accurate.
3. **Let exceptions escape to the `_runner` wrapper** — it catches and
   calls `task.fail(...)` so the registry records the failure correctly.
