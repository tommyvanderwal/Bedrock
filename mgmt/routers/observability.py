"""Operator action: promote/swap a metrics or logs backend (heavier DI than the read-only
routes in routes_obs.py)."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from dependencies import require_operator
from common import load_cluster, ssh_cmd, ssh_cmd_rc, push_log
import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import bedrock_state as _bs           # noqa: E402
router = APIRouter(tags=["observability"])




# ── Observability backend management (operator CLI) ─────────────────

class ObsPromote(BaseModel):
    new_node: str
    replace: str = ""
    kind: str = "both"   # "both", "metrics", or "logs"




@router.post("/api/observability/backends")
def observability_promote(req: ObsPromote, user: str = Depends(require_operator)):
    """Add or swap a backend in `obs_backends`. Runs the vmbackup-
    vmrestore seed BEFORE flipping the snapshot so the new backend
    isn't visible until it's caught up. Synchronous — operator CLI
    waits on this call."""
    cluster = load_cluster()
    nodes = (cluster.get("nodes") or {})
    if req.new_node not in nodes:
        raise HTTPException(400, f"unknown node {req.new_node!r}")
    obs = (cluster.get("obs_backends") or {})
    metrics_bk = list(obs.get("metrics") or [])
    logs_bk    = list(obs.get("logs") or [])
    do_metrics = req.kind in ("both", "metrics")
    do_logs    = req.kind in ("both", "logs")

    def _slot(curr: list[str]) -> tuple[list[str], str]:
        """Compute the post-promote list + source for seeding. Returns
        (new_list, source_host_or_'')."""
        if req.new_node in curr:
            return curr, ""   # nothing to do — already a backend
        if len(curr) < 2:
            # Free slot available; just append.
            return curr + [req.new_node], (nodes.get(curr[0], {}).get("host", "") if curr else "")
        # Both slots full → must replace.
        if not req.replace:
            raise HTTPException(400, "both backend slots full; pass --replace")
        if req.replace not in curr:
            raise HTTPException(400, f"--replace {req.replace!r} not in current backend list {curr}")
        # Seed from the OTHER existing backend (the one we're keeping).
        keep = [b for b in curr if b != req.replace][0]
        return [n if n != req.replace else req.new_node for n in curr], \
               nodes.get(keep, {}).get("host", "")

    new_metrics, src_metrics = (_slot(metrics_bk) if do_metrics else (metrics_bk, ""))
    new_logs,    src_logs    = (_slot(logs_bk)    if do_logs    else (logs_bk, ""))

    target_host = nodes[req.new_node].get("host", "")
    if not target_host:
        raise HTTPException(503, f"{req.new_node!r} has no host address")

    # === Phase 1: flip the snapshot FIRST. ===
    # This puts the new node into the agent target list everywhere.
    # Every node's vmagent + vlagent reconfigures and starts dual-
    # writing to the new target — which isn't accepting yet, so writes
    # accumulate in the agent disk queue. The new node's reactor sees
    # itself in `obs_backends` but `_can_start_vm_backend` returns
    # False (data dir empty + not solo backend), so bedrock-vm stays
    # stopped. bedrock-vl starts (VL has no seed path).
    try:
        _bs.obs_backends_set(metrics=new_metrics, logs=new_logs)
    except Exception as e:
        raise HTTPException(503, f"could not set obs backends: {e}")

    # Give agents a moment to fold the entry + reconfigure. Two seconds
    # is enough on the testbed; the orchestrator subscriber polls fast.
    # If we skipped this and went straight to seed, agents would still
    # be configured for the OLD target list and writes between snapshot
    # and start would land only on the source — exactly the gap this
    # reorder eliminates.
    import time as _t
    _t.sleep(2)

    # === Phase 2: seed the new node's data dir. ===
    # During this window: agents are buffering for the new target;
    # source backend is still serving reads. vmbackup snapshots the
    # source at this instant, ships, vmrestores into the target's data
    # dir. The seed is "frozen in time" from this snapshot moment.
    seed_report = {}
    try:
        from lib import observability as _obs

        def _runner(host: str, cmd: str, timeout: int = 60):
            return ssh_cmd_rc(host, cmd, timeout=timeout)

        # `force=True` whenever we're replacing an existing backend.
        # The new node might have stale data from a previous tenancy
        # as a backend; without force, seed_backend's "data dir is
        # not empty, skip" guard would leave that stale data in
        # place. For a free-slot promote (cluster expansion 1→2),
        # the empty-data-dir check is the right safety net.
        _force = bool(req.replace)
        if do_metrics and src_metrics and req.new_node not in metrics_bk:
            rep = _obs.seed_backend(src_metrics, target_host, _runner, None,
                                    force=_force)
            seed_report["metrics"] = rep.get("metrics", "?")
        if do_logs and src_logs and req.new_node not in logs_bk:
            if src_logs != src_metrics or not do_metrics:
                rep = _obs.seed_backend(src_logs, target_host, _runner, None,
                                        force=_force)
            seed_report["logs"] = rep.get("logs", "?")
    except Exception as e:
        push_log(f"obs.seed_backend warning: {e}",
                 node="mgmt", app="bedrock-mgmt", level="warn")

    # === Phase 3: start the backend daemon on the new node. ===
    # Reactor's seed gate keeps bedrock-vm stopped until the data dir
    # is populated. We just populated it via vmrestore, so SSH in and
    # start it explicitly. Once it's up, agents drain their disk-queue
    # buffers (writes that accumulated during phases 1+2) into the new
    # backend — convergence with zero data gap.
    if do_metrics and req.new_node in new_metrics and target_host:
        try:
            ssh_cmd(target_host, "systemctl start bedrock-vm.service", timeout=20)
        except Exception as e:
            push_log(f"could not start bedrock-vm on {req.new_node}: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")

    _replace_disp = req.replace or "-"
    push_log(f"operator {user!r} promoted {req.new_node!r} "
             f"(replace={_replace_disp}, kind={req.kind})",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {
        "metrics_backends": new_metrics,
        "logs_backends":    new_logs,
        "seed_report":      seed_report,
    }
