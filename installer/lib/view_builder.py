"""Materialised-view builder.

Replays the bedrock-rust log via IPC and folds typed entries into the
two on-disk JSON files Bedrock has historically maintained ad-hoc:

  /etc/bedrock/cluster.json  — cluster_name, cluster_uuid, nodes,
                                tiers, witnesses, params
  /etc/bedrock/state.json    — this node's role + mgmt_url + witness_host

The log is canonical (design §3); these JSON files are caches that any
consumer (the FastAPI app, `bedrock storage status`, the operator
running `cat`) can read. Rebuild them with `rebuild()` whenever the
log moves; on a real cluster a small daemon will do this in response
to commit events. v0.1 just provides the rebuild function — callers
invoke it explicitly after an append.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import log_entries as le
from . import rust_ipc


CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")


def empty_snapshot() -> dict:
    """The starting shape of a snapshot — keep callers from forgetting
    a key when they fold incrementally."""
    return {
        "cluster_name": None,
        "cluster_uuid": None,
        "nodes": {},
        "tiers": {},
        "witnesses": {},
        "params": {},
        "mgmt_master": None,
        "vms": {},
        "backup_targets": {},
        # paths: keyed by canonical-form (a < b alphabetically) so each
        # path appears exactly once. Direction-symmetric.
        # Key = "node_a|nic_a|node_b|nic_b" (always in canonical order).
        # Value = {speed_mbps, rtt_us, observed_at}.
        "paths": {},
        "log_index": 0,
    }


def _path_key(node_a: str, nic_a: str, node_b: str, nic_b: str) -> str:
    """Canonical-order key for path table dedup. Sort by (node, nic) tuple
    so the same physical path is identified the same way no matter which
    end first reported it."""
    a = (node_a, nic_a)
    b = (node_b, nic_b)
    if a > b:
        a, b = b, a
    return f"{a[0]}|{a[1]}|{b[0]}|{b[1]}"


def fold(entries: list[dict]) -> dict:
    """Fold an ordered list of decoded log-entry payloads into a
    cluster-shaped dict. Pure function — easy to unit-test.

    Returns:
        {
            "cluster_name": str,
            "cluster_uuid": str,
            "nodes": {name: {host, drbd_ip, role, pubkey}},
            "tiers": {tier: {mode, master, peers, drbd_node_ids,
                              backend_path, garage_endpoint, version}},
            "witnesses": {wid: {addr, witness_pubkey, encrypted_witness_key}},
            "params": {key: value},
            "mgmt_master": str | None,
            "log_index": int,    # index of the last folded entry
        }
    """
    return fold_into(empty_snapshot(), entries)


def fold_into(out: dict, entries: list[dict]) -> dict:
    """Same as fold(), but folds new entries onto an EXISTING snapshot
    in place (and returns it). The watcher uses this to keep an
    in-memory snapshot up to date on every poll without replaying
    the entire log — only the entries since `out['log_index']` are
    folded. O(new_entries), not O(log_size).

    Caller is responsible for passing entries in strictly-increasing
    index order and only entries newer than `out['log_index']`. The
    fold itself is idempotent on re-fold of the same entry, but
    duplicate folds waste cycles.
    """
    for entry in entries:
        payload = le.decode(entry["payload"])
        kind = payload.get("t")
        out["log_index"] = entry["index"]

        if kind == le.BOOTSTRAP:
            # First entry in every log; carries the cluster's permanent
            # uuid. cluster_name is set by a later cluster_init entry.
            out["cluster_uuid"] = payload["uuid"]

        elif kind == le.CLUSTER_INIT:
            out["cluster_name"] = payload["name"]
            # Update uuid only if it differs from the bootstrap one
            # (it shouldn't, but the explicit cluster_init entry exists
            # so an operator can rename a cluster without rewriting
            # every node's bootstrap).
            out["cluster_uuid"] = payload["uuid"]

        elif kind == le.NODE_REGISTER:
            n = payload["node_name"]
            existing = out["nodes"].get(n, {})
            existing.update({
                "host": payload["host"],
                "drbd_ip": payload["drbd_ip"],
                "role": payload.get("role", "compute"),
                "pubkey": payload.get("pubkey", ""),
            })
            out["nodes"][n] = existing

        elif kind == le.NODE_UNREGISTER:
            out["nodes"].pop(payload["node_name"], None)
            # Also strip from tier peer lists.
            for t in out["tiers"].values():
                if payload["node_name"] in t.get("peers", []):
                    t["peers"] = [p for p in t["peers"] if p != payload["node_name"]]
                t.get("drbd_node_ids", {}).pop(payload["node_name"], None)

        elif kind == le.MGMT_MASTER:
            old = out["mgmt_master"]
            new = payload["node_name"]
            out["mgmt_master"] = new
            for n_name, info in out["nodes"].items():
                if n_name == new:
                    info["role"] = "mgmt+compute"
                elif n_name == old and info.get("role") == "mgmt+compute":
                    info["role"] = "compute"

        elif kind == le.TIER_STATE:
            tier = payload["tier"]
            existing = out["tiers"].get(tier, {})
            existing["mode"] = payload["mode"]
            if payload.get("master") is not None:
                existing["master"] = payload["master"]
            if payload.get("peers"):
                existing["peers"] = list(payload["peers"])
            if payload.get("backend_path") is not None:
                existing["backend_path"] = payload["backend_path"]
            if payload.get("garage_endpoint") is not None:
                existing["garage_endpoint"] = payload["garage_endpoint"]
            existing["version"] = existing.get("version", 0) + 1
            out["tiers"][tier] = existing

        elif kind == le.DRBD_NODE_ID:
            tier = payload["tier"]
            t = out["tiers"].setdefault(tier, {"mode": "local"})
            ids = t.setdefault("drbd_node_ids", {})
            ids[payload["node_name"]] = payload["node_id"]

        elif kind == le.DRBD_NODE_ID_FREED:
            tier = payload["tier"]
            t = out["tiers"].get(tier)
            if t is not None:
                t.get("drbd_node_ids", {}).pop(payload["node_name"], None)

        elif kind == le.WITNESS_REGISTER:
            wid = payload["witness_id"]
            out["witnesses"][wid] = {
                "addr": payload["addr"],
                "witness_pubkey": payload["witness_pubkey"],
                "encrypted_witness_key": payload["encrypted_witness_key"],
            }

        elif kind == le.WITNESS_UNREGISTER:
            out["witnesses"].pop(payload["witness_id"], None)

        elif kind == le.PARAM_CHANGE:
            out["params"][payload["key"]] = payload["value"]

        elif kind == le.VM_CREATE_INTENT:
            # Intent is pre-create. Record it as state="creating" so
            # crash recovery can find unfinished creates and decide
            # whether to resume or roll back. A subsequent VM_CREATED
            # or VM_CREATE_FAILED entry settles the outcome.
            out["vms"][payload["name"]] = {
                "vm_type": payload.get("vm_type", "cattle"),
                "host":    payload.get("host", ""),
                "ram_mb":  payload.get("ram_mb", 0),
                "disk_gb": payload.get("disk_gb", 0),
                "state":   "creating",
                "intent_index": entry["index"],
            }

        elif kind == le.VM_CREATED:
            vm = out["vms"].setdefault(payload["name"], {})
            vm.update({
                "vm_type": payload.get("vm_type", vm.get("vm_type", "cattle")),
                "host":    payload.get("host", vm.get("host", "")),
                "ram_mb":  payload.get("ram_mb", vm.get("ram_mb", 0)),
                "disk_gb": payload.get("disk_gb", vm.get("disk_gb", 0)),
                "state":   "created",
            })

        elif kind == le.VM_CREATE_FAILED:
            vm = out["vms"].setdefault(payload["name"], {})
            vm["state"] = "create_failed"
            vm["fail_reason"] = payload.get("reason", "")

        elif kind == le.VM_DESTROYED:
            out["vms"].pop(payload["name"], None)

        elif kind == le.VM_MIGRATED:
            vm = out["vms"].get(payload["name"])
            if vm is not None:
                vm["host"] = payload.get("dst_host", vm.get("host"))

        elif kind == le.VM_STATE_CHANGE:
            vm = out["vms"].get(payload["name"])
            if vm is not None:
                vm["state"] = payload.get("state", vm.get("state"))
                if payload.get("host"):
                    vm["host"] = payload["host"]

        elif kind == le.NODE_MAINTENANCE:
            n = out["nodes"].get(payload["node_name"])
            if n is not None:
                n["maintenance"] = bool(payload.get("on", False))

        # ── mesh path table ──────────────────────────────────────────
        elif kind == le.NODE_LOOPBACK:
            n = out["nodes"].setdefault(payload["node_name"], {})
            n["loopback_ip"] = payload["loopback_ip"]

        elif kind in (le.LINK_UP, le.LINK_QUALITY):
            key = _path_key(payload["node_a"], payload["nic_a"],
                            payload["node_b"], payload["nic_b"])
            entry_age = float(payload.get("observed_at", 0.0))
            existing = out["paths"].get(key, {})
            # LINK_UP creates the entry; LINK_QUALITY only updates if
            # the entry already exists (we don't want a quality update
            # to resurrect a path that was explicitly torn down).
            if kind == le.LINK_QUALITY and not existing:
                continue
            out["paths"][key] = {
                "node_a": payload["node_a"], "nic_a": payload["nic_a"],
                "node_b": payload["node_b"], "nic_b": payload["nic_b"],
                "speed_mbps": int(payload.get("speed_mbps", 0)),
                "rtt_us": int(payload.get("rtt_us", 0)),
                "observed_at": entry_age,
                "up_since": existing.get("up_since", entry_age) if kind == le.LINK_QUALITY else entry_age,
            }

        elif kind == le.LINK_DOWN:
            key = _path_key(payload["node_a"], payload["nic_a"],
                            payload["node_b"], payload["nic_b"])
            out["paths"].pop(key, None)

        # ── backup ─────────────────────────────────────────────────
        elif kind == le.BACKUP_TARGET_SET:
            targets = out.setdefault("backup_targets", {})
            tid = payload["target_id"]
            existing = targets.get(tid, {})
            existing.update({
                "id":   tid,
                "kind": payload.get("kind", "kopia-s3"),
                "s3_endpoint":     payload.get("s3_endpoint", ""),
                "s3_bucket":       payload.get("s3_bucket", ""),
                "s3_region":       payload.get("s3_region", ""),
                "s3_disable_tls":              bool(payload.get("s3_disable_tls", False)),
                "s3_disable_tls_verification": bool(payload.get("s3_disable_tls_verification", False)),
                "filesystem_path": payload.get("filesystem_path", ""),
                "override_source_prefix": payload.get("override_source_prefix", ""),
                "cache_directory": payload.get("cache_directory", ""),
            })
            targets[tid] = existing

        elif kind == le.BACKUP_TARGET_REMOVED:
            (out.get("backup_targets") or {}).pop(payload["target_id"], None)

        elif kind == le.BACKUP_DONE:
            vm = out["vms"].setdefault(payload["vm"], {})
            backups = vm.setdefault("backups", [])
            # Multi-disk schema: `disks` is the canonical list. Legacy
            # entries (singular `kopia_snapshot_id` at the top) get
            # normalised into a 1-element list so the UI/restore path
            # has one shape to deal with.
            disks = payload.get("disks")
            if not disks:
                disks = [{
                    "target_dev":  "disk0",
                    "lv_path":     "",
                    "kopia_snapshot_id": payload.get("kopia_snapshot_id", ""),
                    "bytes_added": payload.get("bytes_added", 0),
                }]
            total_bytes = sum(int(d.get("bytes_added", 0) or 0) for d in disks)
            primary_kid = (disks[0].get("kopia_snapshot_id") or "") if disks else ""
            backups.append({
                # `kopia_snapshot_id` stays at the top as the row's
                # primary identifier (= disk0's kopia id); the UI uses it
                # for delete/restore lookup. `disks[]` is authoritative.
                "kopia_snapshot_id": primary_kid,
                "disks":             disks,
                "target_id":   payload["target_id"],
                "source_node": payload.get("source_node", ""),
                "bytes_added": total_bytes,    # rolled-up across disks
                "duration_s":  payload.get("duration_s", 0.0),
                "label":       payload.get("label", ""),
                "fs_freeze_used": bool(payload.get("fs_freeze_used", False)),
                "ts_index":    entry["index"],   # log index serves as timestamp
            })
            # Keep newest first; cap to a reasonable length so cluster.json
            # doesn't bloat unboundedly. Older entries still in the log
            # if anyone wants to replay.
            backups.sort(key=lambda b: b["ts_index"], reverse=True)
            del backups[200:]

        elif kind == le.BACKUP_FAILED:
            vm = out["vms"].setdefault(payload["vm"], {})
            vm["last_backup_error"] = {
                "ts_index": entry["index"],
                "target_id": payload.get("target_id", ""),
                "reason": payload.get("reason", ""),
            }

        elif kind == le.BACKUP_DELETED:
            vm = out["vms"].get(payload["vm"])
            if vm is not None:
                vm["backups"] = [
                    b for b in (vm.get("backups") or [])
                    if b.get("kopia_snapshot_id") != payload["kopia_snapshot_id"]
                ]

        elif kind == le.RESTORE_DONE:
            vm = out["vms"].setdefault(payload["vm"], {})
            vm["last_restore"] = {
                "ts_index":          entry["index"],
                "kopia_snapshot_id": payload["kopia_snapshot_id"],
                "target_id":         payload.get("target_id", ""),
                "dest_node":         payload.get("dest_node", ""),
            }

        elif kind == le.RESTORE_FAILED:
            vm = out["vms"].setdefault(payload["vm"], {})
            vm["last_restore_error"] = {
                "ts_index":          entry["index"],
                "kopia_snapshot_id": payload.get("kopia_snapshot_id", ""),
                "target_id":         payload.get("target_id", ""),
                "reason":            payload.get("reason", ""),
            }

        elif kind == le.BACKUP_SCHEDULE_SET:
            vm = out["vms"].setdefault(payload["vm"], {})
            vm["backup_schedule"] = {
                "target_id":       payload.get("target_id", ""),
                "cron_expr":       payload.get("cron_expr", ""),
                "label_prefix":    payload.get("label_prefix", "auto"),
                "retention_count": int(payload.get("retention_count", 0)),
                "set_at_index":    entry["index"],
            }

        elif kind == le.BACKUP_SCHEDULE_REMOVED:
            vm = out["vms"].get(payload["vm"])
            if vm is not None:
                vm.pop("backup_schedule", None)

        # Bootstrap entry, free-form payloads, and unknown kinds are
        # ignored — they just record history without affecting the
        # materialised view.
    return out


def rebuild(sock_path: str = rust_ipc.DEFAULT_SOCK,
            cluster_json: Path = CLUSTER_JSON,
            state_json: Path = STATE_JSON,
            *,
            this_node: str | None = None) -> dict:
    """Pull the whole log via IPC, fold it, and rewrite the JSON caches.

    `this_node` is used to project the cluster-wide view onto
    state.json (each node's state.json holds *its* role; cluster.json
    is identical on every node).
    """
    with rust_ipc.Daemon(sock_path) as d:
        entries = list(d.read(from_index=1))
    view = fold(entries)

    cluster_json.parent.mkdir(parents=True, exist_ok=True)
    cluster_json.write_text(json.dumps(_cluster_view(view), indent=2))

    if this_node and this_node in view["nodes"]:
        state_json.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if state_json.exists():
            try:
                existing = json.loads(state_json.read_text())
            except json.JSONDecodeError:
                existing = {}
        existing.update(_state_view(view, this_node))
        state_json.write_text(json.dumps(existing, indent=2))

    return view


def _cluster_view(v: dict) -> dict:
    """The cluster.json shape — cluster-wide canonical view."""
    return {
        "cluster_name":   v["cluster_name"],
        "cluster_uuid":   v["cluster_uuid"],
        "nodes":          v["nodes"],
        "tiers":          v["tiers"],
        "witnesses":      v["witnesses"],
        "params":         v["params"],
        "vms":            v.get("vms", {}),
        "backup_targets": v.get("backup_targets", {}),
        # Mesh path table — replicated topology, not per-node liveness.
        # Each node's bedrock-net daemon also keeps a sub-second gossip
        # view in memory; only durable transitions land here.
        "paths":          v.get("paths", {}),
        "log_index":      v["log_index"],
    }


def _state_view(v: dict, node_name: str) -> dict:
    """The state.json shape — this node's POV."""
    me = v["nodes"].get(node_name, {})
    master = v.get("mgmt_master")
    master_host = (
        v["nodes"].get(master, {}).get("host", "") if master else ""
    )
    return {
        "node_name": node_name,
        "cluster_name": v["cluster_name"],
        "cluster_uuid": v["cluster_uuid"],
        "role": me.get("role", "compute"),
        "mgmt_ip": me.get("host", ""),
        "drbd_ip": me.get("drbd_ip", ""),
        # Mesh identity — read by bedrock-net.service to know which /32
        # to claim on `lo` and to advertise in probes.
        "loopback_ip": me.get("loopback_ip", ""),
        "mgmt_url": f"http://{master_host}:8080" if master_host else "",
        "witness_host": master_host,  # v0.1 — phase 6 swaps in real witnesses
    }
