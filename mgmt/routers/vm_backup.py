"""Per-VM backup/restore: run a backup, list a VM's backups, restore, manage backup schedule,
delete a snapshot. Long ops run as background tasks (the dashboard polls /api/tasks)."""
from __future__ import annotations
from typing import Optional
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from dependencies import require_operator
from common import (load_cluster, push_log, build_cluster_state, get_nodes,
                    _import_backup_module)
from tasks import registry as task_registry

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs             # noqa: E402
router = APIRouter(tags=["vm-backup"])




class BackupRunRequest(BaseModel):
    target_id: str = "main"
    label: str = ""                   # operator-visible tag




class RestoreRequest(BaseModel):
    target_id: str = "main"
    # Empty → restore the VM's NEWEST recorded backup. run_restore_to_ha
    # resolves vms[<name>].backups[0] when this is blank, so the common
    # "restore latest" case needs no snapshot id from the operator.
    kopia_snapshot_id: str = ""
    dest_node: Optional[str] = None
    target_lv_path: Optional[str] = None




class BackupDeleteRequest(BaseModel):
    target_id: str = "main"
    reason: str = ""




@router.post("/api/vms/{vm_name}/backup")
async def api_vm_backup(vm_name: str, req: BackupRunRequest = BackupRunRequest()):
    """Take a backup of `vm_name` to `target_id`. Returns 202 + task_id;
    the UI watches /api/tasks (or the WS task channel) for completion.

    The backup runs on the VM's home node: take an LV snapshot, kopia
    snapshot create, drop the LV snapshot. Idempotent only at the
    log-entry level; multiple in-flight backups of the same VM are not
    serialised here — the operator is expected not to double-click."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    target = (cluster.get("backup_targets") or {}).get(req.target_id)
    if target is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    # Multi-target: resolve the primary's mirror set NOW (from cluster state)
    # and carry it in the saga params so the home node's sync_to_secondaries
    # step mirrors the backup. Putting it in params (vs re-reading on the home
    # node) makes it durable across resume + master failover.
    secondary_target_ids = list(target.get("sync_to") or [])

    # Submit a vm_backup saga targeted at the VM's HOME node. That node's
    # operations_drain (mgmt/orchestrator.py) runs it locally — kopia on
    # the node that owns the disks — recording the result to rqlite. No
    # SSH from here; rqlite's `operations` table is the channel.
    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_backup
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_backup", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "label": req.label or "",
                    "secondary_target_ids": secondary_target_ids},
            requested_by="api_vm_backup",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue backup: {e}")
    push_log(f"VM {vm_name}: backup queued → {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}




@router.get("/api/vms/{vm_name}/backups")
def api_vm_backups_list(vm_name: str):
    """Backup history for a VM, drawn from cluster state. Newest first."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    return {
        "vm": vm_name,
        "backups": vm.get("backups") or [],
        "last_backup_error": vm.get("last_backup_error"),
        "last_restore": vm.get("last_restore"),
        "last_restore_error": vm.get("last_restore_error"),
    }




@router.post("/api/vms/{vm_name}/restore")
async def api_vm_restore(vm_name: str, req: RestoreRequest):
    """Restore a VM from a kopia backup and bring it back up HA. Submits
    a vm_restore saga targeted at the VM's home node; that node powers the
    VM off, restores each disk through its DRBD primary (so the bytes
    replicate to peers), and starts it again. Returns 202 + operation_id."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not present in this cluster")
    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_restore
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_restore", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "kopia_snapshot_id": req.kopia_snapshot_id or ""},
            requested_by="api_vm_restore",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue restore: {e}")
    push_log(f"VM {vm_name}: restore queued from {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}




class BackupScheduleSetRequest(BaseModel):
    target_id: str = "main"
    cron_expr: str               # 5-field UTC cron, e.g. "0 2 * * *" or "@daily"
    label_prefix: str = "auto"   # auto-generated labels start with "<prefix>-"
    retention_count: int = 0     # 0 = keep all (v1.0 default)
    reason: str = ""




@router.post("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_set(vm_name: str, req: BackupScheduleSetRequest):
    """Set or replace the periodic-backup schedule for a VM. The
    schedule is stored in the cluster log so it survives master
    failover; the master's `backup_scheduler` loop is the only firer.

    Returns the next 5 fire times (UTC) so the caller can sanity-check
    their cron expression before relying on it."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    import cron as _cron

    cluster = load_cluster()
    if (cluster.get("vms") or {}).get(vm_name) is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    # Validate the cron expression server-side. Better to fail at submit
    # time than have the scheduler silently skip the VM forever.
    try:
        next_fires = _cron.next_n(req.cron_expr, n=5)
    except _cron.CronError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")

    try:
        rev = _bs.backup_schedule_set(
            vm=vm_name, target_id=req.target_id,
            cron_expr=req.cron_expr,
            label_prefix=req.label_prefix,
            retention_count=req.retention_count,
            reason=req.reason or "set via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    push_log(f"backup schedule set for VM {vm_name}: cron={req.cron_expr!r} "
             f"target={req.target_id}",
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "vm": vm_name,
        "cron_expr": req.cron_expr,
        "next_fires_utc": next_fires,
    }




@router.delete("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_remove(vm_name: str, reason: str = ""):
    try:
        rev = _bs.backup_schedule_removed(
            vm=vm_name, reason=reason or "removed via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "vm": vm_name}




@router.delete("/api/vms/{vm_name}/backups/{kopia_snapshot_id}")
def api_vm_backup_delete(vm_name: str, kopia_snapshot_id: str,
                         req: BackupDeleteRequest = BackupDeleteRequest()):
    """Delete one snapshot from the kopia repo. Returns synchronously —
    delete is fast (just drops a manifest). GC of the underlying chunks
    happens during the next `kopia maintenance run` on the master."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    backup = _import_backup_module()
    try:
        backup.delete_backup(req.target_id, kopia_snapshot_id, vm_name,
                             reason=req.reason or "operator-delete")
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"status": "ok", "kopia_snapshot_id": kopia_snapshot_id}
