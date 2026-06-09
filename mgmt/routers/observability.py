"""Observability domain: the operator's promote/swap-a-backend action, plus the
read-only metrics + logs endpoints (thin pre-canned queries over VictoriaMetrics
and VictoriaLogs for the dashboard — no state, no auth gates beyond the global
middleware)."""
from __future__ import annotations
import time
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from dependencies import require_operator
from common import load_cluster, ssh_cmd, ssh_cmd_rc, push_log
from victoria import query_range, query_logs
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


# ─── Metrics (VictoriaMetrics) ────────────────────────────────

@router.get("/api/metrics/nodes")
def api_metrics_nodes(hours: int = 1, step: str = "30s"):
    """CPU and memory for all nodes over time."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "cpu": query_range(
            '100 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100',
            start, end, step),
        "mem": query_range(
            '(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100',
            start, end, step),
        "net_rx": query_range(
            'rate(node_network_receive_bytes_total{device="br0"}[1m])',
            start, end, step),
        "net_tx": query_range(
            'rate(node_network_transmit_bytes_total{device="br0"}[1m])',
            start, end, step),
    }


@router.get("/api/metrics/vms")
def api_metrics_vms(hours: int = 1, step: str = "30s"):
    """Per-VM CPU and disk IOPS over time."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "cpu": query_range(
            'rate(bedrock_vm_cpu_time_ns[1m]) / 1e9 * 100',
            start, end, step),
        "disk_rd_iops": query_range(
            'rate(bedrock_vm_disk_read_reqs{disk="0"}[1m])',
            start, end, step),
        "disk_wr_iops": query_range(
            'rate(bedrock_vm_disk_write_reqs{disk="0"}[1m])',
            start, end, step),
        "disk_wr_lat": query_range(
            'rate(bedrock_vm_disk_write_time_ns{disk="0"}[1m]) / rate(bedrock_vm_disk_write_reqs{disk="0"}[1m]) / 1e6',
            start, end, step),
    }


@router.get("/api/metrics/drbd")
def api_metrics_drbd(hours: int = 1, step: str = "30s"):
    """DRBD replication metrics."""
    end = int(time.time())
    start = end - hours * 3600
    return {
        "sent": query_range('rate(bedrock_drbd_sent_kb[1m])',
                            start, end, step),
        "received": query_range('rate(bedrock_drbd_received_kb[1m])',
                                start, end, step),
        "out_of_sync": query_range('bedrock_drbd_out_of_sync_kb',
                                   start, end, step),
    }


# ─── Logs (VictoriaLogs) ──────────────────────────────────────

@router.get("/api/logs")
def api_logs(query: str = "*", limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(query, limit=limit, start=start, end=end)


@router.get("/api/logs/node/{node_name}")
def api_logs_node(node_name: str, limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(f'hostname:"{node_name}"',
                      limit=limit, start=start, end=end)


@router.get("/api/logs/vm/{vm_name}")
def api_logs_vm(vm_name: str, limit: int = 50, hours: int = 1):
    end = int(time.time())
    start = end - hours * 3600
    return query_logs(f'"{vm_name}"',
                      limit=limit, start=start, end=end)
