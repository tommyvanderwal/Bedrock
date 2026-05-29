# mgmt/cron.py

A self-contained 5-field cron parser and scheduler-decision helper for Bedrock backup scheduling. It depends only on `datetime` and `re` — no `croniter` PyPI package — so the bedrock-mgmt service runtime keeps a minimal supply chain. The dashboard cron-preview endpoint calls `next_n`; the backup scheduler loop calls `should_fire_now` to decide when a VM's backup is due. All times are naive UTC.

## Functions / Classes

### `class CronError(ValueError)`
Raised for any malformed cron expression (empty input, wrong field count, unknown token, reversed range, non-positive step, out-of-range value, or no match within the search window).

### `parse(expr) -> dict`
Parse a cron expression into per-field match-sets.
- **In:** `expr` — a 5-field cron string (`minute hour day-of-month month day-of-week`) or a preset (`@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`, `@annually`). Case-insensitive.
- **Out:** dict with keys `minute` / `hour` / `dom` / `month` / `dow`, each a `set[int]` of every legal value. Raises `CronError` on invalid input. No side effects.

### `next_after(parsed, t) -> dt.datetime`
Smallest minute strictly after `t` that matches.
- **In:** `parsed` — the dict from `parse`; `t` — naive UTC datetime.
- **Out:** matching naive UTC datetime (second and microsecond zeroed). Searches minute-by-minute up to ~4 years ahead and raises `CronError` if nothing matches. No side effects.

### `next_n(expr, n=5, start=None) -> list[str]`
Convenience wrapper used by the dashboard cron-preview endpoint.
- **In:** `expr` — cron string; `n` — how many fire times to return (default 5); `start` — naive UTC datetime to count from (defaults to `dt.datetime.utcnow()`).
- **Out:** list of `n` UTC ISO-8601 strings formatted `%Y-%m-%dT%H:%M:%SZ` (trailing `Z`). Parses `expr` (may raise `CronError`). No side effects.

### `should_fire_now(expr, *, now=None, last_fired_at=None, grace_minutes=60) -> bool`
Decide whether the scheduler should fire this VM's backup right now.
- **In:** `expr` — cron string; `now` — keyword-only naive UTC datetime (defaults to `dt.datetime.utcnow()`); `last_fired_at` — keyword-only naive UTC datetime of the last fire, or `None` if never fired; `grace_minutes` — look-back window (default 60) used only when never fired.
- **Out:** `bool`. Parses `expr` (may raise `CronError`). No side effects.

### Private helpers
- `_parse_field(spec, lo, hi, names=None)` — expand one field (`*`, `N`, `N,M`, `N-M`, `*/k`, `N-M/k`) into a `set[int]` clamped to `[lo, hi]`; `names` is the lowercase alias map for month / day-of-week fields.
- `_resolve_value(token, lo, hi, names)` — resolve one token to an int via the alias map or `int()`, apply the dow `7 == 0` quirk, range-check against `[lo, hi]`.
- `_is_match(parsed, t)` — test whether a UTC datetime satisfies a parsed expression.

## How it works

`parse` first folds presets via `_PRESETS`, then splits on whitespace and requires exactly 5 fields. Each field is expanded by `_parse_field` with that field's `[lo, hi]` bounds and, for month and day-of-week, an alias map (`_MONTH_NAMES`, `_DOW_NAMES`). `_parse_field` splits comma segments, peels an optional `/step` (must be a positive int), then expands the base as `*` (full range), an `a-b` range, or a single value via `_resolve_value`, accumulating `range(start, end+1, step)` into a set. A reversed range raises `CronError`. Day-of-week accepts `0..7` with both `0` and `7` meaning Sunday; the `7 → 0` collapse happens in `_resolve_value` only when matching against `_DOW_NAMES`.

Field ranges: minute `0-59`, hour `0-23`, day-of-month `1-31`, month `1-12`, day-of-week `0-6`.

`_is_match` checks minute, hour, and month directly. Day matching follows Vixie-cron's OR/AND coupling between day-of-month (DOM) and day-of-week (DOW):

```
DOM=*   DOW=*    -> every day
DOM=*   DOW=set  -> match on DOW
DOM=set DOW=*    -> match on DOM
DOM=set DOW=set  -> match if EITHER matches
```

"Restricted" (`set`) is detected by comparing the field-set against the full range (`1..31` for DOM, `0..6` for DOW). Python's `weekday()` (Mon=0..Sun=6) is converted to cron's convention (Sun=0..Sat=6) with `(weekday() + 1) % 7`.

`next_after` truncates `t` to the minute, steps forward one minute, and brute-forces minute-by-minute until `_is_match` is true, capped at `366 * 4` days so leap-year edges are covered before it gives up with `CronError`.

`should_fire_now` has two branches:

```
last_fired_at is None  (never fired)
  scan back grace_minutes from `now`, minute by minute
  fire if ANY scanned minute matches the cron
  -> catches "just set @daily 02:00, it's now 02:30, go"

last_fired_at set
  next_fire = next_after(parsed, last_fired_at)
  fire if next_fire <= now
  -> deterministic, idempotent catch-up after a master restart
```

Both branches consider at most one missed window, so a long downtime does not trigger a burst of catch-up backups.

## Why

A bespoke parser keeps a pip package out of the service runtime. UTC throughout (parser, scheduler loop, dashboard display) keeps the cluster's notion of time unambiguous regardless of operator local timezone.
