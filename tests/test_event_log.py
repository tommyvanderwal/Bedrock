"""event_log — structured VM/storage events → VictoriaLogs. The entry shape +
per-VM stream fields are what LogsQL filters and the dashboard live-tails on, so
they're pinned here. emit() must be non-blocking + never raise (telemetry can't
break a VM operation)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import event_log as el  # noqa: E402


def test_entry_shape_and_per_vm_stream():
    entry, sf = el._build_entry(
        "vm_lifecycle", "VM web1 → running (operator)",
        vm="web1", node="node-a", level="info",
        fields={"state": "running", "reason": "operator", "empty": "", "n": None})
    assert entry["_msg"] == "VM web1 → running (operator)"
    assert entry["event"] == "vm_lifecycle"
    assert entry["vm"] == "web1" and entry["node"] == "node-a"
    assert entry["state"] == "running" and entry["reason"] == "operator"
    assert entry["_time"].endswith("Z")          # UTC RFC3339
    # empty / None extra fields dropped
    assert "empty" not in entry and "n" not in entry
    # per-VM stream: vm + node identify the stream (cheap filter + live tail)
    assert sf == "vm,node"


def test_node_only_stream_when_no_vm():
    entry, sf = el._build_entry("cluster_event", "quorum lost", vm="", node="n1",
                                level="warn", fields={})
    assert "vm" not in entry
    assert sf == "node"
    assert entry["level"] == "warn"


def test_numeric_fields_kept_as_is():
    entry, _ = el._build_entry("storage_move", "migrated", vm="v", node="n",
                               level="info", fields={"bytes": 1234, "ok": True})
    assert entry["bytes"] == 1234 and entry["ok"] is True


def test_emit_is_nonblocking_and_dispatches_entry(monkeypatch):
    captured = {}
    monkeypatch.setattr(el, "_post",
                        lambda entry, sf: captured.update(entry=entry, sf=sf))
    el.emit("vm_error", "qemu: failed to start", vm="web1",
            error="boom")                          # returns immediately
    import time
    for _ in range(50):
        if "entry" in captured:
            break
        time.sleep(0.01)
    assert captured["entry"]["event"] == "vm_error"
    assert captured["entry"]["error"] == "boom"
    assert captured["sf"] == "vm,node"


def test_post_swallows_unreachable_backends(monkeypatch):
    # the real _post must NOT raise when every backend is down (best-effort).
    monkeypatch.setattr(el, "_vl_urls", lambda: ["http://127.0.0.1:1"])  # nothing there
    el._post({"_msg": "x", "event": "t"}, "node")  # must return quietly
