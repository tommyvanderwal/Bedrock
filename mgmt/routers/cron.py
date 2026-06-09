"""Cron-expression preview helper for the dashboard's backup-schedule UI."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
router = APIRouter(tags=["cron"])




@router.get("/api/cron/preview")
def api_cron_preview(expr: str, n: int = 5):
    """Return the next N fire times for a cron expression (UTC ISO).
    Used by the dashboard's schedule-input field for live preview as
    the operator types. Pure parser — no I/O."""
    from mgmt import cron as _cron
    try:
        return {"cron_expr": expr, "next_fires_utc": _cron.next_n(expr, n=max(1, min(n, 20)))}
    except _cron.CronError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")
