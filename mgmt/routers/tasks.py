"""Async task / saga status read endpoints (the dashboard polls these + the WS 'task' channel)."""
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from tasks import registry as task_registry
router = APIRouter(tags=["tasks"])




@router.get("/api/tasks")
def api_tasks():
    """Active + recently-finished tasks. Clients use WS 'task' channel for
    live updates; this endpoint is the snapshot on fresh page load."""
    return task_registry().list()




@router.get("/api/tasks/{task_id}")
def api_task_get(task_id: str):
    t = task_registry().get(task_id)
    if not t:
        raise HTTPException(404, "task not found (finished and aged out, or never existed)")
    from tasks import _serialize
    return _serialize(t)
