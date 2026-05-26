"""VictoriaMetrics + VictoriaLogs query client for the Bedrock dashboard.

Reads cycle through the cluster's designated backends from
`obs_backends` in cluster.json — first 2xx wins, errors fall through.
Local 127.0.0.1 is tried first when this node IS a backend (saves a
LAN hop, also the only path that works during a brief LAN blip).

Not a merging proxy — vmagent/vlagent dual-write make both backends
carry identical data from promotion onward, so a single-backend
response is the right answer.
"""

import urllib.request
import urllib.parse
import json
import socket
import time
from pathlib import Path

CLUSTER_FILE = Path("/etc/bedrock/cluster.json")


def _backend_hosts(kind: str) -> list[str]:
    try:
        import sys; sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import cluster_state
        cluster = cluster_state.load_cluster()
    except Exception:
        return []
    backends = (cluster.get("obs_backends") or {}).get(kind) or []
    nodes = cluster.get("nodes") or {}
    try:
        my_host = socket.gethostname()
    except Exception:
        my_host = ""
    addrs = []
    for name in backends:
        info = nodes.get(name) or {}
        host = info.get("loopback_ip") or info.get("drbd_ip") or info.get("host", "")
        if not host:
            continue
        if name == my_host:
            addrs.insert(0, "127.0.0.1")
        else:
            addrs.append(host)
    return addrs


def _vm_urls() -> list[str]:
    addrs = _backend_hosts("metrics") or ["127.0.0.1"]
    return [f"http://{a}:8428" for a in addrs]


def _vl_urls() -> list[str]:
    addrs = _backend_hosts("logs") or ["127.0.0.1"]
    return [f"http://{a}:9428" for a in addrs]


def _try(urls: list[str], path: str, params: dict | None = None,
         data: bytes | None = None, headers: dict | None = None,
         timeout: float = 5.0) -> bytes:
    """Try each backend until one returns 2xx. Raises last error if all fail."""
    last: Exception | None = None
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    for base in urls:
        try:
            req = urllib.request.Request(base + path + qs, data=data,
                                         headers=headers or {})
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
    raise last or RuntimeError("no backends configured")


def query_range(promql: str, start: int = None, end: int = None, step: str = "15s") -> dict:
    """Query VictoriaMetrics for a time range. Returns {metric_labels: [[ts, val], ...]}"""
    now = int(time.time())
    if end is None:
        end = now
    if start is None:
        start = now - 3600  # last hour

    try:
        raw = _try(_vm_urls(), "/api/v1/query_range",
                   params={"query": promql, "start": start, "end": end, "step": step})
        data = json.loads(raw)
        results = {}
        for r in data.get("data", {}).get("result", []):
            label = _label_key(r["metric"])
            results[label] = [[v[0], float(v[1])] for v in r["values"]]
        return results
    except Exception as e:
        return {"error": str(e)}


def query_instant(promql: str) -> dict:
    """Instant query. Returns {label: value}."""
    try:
        raw = _try(_vm_urls(), "/api/v1/query", params={"query": promql})
        data = json.loads(raw)
        results = {}
        for r in data.get("data", {}).get("result", []):
            label = _label_key(r["metric"])
            results[label] = float(r["value"][1])
        return results
    except Exception:
        return {}


def query_logs(logsql: str, limit: int = 50, start: int = None, end: int = None) -> list[dict]:
    """Query VictoriaLogs with LogsQL. Returns list of log entries."""
    params = {"query": logsql, "limit": str(limit)}
    if start: params["start"] = str(start)
    if end:   params["end"] = str(end)
    try:
        raw = _try(_vl_urls(), "/select/logsql/query", params=params)
        results = []
        for line in raw.decode().strip().split("\n"):
            if line:
                results.append(json.loads(line))
        return results
    except Exception as e:
        return [{"error": str(e)}]


def push_log(msg: str, node: str = "mgmt", app: str = "bedrock", level: str = "info"):
    """Push a structured log entry to BOTH log backends (dual-write).
    Mgmt's own log lines should be replicated the same way agent-scraped
    syslog is. Best-effort: silent on failure."""
    entry = json.dumps({
        "_msg": msg,
        "_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hostname": node,
        "app": app,
        "level": level,
    }).encode()
    for base in _vl_urls():
        try:
            req = urllib.request.Request(
                base + "/insert/jsonline",
                data=entry,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass


def _label_key(metric: dict) -> str:
    """Create a readable label from metric labels."""
    name = metric.get("__name__", "")
    instance = metric.get("instance", "")
    vm = metric.get("vm", "")
    resource = metric.get("resource", "")
    if vm:
        return f"{vm}"
    if resource:
        return f"{resource}"
    if instance:
        host = instance.split(":")[0].split(".")[-1]
        return f"node{host}" if host in ("141", "142") else instance
    return name or "unknown"
