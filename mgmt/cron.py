"""Minimal cron-expression parser for bedrock backup scheduling.

We deliberately avoid the `croniter` PyPI dependency — adding pip
packages to the bedrock-mgmt service runtime expands the supply
chain. The 5-field cron syntax bedrock supports is small enough
that a self-contained parser is well under 200 lines.

Supported syntax:
  - 5 fields: minute hour day-of-month month day-of-week
  - Each field: `*`, `N`, `N,M,…`, `N-M`, `*/k`, `N-M/k`
  - Presets: `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`
  - Day-of-week: 0..7 (0 and 7 both = Sunday), or sun/mon/...

NOT supported (Quartz extensions):
  - `?`, `L`, `W`, `#`, year field

All times are interpreted as UTC. The scheduler loop runs in UTC
and the dashboard displays UTC explicitly so operators don't get
confused by their local timezone vs the cluster's notion of time.
"""
from __future__ import annotations

import datetime as dt
import re

# Day-of-week aliases. `7` and `sun` both mean Sunday (cron convention).
_DOW_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}
_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_PRESETS = {
    "@hourly":  "0 * * * *",
    "@daily":   "0 0 * * *",
    "@weekly":  "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly":  "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


class CronError(ValueError):
    pass


def _parse_field(spec: str, lo: int, hi: int,
                 names: dict | None = None) -> set[int]:
    """Expand one cron field (e.g. '*/5', '1,3-5', 'mon-fri') into a
    sorted set of integer values. `names` is a lowercase->int map for
    DOW/month aliases (None for numeric-only fields)."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty cron field segment in {spec!r}")
        # Step part: <expr>/<step>
        step = 1
        if "/" in part:
            base, step_s = part.rsplit("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise CronError(f"invalid step {step_s!r} in {part!r}")
            if step <= 0:
                raise CronError(f"step must be positive in {part!r}")
            part = base
        # Expand base
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start = _resolve_value(a, lo, hi, names)
            end = _resolve_value(b, lo, hi, names)
        else:
            v = _resolve_value(part, lo, hi, names)
            start = end = v
        if start > end:
            raise CronError(f"range {start}-{end} reversed in {spec!r}")
        for v in range(start, end + 1, step):
            out.add(v)
    return out


def _resolve_value(token: str, lo: int, hi: int,
                   names: dict | None) -> int:
    token = token.strip().lower()
    if names and token in names:
        return names[token]
    try:
        v = int(token)
    except ValueError:
        raise CronError(f"unknown token {token!r}")
    # cron quirk: dow 7 == 0 (Sunday)
    if names is _DOW_NAMES and v == 7:
        v = 0
    if v < lo or v > hi:
        raise CronError(f"value {v} out of range [{lo},{hi}]")
    return v


def parse(expr: str) -> dict:
    """Parse a cron expression into match-sets. Raises CronError on
    invalid input. Returns a dict with keys minute / hour / dom /
    month / dow, each mapped to a set of legal integer values."""
    expr = (expr or "").strip()
    if not expr:
        raise CronError("empty cron expression")
    if expr.lower() in _PRESETS:
        expr = _PRESETS[expr.lower()]
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(
            f"cron expression must have 5 fields (got {len(fields)}): {expr!r}"
        )
    return {
        "minute": _parse_field(fields[0], 0, 59),
        "hour":   _parse_field(fields[1], 0, 23),
        "dom":    _parse_field(fields[2], 1, 31),
        "month":  _parse_field(fields[3], 1, 12, _MONTH_NAMES),
        "dow":    _parse_field(fields[4], 0, 6,  _DOW_NAMES),
    }


def _is_match(parsed: dict, t: dt.datetime) -> bool:
    """Cron's day-of-month / day-of-week matching is OR-ed when both
    are restricted, AND-ed when one is *. We approximate the standard
    Vixie-cron behavior:
      - Both DOM=* and DOW=*: match every day
      - DOM=*, DOW=restricted: match on DOW
      - DOM=restricted, DOW=*: match on DOM
      - Both restricted: match if EITHER matches
    """
    if t.minute not in parsed["minute"]: return False
    if t.hour not in parsed["hour"]: return False
    if t.month not in parsed["month"]: return False
    dom_full = parsed["dom"] == set(range(1, 32))
    dow_full = parsed["dow"] == set(range(0, 7))
    # Python weekday(): Mon=0..Sun=6. Convert to cron Sun=0..Sat=6:
    dow_cron = (t.weekday() + 1) % 7
    if dom_full and dow_full:
        return True
    if dom_full and not dow_full:
        return dow_cron in parsed["dow"]
    if not dom_full and dow_full:
        return t.day in parsed["dom"]
    return (t.day in parsed["dom"]) or (dow_cron in parsed["dow"])


def next_after(parsed: dict, t: dt.datetime) -> dt.datetime:
    """Smallest time strictly greater than `t` that matches the
    expression. Time is naive UTC. Caps the search at 4 years
    (covers leap-year edge cases) and raises if no match found."""
    # Truncate to minute precision and step up by 1 minute
    cur = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    deadline = cur + dt.timedelta(days=366 * 4)
    while cur < deadline:
        if _is_match(parsed, cur):
            return cur
        cur += dt.timedelta(minutes=1)
    raise CronError(f"no match within 4 years for {parsed!r}")


def next_n(expr: str, n: int = 5,
           start: dt.datetime | None = None) -> list[str]:
    """Convenience wrapper: parse + return the next N fire times as
    UTC ISO-8601 strings (with trailing 'Z'). Used by the dashboard
    cron-preview endpoint."""
    parsed = parse(expr)
    cur = start or dt.datetime.utcnow()
    out: list[str] = []
    for _ in range(n):
        cur = next_after(parsed, cur)
        out.append(cur.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return out


def should_fire_now(expr: str, *, now: dt.datetime | None = None,
                    last_fired_at: dt.datetime | None = None,
                    grace_minutes: int = 60) -> bool:
    """Decide whether the scheduler should fire this VM's backup
    right now. Logic:

      - If never fired before: fire if cron has fired any time within
        the last `grace_minutes` minutes (catches "just configured a
        @daily 02:00 schedule and it's now 02:30 — go").
      - If fired before: fire if the next-fire-after-last is now-or-
        past. This makes catch-up after master restart deterministic
        and idempotent.

    Both cases are bounded so we don't fire dozens of catch-up
    backups after a long downtime — only the most recent missed
    window triggers.
    """
    parsed = parse(expr)
    now = now or dt.datetime.utcnow()

    if last_fired_at is None:
        # Never fired — look back grace_minutes for any matching minute
        scan = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=grace_minutes)
        cur = scan
        while cur <= now:
            if _is_match(parsed, cur):
                return True
            cur += dt.timedelta(minutes=1)
        return False

    # Has fired — find the next fire after last and see if it's due.
    next_fire = next_after(parsed, last_fired_at)
    return next_fire <= now
