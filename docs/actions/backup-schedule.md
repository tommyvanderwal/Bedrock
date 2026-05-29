# Schedule periodic backups

Per-VM cron schedule that fires a backup automatically. The schedule is
a JSON blob in the rqlite `vms.backup_schedule` column, so it survives
mgmt-master failover. Only the master node fires.

**Triggered by:**

- Dashboard: VM detail → Backups card → "Schedule" subsection →
  cron input field + Save schedule
- HTTP (8443 HTTPS, operator-authed):
  - `POST /api/vms/{name}/backup-schedule` body
    `{"target_id":"main", "cron_expr":"0 2 * * *",
      "label_prefix":"auto", "retention_count":0}`
  - `DELETE /api/vms/{name}/backup-schedule`
  - `GET  /api/cron/preview?expr=<cron>&n=5` — pure parser, no I/O

**Source:** `mgmt/app.py:api_vm_backup_schedule_set` /
`api_vm_backup_schedule_remove` / `api_cron_preview`,
`mgmt/cron.py` (parser),
`installer/lib/bedrock_state.py:backup_schedule_set` /
`backup_schedule_removed` (rqlite mutation),
`mgmt/orchestrator.py:backup_scheduler` + `_scheduler_tick` (runs inside
`bedrock-d`'s asyncio orchestrator),
`installer/lib/view_builder.py` (deserializes the `vms.backup_schedule`
JSON column into `vm["backup_schedule"]`).

## Cron syntax (UTC always)

```
m h dom mon dow
│ │ │   │   └── day-of-week  0..7  (0 and 7 = Sunday) | sun..sat
│ │ │   └────── month        1..12 | jan..dec
│ │ └────────── day-of-month 1..31
│ └──────────── hour         0..23
└────────────── minute       0..59
```

Each field accepts: `*`, `N`, `N,M,…`, `N-M`, `*/k`, `N-M/k`. Bedrock
also recognises the canonical presets: `@hourly`, `@daily`, `@weekly`,
`@monthly`, `@yearly` (`@annually` is an alias of `@yearly`). NOT
supported: Quartz extensions (`?`, `L`,
`W`, `#`) and a year field. Day-of-month vs day-of-week follow Vixie-
cron rules — when both are restricted, EITHER match fires.

**All times are UTC.** The dashboard input field shows next-run
preview labels ending in `Z` so the operator can sanity-check what
they typed. The scheduler loop on the master runs in UTC; converting
to local-time happens only in the browser if you choose to.

## Sequence — set

```
  T=0    POST /api/vms/NAME/backup-schedule  {body}
         │
         │ load_cluster()
         │   vms[NAME] missing                 → 404 VM not found
         │   backup_targets[target_id] missing → 400 target not configured
         │
         │ cron.next_n(cron_expr, 5)   ← parses + validates upfront
         │   CronError → 400 invalid cron expression: <reason>
         │
         │ bedrock_state.backup_schedule_set(vm, target_id, cron_expr,
         │   label_prefix, retention_count)
         │   → UPDATE vms SET backup_schedule = <json>, bumps revision
         │
         │ Return 200 { status, revision, vm, cron_expr, next_fires_utc }
         │
  (async) The schedule JSON now sits in vms.backup_schedule. Every
          node's load_cluster()/view projects it under
          vm["backup_schedule"]; only the master's backup_scheduler
          loop reads it and fires.
```

## Sequence — fire (master loop)

```
  backup_scheduler: every 60s
    │
    │   skip unless _is_leader()  ← SELECT mgmt_master FROM cluster_info
    │                                WHERE id=1 in rqlite (level='none')
    │
    │   _scheduler_tick:
    │     cluster = cluster_state.load_cluster()   ← local rqlite read
    │     for vm_name, vm in cluster.vms:
    │       sched = vm.backup_schedule
    │       if not sched or no target_id/cron_expr: continue
    │       if sched.target_id not in backup_targets: warn + skip
    │       if vm_name in _SCHEDULED_INFLIGHT: continue   ← prior fire
    │                                                        still running
    │
    │       last_fired = _last_scheduled_fire_time(vm, sched):
    │         scan vm.backups (newest-first, projected from vm_backups)
    │         for the same target_id whose label parses as
    │         "<label_prefix>-YYYYMMDDTHHMMSS"; else None
    │
    │       should_fire = cron.should_fire_now(
    │                       sched.cron_expr, now=now,
    │                       last_fired_at = last_fired,
    │                       grace_minutes = 60)
    │
    │       if should_fire:
    │         _SCHEDULED_INFLIGHT.add(vm_name)
    │         create_task(_run_scheduled_backup):
    │           backup.run_backup(target_id, vm_name,
    │                             label="<prefix>-<utc-stamp>")
    │           finally: _SCHEDULED_INFLIGHT.discard(vm_name)
```

`should_fire_now(expr, now, last_fired_at, grace_minutes=60)`:

- **`last_fired_at is None`** (never fired): fire if cron has any
  matching minute in the last 60 minutes ("just configured a
  `@daily 02:00` schedule and it's now 02:30 — go"). Bounded to the
  grace window, so a fresh schedule never back-fires more than once.
- **`last_fired_at` set**: fire if `next_after(last_fired_at)` is
  now-or-past. After a long master outage this fires AT MOST ONE
  catch-up: the run records a fresh `vm_backups` row, the next tick
  reads that as the new `last_fired_at`, and waits for the next window.

## Why master-only firing

The master is the single node that drives scheduled work; letting every
node fire would duplicate every backup. Gating the loop on `_is_leader()`
serialises firing, while the backup record still lands in rqlite, so
state is consistent no matter which node ran it.

A master change mid-fire is harmless: each scheduled backup is named by
its `<prefix>-<utc-stamp>` label and recorded as a `vm_backups` row. The
new master's first tick reads that row (via `vm["backups"]`) as
`last_fired_at` and waits for `next_after(last_fired_at)`.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 invalid cron expression: …` | Typo in the cron field | Read the message, fix the field. The dashboard shows the same error live as the operator types. |
| `404 VM 'X' not found` | Schedule for a non-existent VM | Use a valid VM name. |
| `400 backup target 'X' not configured` | Schedule referenced a target absent from `backup_targets` | Configure the target, or set the schedule to a valid one. |
| Schedule set, but no backups fire | `bedrock-d` isn't running on the master, or `cluster_info.mgmt_master` doesn't point at this node | Check `journalctl -u bedrock-d \| grep scheduler` on the master. The first log line is `scheduler: starting (master-only loop)`. |
| Scheduler logs `scheduled to non-existent target` and skips | The schedule's `target_id` is no longer in `backup_targets` | Re-create the target or re-set the schedule. |
| Fires don't catch up after an mgmt outage | By design, only ONE window catches up: the never-fired path scans only `grace_minutes=60`; the fired path fires once and advances `last_fired_at`. | Expected. Run `bedrock vm backup` manually if you need an immediate one. |
| `_SCHEDULED_INFLIGHT` growing unbounded | A scheduled `run_backup` raised but the `finally` block didn't run | Check `journalctl -u bedrock-d \| grep "scheduler:"` for the failing VM, fix the underlying issue (wrong target, no creds, network), restart `bedrock-d` to reset the in-memory set. |

## Operator perspective

- **Default cadence**: none. There is no auto-schedule; the operator
  opts in per VM.
- **Recommended starting points**:
  - DB / app server with hot data: `0 */4 * * *` (every 4 hours UTC)
  - File server / dev VM: `0 2 * * *` (daily 02:00 UTC)
  - Archive VM: `0 3 * * 0` (weekly Sunday 03:00 UTC)
- **Retention**: scheduled backups are kept until the operator deletes
  them. `retention_count` is stored on the schedule entry but not
  enforced; `0` means keep all.
- **UTC everywhere** avoids the DST footgun — `02:30` runs zero or two
  times on a transition day. The dashboard shows `Z`-suffixed times.
