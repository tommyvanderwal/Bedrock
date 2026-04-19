# Task registry

A single in-process registry in the mgmt app tracks every operation that
takes more than a second. Long-running REST endpoints return **202
Accepted + `task_id`** immediately and do the real work in the
background; the dashboard subscribes to a WebSocket `task` channel for
live progress so the UI stays snappy regardless of backend wall-clock.

**Source:** `mgmt/tasks.py` + the `/api/tasks` endpoints in `mgmt/app.py`.

## What is tracked

| Action | Task type | Typical duration |
|---|---|---|
| Convert cattle ↔ pet ↔ ViPet | `vm.convert` | 30 s – 2 min (per disk) |
| Delete VM | `vm.delete` | 2 – 15 s |
| Create cattle VM | `vm.create` | 5 – 60 s |
| Create VM from import | `vm.create_from_import` | 30 s – 5 min |
| Import disk image convert (virt-v2v) | `import.convert` | 1 – 10 min |
| Live migrate | (sync; not task-backed — single virsh call) | ~3 s |

Short-running actions (start / shutdown / poweroff / cdrom eject) remain
synchronous; the 100 ms round-trip is already fast enough and they don't
benefit from step-level reporting.

## Lifecycle

```
  POST /api/vms/foo/convert { target_type: "pet" }
       │
       │ validate + compute current type
       │
       │ task = registry.create("vm.convert", "VM foo: cattle → pet",
       │                         vm_name="foo", node=<src>)
       │ asyncio.create_task(_runner())       ← background
       │
       ▼
  202 Accepted { "task_id": "t-1776614156-e3dd2d",
                 "from": "cattle", "to": "pet" }

  ── in the background ──────────────────────────────────
  _runner:
      task.step_start("disk0 (vda): create meta LV on source")
      … do the work …
      task.step_done("disk0 (vda): create meta LV on source")
      task.step_start("disk0 (vda): blockcopy → /dev/drbd1000")
      … do the work …
      task.step_done("disk0 (vda): blockcopy → /dev/drbd1000")
      …
      task.succeed()   (or task.fail("rc=1: blockcopy …"))
```

Every mutation (`step_start`, `step_done`, `set_progress`, `log`,
`succeed`, `fail`) broadcasts on WS `task` so the dashboard updates in
real time.

## Task shape

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

`state` is one of `pending | running | succeeded | failed | cancelled`.
Steps have their own state: `pending | running | done | failed | skipped`.

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/tasks` | Active + recently-finished tasks. Fresh-join snapshot. |
| GET | `/api/tasks/{id}` | One task (full `log_tail`). |

The `id` form is `t-<unix-timestamp>-<6 hex chars>`. Finished tasks
(`succeeded`/`failed`/`cancelled`) age out of `/api/tasks` after 15
minutes. History of the underlying actions is in VictoriaLogs via the
regular `push_log` stream.

## WS channel

Every task state change broadcasts one JSON frame:

```json
{ "channel": "task",
  "event":   "task.create" | "task.update",
  "task":    { ...full task object as above... } }
```

The dashboard merges these into a local `tasks` Svelte store keyed by
`id` so the task drawer + per-VM banners re-render automatically.

## Atomicity — rollback helpers

Long ops (`vm.convert`, multi-disk create) call `task.rollback(fn)` as
they go:

```python
task.rollback(lambda: ssh_cmd_rc(host, f"lvremove -f {lv_path}"))
```

If the outer step fails, `registry._complete(task, error=...)` walks the
rollback stack in **reverse** order. This is how multi-disk
`cattle → pet` unwinds disk 0's LVs on the peers when disk 2's
blockcopy blows up.

## Concurrency

- Tasks created from FastAPI handlers run on the main event loop; the
  blocking work inside them goes through `loop.run_in_executor(None, ...)`
  so paramiko/subprocess calls don't freeze the loop.
- Registry internals use a `threading.Lock` because worker threads call
  back in with step updates.
- Broadcasts are marshalled onto the main loop with
  `asyncio.run_coroutine_threadsafe(hub.broadcast(...))`, same pattern as
  `push_log`.
- Restart caveat: the registry is in-memory. If `bedrock-mgmt` restarts
  mid-convert, the in-flight tasks are orphaned. The next state tick will
  reconcile reality (the VM either did or didn't end up converted); no
  automatic resume today.

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
tasks stay visible for 15 minutes so the operator sees "this just worked"
after clicking away. Failed tasks' `error` is shown inline.

## Extending

Adding a new task-backed action:

```python
@app.post("/api/vms/{name}/do-something")
async def api_vm_do_something(name: str, req):
    # validate synchronously so bad input fails fast
    ...
    task = task_registry().create(
        "vm.do_something", f"VM {name}: do something",
        vm_name=name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _do_something, name, req, task)
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

1. **Validate up front, before creating the task.** Client sees 4xx for
   bad input; the task list doesn't fill with invalid garbage.
2. **Emit step_start before a slow call, step_done after.** Keeps the
   drawer's "what is it doing right now?" accurate.
3. **Let exceptions escape to the `_runner` wrapper** — it catches and
   calls `task.fail(...)` so the registry records the failure correctly.
