"""Structured event log → VictoriaLogs.

Lib-level (callable from bedrock_state + the sagas on ANY node, unlike mgmt's
push_log) so the single chokepoint where VM state changes can record a baseline
event, while richer/error events are emitted at the call sites that hold the
detail (the exact virsh/qemu error, a storage move's source→dest).

Design:
  * STRUCTURED — each event is JSON with an ``event`` type + arbitrary fields,
    so LogsQL can filter/group precisely ("all vm_lifecycle for web1 today").
  * PER-VM/PER-NODE STREAMS — ``vm`` + ``node`` are declared as VictoriaLogs
    stream fields, so each machine is its own log stream: cheap to filter and to
    LIVE-TAIL (the dashboard tails a VM's events instead of polling virsh).
  * NON-BLOCKING + BEST-EFFORT — the POST runs on a short-lived daemon thread so
    it never slows a state transition, and a backend being down is swallowed
    (telemetry must never break a VM operation). An UNEXPECTED error is logged
    loudly to stderr (never silently lost) but still can't escape into the caller.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request

log = logging.getLogger("bedrock.event_log")

_VL_PORT = 9428
_TIMEOUT_S = 2.0


def _vl_urls() -> list:
    """VictoriaLogs ingest URLs (designated log backends, local first). Falls
    back to loopback so a single-node / pre-obs cluster still records locally."""
    try:
        try:
            from . import cluster_state            # type: ignore
        except ImportError:                          # pragma: no cover
            import cluster_state                      # type: ignore
        cluster = cluster_state.load_cluster()
    except Exception:
        return [f"http://127.0.0.1:{_VL_PORT}"]
    backends = (cluster.get("obs_backends") or {}).get("logs") or []
    nodes = cluster.get("nodes") or {}
    try:
        my = socket.gethostname()
    except Exception:
        my = ""
    urls: list = []
    for name in backends:
        if name == my:
            urls.insert(0, f"http://127.0.0.1:{_VL_PORT}")
            continue
        info = nodes.get(name) or {}
        host = info.get("loopback_ip") or info.get("host", "")
        if host:
            urls.append(f"http://{host}:{_VL_PORT}")
    return urls or [f"http://127.0.0.1:{_VL_PORT}"]


def _post(entry: dict, stream_fields: str) -> None:
    """Dual-write the entry to every log backend (best-effort). Runs on a worker
    thread. Network failures are swallowed (a backend may be down); only an
    unexpected build/encode bug is logged loudly."""
    try:
        data = json.dumps(entry).encode()
        qs = (f"?_stream_fields={stream_fields}"
              "&_msg_field=_msg&_time_field=_time")
        ok = False
        for base in _vl_urls():
            try:
                req = urllib.request.Request(
                    base + "/insert/jsonline" + qs, data=data,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=_TIMEOUT_S)
                ok = True
            except Exception:
                continue            # this backend down — try the next (HA)
        if not ok:
            log.debug("event_log: no VL backend accepted event %r",
                      entry.get("event"))
    except Exception:               # a real bug (not a down backend) — fail loud
        log.warning("event_log: failed to emit event %r",
                    entry.get("event"), exc_info=True)


def _build_entry(event: str, msg: str, vm: str, node: str, level: str,
                 fields: dict) -> tuple:
    """Pure: build the (entry, stream_fields) for an event. Empty/None extra
    fields are dropped; vm+node are the stream identifiers."""
    if not node:
        try:
            node = socket.gethostname()
        except Exception:
            node = "?"
    entry = {
        "_msg": msg,
        "_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "app": "bedrock",
        "level": level,
        "node": node,
    }
    if vm:
        entry["vm"] = vm
    for k, v in fields.items():
        if v != "" and v is not None:
            entry[k] = v if isinstance(v, (int, float, bool)) else str(v)
    return entry, ("vm,node" if vm else "node")


def emit(event: str, msg: str, *, vm: str = "", node: str = "",
         level: str = "info", **fields) -> None:
    """Record a structured event to VictoriaLogs. ``event`` is the type
    (e.g. 'vm_lifecycle', 'vm_error', 'storage_move'); ``vm``+``node`` become the
    stream fields; extra kwargs become indexed log fields (empty ones dropped).
    Non-blocking + best-effort — never raises, never slows the caller."""
    try:
        entry, stream_fields = _build_entry(event, msg, vm, node, level, fields)
        threading.Thread(target=_post, args=(entry, stream_fields),
                         daemon=True).start()
    except Exception:               # building/spawning must never break a caller
        log.warning("event_log: emit(%r) failed", event, exc_info=True)
