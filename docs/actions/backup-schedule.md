# Schedule periodic backups

Per-VM cron schedule that fires `vm-backup` automatically. The
schedule lives in the cluster log so it survives mgmt master
failover; the master is the only node that fires.

**Triggered by:**

- Dashboard: VM detail → Backups card → "Schedule" subsection →
  cron input field + Save schedule
- HTTP:
  - `POST /api/vms/{name}/backup-schedule` body
    `{"target_id":"main", "cron_expr":"0 2 * * *",
      "label_prefix":"auto", "retention_count":0}`
  - `DELETE /api/vms/{name}/backup-schedule`
  - `GET  /api/cron/preview?expr=<cron>&n=5` — pure parser, no I/O

**Source:** `mgmt/app.py:api_vm_backup_schedule_set` /
`api_cron_preview`,
`mgmt/cron.py` (parser),
`mgmt/orchestrator.py:backup_scheduler` + `_scheduler_tick`,
`installer/lib/view_builder.py` BACKUP_SCHEDULE_* fold rules.

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
`@monthly`, `@yearly`. NOT supported: Quartz extensions (`?`, `L`,
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
         │   VM not in cluster.json   → 404
         │   target_id not configured → 400
         │
         │ cron.next_n(cron_expr, 5)   ← parses + validates upfront
         │   parse error → 400 with the operator-facing reason
         │
         │ bedrock-rust IPC: append BACKUP_SCHEDULE_SET
         │   {vm, target_id, cron_expr, label_prefix, retention_count}
         │
         │ Return 200 { status, log_index, vm, cron_expr, next_fires_utc }
         │
  (async) Every node's mgmt subscriber folds the entry into
          cluster.json under vm["backup_schedule"]. Only the master's
          backup_scheduler loop acts on it.
```

## Sequence — fire (master loop)

```
  every 60s on the leader (role file = "leader"):
    │
    │ tick:
    │   skip if not leader
    │   for vm in cluster.json.vms:
    │     sched = vm.backup_schedule
    │     if not sched: continue
    │     if vm in _SCHEDULED_INFLIGHT: continue   ← skip if previous
    │                                                 fire still running
    │
    │     last_fired = parse most recent BACKUP_DONE label that
    │                   starts with "<label_prefix>-YYYYMMDDTHHMMSS"
    │
    │     should_fire = cron.should_fire_now(
    │                     sched.cron_expr,
    │                     last_fired_at = last_fired,
    │                     grace_minutes = 60)
    │
    │     if should_fire:
    │       _SCHEDULED_INFLIGHT.add(vm)
    │       asyncio.create_task( run_backup(target, vm,
    │                                        label="<prefix>-<utc-stamp>") )
    │       (on completion / failure: remove from inflight set)
```

`should_fire_now` semantics:

- **Never fired before**: fire if cron has any matching minute within
  the last 60 minutes ("just configured a `@daily 02:00` schedule
  and it's now 02:30 — go").
- **Fired before**: fire if the next-cron-fire-after-last is now-or-
  past. Idempotent under master restart: catching up after a long
  downtime fires AT MOST ONE missed window, not dozens.

## Why master-only firing

Appending to the cluster log is single-writer (the leader). Letting
followers fire the scheduler would either (a) duplicate every backup
or (b) crash on the IPC append. The leader's view of cluster.json is
the canonical "what should fire" — running the loop there is the
naturally serialised choice.

A leader-change mid-fire is a non-issue: the in-flight backup is
identified by its label timestamp, which gets folded into cluster.json
on the new leader's catch-up. The new leader's first tick sees
`last_fired_at` = the in-flight backup's timestamp, computes
"next-after-that", and waits for the next legitimate window.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 invalid cron expression: …` | Typo in the cron field | Read the message, fix the field. The dashboard shows the same error live as the operator types. |
| `404 VM 'X' not found` | Schedule for a non-existent VM | Use a valid VM name. |
| `400 backup target 'X' not configured` | Schedule referenced a target that was deleted | Re-create the target or set the schedule to a valid one. |
| Schedule set, but no backups fire | Master mgmt isn't running, or `/run/bedrock-rust.role` doesn't say `leader` | Check `journalctl -u bedrock-mgmt \| grep scheduler` on the master. The first log line should be `scheduler: starting (master-only loop)`. |
| Backup fires every minute even though cron is `@daily` | Bug in cron parser? Open an issue with the exact expression — `mgmt/cron.py` has a sanity-test suite at the top of the file. |
| Fires accumulate during an mgmt outage | By design, only ONE missed window catches up after master restart (the most recent matching minute within `grace_minutes=60`). Older missed windows are skipped. |
| `_SCHEDULED_INFLIGHT` growing unbounded | A scheduled `run_backup` raised but the `finally` block didn't run | Check `journalctl -u bedrock-mgmt \| grep "scheduler:"` for the failing VM, fix the underlying issue (wrong target, no creds, network), restart mgmt to reset the in-memory set. |

## Operator perspective

- **Default cadence**: nothing. v1.0 ships with no auto-schedule;
  the operator opts in per VM.
- **Recommended starting points**:
  - DB / app server with hot data: `0 */4 * * *` (every 4 hours UTC)
  - File server / dev VM: `0 2 * * *` (daily 02:00 UTC)
  - Archive VM: `0 3 * * 0` (weekly Sunday 03:00 UTC)
- **Retention**: v1.0 keeps everything until the operator deletes
  manually. v1.x will honour `retention_count` on the schedule entry
  to prune oldest scheduled backups beyond that count after each
  successful fire.
- **UTC everywhere** is a deliberate choice — DST transitions in
  cron are a footgun (`02:30` runs zero or two times depending on
  the day). Operators reading the dashboard see `Z`-suffixed times
  and learn to convert mentally.
