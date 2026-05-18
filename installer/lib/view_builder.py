"""Materialised-view builder — rqlite edition.

Reads the cluster state from rqlite (the post-alpha rewrite's new
state store, D-01..D-22) and projects it into the on-disk JSON
files Bedrock has historically maintained:

  /etc/bedrock/cluster.json  — cluster-wide canonical view
  /etc/bedrock/state.json    — this node's role + mgmt_url

The rqlite tables ARE the canonical store; these JSON files are
caches that any consumer (the FastAPI app, `bedrock storage status`,
the operator running `cat`) can read without a SQL round-trip.
Callers invoke `rebuild()` whenever the cluster's
bedrock_meta.revision advances; on a real cluster the orchestrator's
`rqlite_subscriber` task does this in a watch loop.

The output shape is IDENTICAL to the pre-rqlite log-replay version
of this module — all downstream consumers (mgmt/app.py,
orchestrator.py reactor, dashboard, CLI verbs) see the same dict
structure they always did. Only the source of data changed.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from . import rqlite_client

log = logging.getLogger("bedrock.view_builder")

CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")


def empty_snapshot() -> dict:
    """The starting shape of a snapshot — kept around so callers
    don't have to remember the full key list. Matches the shape
    that build_snapshot() returns for an empty cluster."""
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
        "paths": {},
        "operators": {},
        "join_requests": {},
        "obs_backends": {"metrics": [], "logs": []},
        # log_index name retained for backward-compat with consumers
        # that read the field name; semantically it's now the
        # bedrock_meta.revision (the rqlite monotonic counter).
        "log_index": 0,
    }


# ─────────────────────────────────────────────────────────────────────
# build_snapshot — read all relevant tables and assemble the dict
# ─────────────────────────────────────────────────────────────────────


def build_snapshot(client: Optional[rqlite_client.RqliteClient] = None,
                   *, level: str = "weak") -> dict:
    """Read the rqlite cluster-state tables and assemble a snapshot
    dict matching the historical log-fold output shape.

    `level` follows rqlite's read-consistency knob — 'weak' (default)
    reads from this node's local Raft follower replica (sub-second
    freshness, no leader round-trip); 'strong' goes via the leader
    for linearizable reads. Use 'strong' only when the caller must
    see a just-committed write.
    """
    owns_client = client is None
    if owns_client:
        client = rqlite_client.RqliteClient()

    try:
        out = empty_snapshot()

        # cluster_info (singleton row)
        ci = client.query_one(
            "SELECT cluster_uuid, cluster_name, mgmt_master "
            "FROM cluster_info WHERE id = 1",
            level=level,
        )
        if ci:
            out["cluster_uuid"] = ci["cluster_uuid"]
            out["cluster_name"] = ci["cluster_name"]
            out["mgmt_master"] = ci["mgmt_master"]

        # bedrock_meta.revision → "log_index" for back-compat field
        meta = client.query_one(
            "SELECT revision FROM bedrock_meta WHERE id = 1",
            level=level,
        )
        if meta:
            out["log_index"] = int(meta["revision"])

        # nodes
        for row in client.query(
            "SELECT node_name, host, drbd_ip, loopback_ip, role, "
            "pubkey, bedrock_pubkey, maintenance FROM nodes",
            level=level,
        ):
            entry = {
                "host": row["host"],
                "drbd_ip": row["drbd_ip"],
                "loopback_ip": row.get("loopback_ip", ""),
                "role": row.get("role", "compute"),
                "pubkey": row.get("pubkey", ""),
                "bedrock_pubkey": row.get("bedrock_pubkey", ""),
            }
            if row.get("maintenance"):
                entry["maintenance"] = bool(row["maintenance"])
            out["nodes"][row["node_name"]] = entry

        # tiers (+ drbd_node_ids per tier)
        tier_rows = client.query(
            "SELECT tier_name, mode, master, peers, backend_path, "
            "garage_endpoint, version FROM tiers",
            level=level,
        )
        for row in tier_rows:
            tier_obj: dict = {
                "mode": row["mode"],
                "version": int(row.get("version") or 0),
            }
            if row.get("master") is not None:
                tier_obj["master"] = row["master"]
            try:
                tier_obj["peers"] = json.loads(row.get("peers") or "[]")
            except (TypeError, json.JSONDecodeError):
                tier_obj["peers"] = []
            if row.get("backend_path") is not None:
                tier_obj["backend_path"] = row["backend_path"]
            if row.get("garage_endpoint") is not None:
                tier_obj["garage_endpoint"] = row["garage_endpoint"]
            out["tiers"][row["tier_name"]] = tier_obj

        for row in client.query(
            "SELECT tier_name, node_name, node_id FROM tier_drbd_node_ids",
            level=level,
        ):
            t = out["tiers"].setdefault(row["tier_name"], {"mode": "local"})
            t.setdefault("drbd_node_ids", {})[row["node_name"]] = int(row["node_id"])

        # witnesses
        for row in client.query(
            "SELECT witness_id, addr, witness_pubkey, "
            "encrypted_witness_key, backend FROM witnesses",
            level=level,
        ):
            entry = {
                "addr": row["addr"],
                "witness_pubkey": row["witness_pubkey"],
                "encrypted_witness_key": row["encrypted_witness_key"],
            }
            # D-17: backend column added so multi-backend (Echo /
            # SMB / NFS / S3) is operator-visible. Default 'echo'
            # for back-compat with consumers expecting just the key.
            if row.get("backend"):
                entry["backend"] = row["backend"]
            out["witnesses"][row["witness_id"]] = entry

        # params (open-ended k/v, value is JSON-encoded)
        for row in client.query(
            "SELECT key, value FROM params",
            level=level,
        ):
            try:
                out["params"][row["key"]] = json.loads(row["value"])
            except (TypeError, json.JSONDecodeError):
                out["params"][row["key"]] = row["value"]

        # operators (login users)
        for row in client.query(
            "SELECT username, salt, password_hash FROM operators",
            level=level,
        ):
            out["operators"][row["username"]] = {
                "salt": row["salt"],
                "hash": row["password_hash"],
            }

        # join_requests
        for row in client.query(
            "SELECT request_id, node_name, host, bedrock_pubkey, "
            "x25519_eph_pubkey, fingerprint, state, "
            "master_eph_pubkey, ciphertext, nonce, reason "
            "FROM join_requests",
            level=level,
        ):
            entry = {
                "node_name": row["node_name"],
                "host": row["host"],
                "bedrock_pubkey": row["bedrock_pubkey"],
                "x25519_eph_pubkey": row["x25519_eph_pubkey"],
                "fingerprint": row["fingerprint"],
                "state": row["state"],
            }
            if row["state"] == "approved":
                entry["master_eph_pubkey"] = row.get("master_eph_pubkey", "")
                entry["ciphertext"] = row.get("ciphertext", "")
                entry["nonce"] = row.get("nonce", "")
            elif row["state"] == "rejected":
                entry["reason"] = row.get("reason", "")
            out["join_requests"][row["request_id"]] = entry

        # vms (current declared/state) + per-VM backups history
        vm_rows = client.query(
            "SELECT vm_name, vm_type, host, ram_mb, disk_gb, state, "
            "intent_index, fail_reason, backup_schedule, "
            "last_backup_error, last_restore, last_restore_err "
            "FROM vms",
            level=level,
        )
        for row in vm_rows:
            vm: dict = {
                "vm_type": row.get("vm_type", "cattle"),
                "host":    row.get("host", ""),
                "ram_mb":  int(row.get("ram_mb") or 0),
                "disk_gb": int(row.get("disk_gb") or 0),
                "state":   row.get("state", "created"),
            }
            if row.get("intent_index") is not None:
                vm["intent_index"] = int(row["intent_index"])
            if row.get("fail_reason"):
                vm["fail_reason"] = row["fail_reason"]
            for src_col, dst_key in (
                ("backup_schedule",  "backup_schedule"),
                ("last_backup_error", "last_backup_error"),
                ("last_restore",     "last_restore"),
                ("last_restore_err", "last_restore_error"),
            ):
                v = row.get(src_col)
                if v:
                    try:
                        vm[dst_key] = json.loads(v)
                    except (TypeError, json.JSONDecodeError):
                        pass
            out["vms"][row["vm_name"]] = vm

        # vm_backups — attach to the owning VM in newest-first order,
        # capped at 200 like the old fold did.
        for row in client.query(
            "SELECT vm_name, primary_kopia_id, disks, target_id, "
            "source_node, bytes_added, duration_s, label, "
            "fs_freeze_used, ts_index "
            "FROM vm_backups ORDER BY ts_index DESC LIMIT 1000",
            level=level,
        ):
            vm = out["vms"].setdefault(row["vm_name"], {})
            backups = vm.setdefault("backups", [])
            if len(backups) >= 200:
                continue
            try:
                disks = json.loads(row["disks"])
            except (TypeError, json.JSONDecodeError):
                disks = []
            backups.append({
                "kopia_snapshot_id": row.get("primary_kopia_id", ""),
                "disks":             disks,
                "target_id":         row["target_id"],
                "source_node":       row.get("source_node", ""),
                "bytes_added":       int(row.get("bytes_added") or 0),
                "duration_s":        float(row.get("duration_s") or 0),
                "label":             row.get("label", ""),
                "fs_freeze_used":    bool(row.get("fs_freeze_used")),
                "ts_index":          int(row["ts_index"]),
            })

        # backup_targets
        for row in client.query(
            "SELECT target_id, kind, s3_endpoint, s3_bucket, s3_region, "
            "s3_disable_tls, s3_disable_tls_verification, "
            "filesystem_path, override_source_prefix, cache_directory "
            "FROM backup_targets",
            level=level,
        ):
            out["backup_targets"][row["target_id"]] = {
                "id":   row["target_id"],
                "kind": row.get("kind", "kopia-s3"),
                "s3_endpoint":     row.get("s3_endpoint", ""),
                "s3_bucket":       row.get("s3_bucket", ""),
                "s3_region":       row.get("s3_region", ""),
                "s3_disable_tls":              bool(row.get("s3_disable_tls")),
                "s3_disable_tls_verification": bool(row.get("s3_disable_tls_verification")),
                "filesystem_path":        row.get("filesystem_path", ""),
                "override_source_prefix": row.get("override_source_prefix", ""),
                "cache_directory":        row.get("cache_directory", ""),
            }

        # paths (mesh topology)
        for row in client.query(
            "SELECT path_key, node_a, nic_a, link_addr_a, "
            "node_b, nic_b, link_addr_b, speed_mbps, rtt_us, "
            "observed_at, up_since FROM paths",
            level=level,
        ):
            out["paths"][row["path_key"]] = {
                "node_a":      row["node_a"],
                "nic_a":       row["nic_a"],
                "link_addr_a": row.get("link_addr_a", ""),
                "node_b":      row["node_b"],
                "nic_b":       row["nic_b"],
                "link_addr_b": row.get("link_addr_b", ""),
                "speed_mbps":  int(row.get("speed_mbps") or 0),
                "rtt_us":      int(row.get("rtt_us") or 0),
                "observed_at": float(row.get("observed_at") or 0),
                "up_since":    float(row.get("up_since") or 0),
            }

        # obs_backends (stack, position) -> ordered list per stack
        rows = client.query(
            "SELECT stack, node_name, position FROM obs_backends "
            "ORDER BY stack, position",
            level=level,
        )
        backends: dict[str, list[str]] = {"metrics": [], "logs": []}
        for row in rows:
            backends.setdefault(row["stack"], []).append(row["node_name"])
        out["obs_backends"] = backends

        return out
    finally:
        if owns_client:
            client.close()


# ─────────────────────────────────────────────────────────────────────
# Projections — cluster.json + state.json
# ─────────────────────────────────────────────────────────────────────


def _cluster_view(v: dict) -> dict:
    """The cluster.json shape — cluster-wide canonical view."""
    return {
        "cluster_name":   v["cluster_name"],
        "cluster_uuid":   v["cluster_uuid"],
        "mgmt_master":    v.get("mgmt_master"),
        "nodes":          v["nodes"],
        "tiers":          v["tiers"],
        "witnesses":      v["witnesses"],
        "params":         v["params"],
        "vms":            v.get("vms", {}),
        "backup_targets": v.get("backup_targets", {}),
        "paths":          v.get("paths", {}),
        "operators":      v.get("operators", {}),
        "join_requests":  v.get("join_requests", {}),
        "obs_backends":   v.get("obs_backends", {"metrics": [], "logs": []}),
        # Field name kept as 'log_index' for back-compat with existing
        # consumers; semantically this is the rqlite revision.
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
        "node_name":    node_name,
        "cluster_name": v["cluster_name"],
        "cluster_uuid": v["cluster_uuid"],
        "role":         me.get("role", "compute"),
        "mgmt_ip":      me.get("host", ""),
        "drbd_ip":      me.get("drbd_ip", ""),
        "loopback_ip":  me.get("loopback_ip", ""),
        "mgmt_url":     f"https://{master_host}:8443" if master_host else "",
        "witness_host": master_host,
    }


# ─────────────────────────────────────────────────────────────────────
# rebuild — refresh on-disk caches from rqlite
# ─────────────────────────────────────────────────────────────────────


def rebuild(cluster_json: Path = CLUSTER_JSON,
            state_json: Path = STATE_JSON,
            *,
            this_node: str | None = None,
            client: Optional[rqlite_client.RqliteClient] = None,
            level: str = "weak") -> dict:
    """Read rqlite, project, and rewrite cluster.json + state.json.

    `this_node` projects the cluster-wide view onto state.json (each
    node's state.json holds *its* role; cluster.json is identical on
    every node).
    """
    view = build_snapshot(client=client, level=level)

    cluster_json.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(cluster_json, _cluster_view(view))

    if this_node and this_node in view["nodes"]:
        state_json.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if state_json.exists():
            try:
                existing = json.loads(state_json.read_text())
            except json.JSONDecodeError:
                existing = {}
        existing.update(_state_view(view, this_node))
        _atomic_write_json(state_json, existing)

    return view


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Same per-call unique-tmp pattern orchestrator.py uses — see
    L48 / lesson_orchestrator_atomic_write for the race the simple
    tmp+rename pattern was hitting before."""
    import os
    import tempfile
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(obj, indent=2))
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────
# fold_into — back-compat shim for orchestrator's incremental updates
# ─────────────────────────────────────────────────────────────────────


def fold_into(out: dict, entries: list) -> dict:
    """Back-compat: replaced by direct rqlite reads.

    The pre-rqlite version of view_builder folded log entries
    incrementally so the orchestrator could keep an in-memory
    snapshot up to date entry-by-entry. Under rqlite, the snapshot
    IS the database — there are no "entries" to fold. The
    orchestrator's rqlite_subscriber should call build_snapshot()
    directly when a revision-change is observed.

    This function is kept as a compatibility shim that simply
    rebuilds the snapshot from rqlite. Any callers passing log-
    entry dicts will see them ignored. Tracked as deprecated;
    delete in v1.0 once orchestrator is migrated.
    """
    log.debug("view_builder.fold_into is deprecated under rqlite; "
              "rebuilding from current state instead")
    new = build_snapshot()
    out.clear()
    out.update(new)
    return out
