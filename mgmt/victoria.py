"""VictoriaMetrics query client for the Bedrock dashboard."""

import urllib.request
import urllib.parse
import json
import time

VM_URL = "http://localhost:8428"
VL_URL = "http://localhost:9428"


def query_range(promql: str, start: int = None, end: int = None, step: str = "15s") -> dict:
    """Query VictoriaMetrics for a time range. Returns {metric_labels: [[ts, val], ...]}"""
    now = int(time.time())
    if end is None:
        end = now
    if start is None:
        start = now - 3600  # last hour

    params = urllib.parse.urlencode({
        "query": promql,
        "start": start,
        "end": end,
        "step": step,
    })
    try:
        resp = urllib.request.urlopen(f"{VM_URL}/api/v1/query_range?{params}", timeout=5)
        data = json.loads(resp.read())
        results = {}
        for r in data.get("data", {}).get("result", []):
            label = _label_key(r["metric"])
            results[label] = [[v[0], float(v[1])] for v in r["values"]]
        return results
    except Exception as e:
        return {"error": str(e)}


def query_instant(promql: str) -> dict:
    """Instant query. Returns {label: value}."""
    params = urllib.parse.urlencode({"query": promql})
    try:
        resp = urllib.request.urlopen(f"{VM_URL}/api/v1/query?{params}", timeout=5)
        data = json.loads(resp.read())
        results = {}
        for r in data.get("data", {}).get("result", []):
            label = _label_key(r["metric"])
            results[label] = float(r["value"][1])
        return results
    except Exception:
        return {}


def query_logs(logsql: str, limit: int = 50, start: int = None, end: int = None) -> list[dict]:
    """Query VictoriaLogs with LogsQL. Returns list of log entries."""
    now = int(time.time())
    params = {"query": logsql, "limit": str(limit)}
    if start:
        params["start"] = str(start)
    if end:
        params["end"] = str(end)

    qs = urllib.parse.urlencode(params)
    try:
        resp = urllib.request.urlopen(f"{VL_URL}/select/logsql/query?{qs}", timeout=5)
        results = []
        for line in resp.read().decode().strip().split("\n"):
            if line:
                results.append(json.loads(line))
        return results
    except Exception as e:
        return [{"error": str(e)}]


def push_log(msg: str, node: str = "mgmt", app: str = "bedrock", level: str = "info"):
    """Push a structured log entry to VictoriaLogs."""
    entry = json.dumps({
        "_msg": msg,
        "_time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "hostname": node,
        "app": app,
        "level": level,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{VL_URL}/insert/jsonline",
            data=entry,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
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
