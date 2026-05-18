# `mgmt/cron.py`

**Module purpose.** Parse cron expressions into "next fire time"
helpers used by `orchestrator.backup_scheduler`. Avoids pulling
in a full croniter dependency for our small set of supported
expressions.

## Functions

- `parse_cron(expr: str) -> CronExpr` — accept `* * * * *`,
  `*/N`, `<min>,<min>`, `<lo>-<hi>` in any of the 5 fields.
  Raises on syntax error.
- `next_fire(expr, after_dt) -> datetime` — return the next
  datetime ≥ after_dt that matches.
- `is_match(expr, dt) -> bool` — does this exact datetime
  match? Used by the scheduler's "did we just fire this run?"
  dedupe.

The CronExpr internal repr is `(minute_set, hour_set, dom_set,
month_set, dow_set)` for fast set-membership checks.
