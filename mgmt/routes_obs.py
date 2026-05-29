"""Observability read-API routes.

Thin wrappers over VictoriaMetrics + VictoriaLogs read endpoints —
queries pre-canned for the dashboard. No state, no auth gates
(they read public-ish data; same security model as the rest of
the dashboard read API).

Three groups: node-level metrics, VM-level metrics, DRBD metrics,
plus three log-search endpoints (global / per-node / per-VM).

# What's NOT here

``observability_promote`` (the operator's "swap a metrics/logs
backend" action) lives in app.py — it pulls in bedrock_state + ssh
+ tier_storage and needs a heavier DI surface than these read-only
wrappers.
"""
from __future__ import annotations

import time

from fastapi import FastAPI

# victoria.py is a peer module in mgmt/; the app's sys.path already
# has mgmt/ for the bare imports inside app.py.
from victoria import query_range, query_logs


def register_routes(app: FastAPI) -> None:
    """Attach the metrics + logs read endpoints to ``app``."""

    # ─── Metrics (VictoriaMetrics) ────────────────────────────────

    @app.get("/api/metrics/nodes")
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

    @app.get("/api/metrics/vms")
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

    @app.get("/api/metrics/drbd")
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

    @app.get("/api/logs")
    def api_logs(query: str = "*", limit: int = 50, hours: int = 1):
        end = int(time.time())
        start = end - hours * 3600
        return query_logs(query, limit=limit, start=start, end=end)

    @app.get("/api/logs/node/{node_name}")
    def api_logs_node(node_name: str, limit: int = 50, hours: int = 1):
        end = int(time.time())
        start = end - hours * 3600
        return query_logs(f'hostname:"{node_name}"',
                          limit=limit, start=start, end=end)

    @app.get("/api/logs/vm/{vm_name}")
    def api_logs_vm(vm_name: str, limit: int = 50, hours: int = 1):
        end = int(time.time())
        start = end - hours * 3600
        return query_logs(f'"{vm_name}"',
                          limit=limit, start=start, end=end)
