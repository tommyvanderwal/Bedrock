"""Materialised-view builder.

Reads the cluster state from rqlite (the Raft-replicated SQLite store)
and assembles it into a single snapshot dict, the shape downstream
consumers (mgmt/app.py, orchestrator reactor, dashboard, CLI verbs)
expect.

The rqlite tables ARE the canonical store. `build_snapshot()` folds
all relevant tables into one dict so a consumer can read the whole
cluster view in a single call instead of issuing many SQL queries.
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
        "storage_endpoints": {},
        "params": {},
        "mgmt_master": None,
        "vms": {},
        "backup_targets": {},
        "paths": {},
        "operators": {},
        "join_requests": {},
        "obs_backends": {"metrics": [], "logs": []},
        # Named 'log_index' because consumers read that field name;
        # semantically it holds bedrock_meta.revision (the rqlite
        # monotonic counter).
        "log_index": 0,
    }


# ─────────────────────────────────────────────────────────────────────
# build_snapshot — read all relevant tables and assemble the dict
# ─────────────────────────────────────────────────────────────────────


def build_snapshot(client: Optional[rqlite_client.RqliteClient] = None,
                   *, level: str = "weak") -> dict:
    """Read the rqlite cluster-state tables and assemble a snapshot dict.

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

        # bedrock_meta.revision → exposed under the "log_index" key
        meta = client.query_one(
            "SELECT revision FROM bedrock_meta WHERE id = 1",
            level=level,
        )
        if meta:
            out["log_index"] = int(meta["revision"])

        # nodes
        for row in client.query(
            "SELECT node_name, host, loopback_ip, role, "
            "pubkey, bedrock_pubkey, maintenance, state FROM nodes",
            level=level,
        ):
            entry = {
                "host": row["host"],
                "loopback_ip": row.get("loopback_ip", ""),
                "role": row.get("role", "compute"),
                "pubkey": row.get("pubkey", ""),
                "bedrock_pubkey": row.get("bedrock_pubkey", ""),
                # Lifecycle gate for the election denominator (C1): only
                # 'active' (not 'joining') nodes count toward n_nodes.
                # maintenance is always carried so the election can
                # exclude a drained node consistently.
                "maintenance": bool(row.get("maintenance")),
                "state": row.get("state") or "active",
            }
            out["nodes"][row["node_name"]] = entry

        # tiers (+ drbd_node_ids per tier)
        tier_rows = client.query(
            "SELECT tier_name, mode, master, peers, backend_path, "
            "version FROM tiers",
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
            "encrypted_witness_key, backend, endpoint_id FROM witnesses",
            level=level,
        ):
            entry = {
                "addr": row["addr"],
                "witness_pubkey": row["witness_pubkey"],
                "encrypted_witness_key": row["encrypted_witness_key"],
            }
            # Carry the backend kind (echo / fileshare / s3) so the witness
            # transport is operator-visible. Only set when present;
            # absent means the consumer assumes the default backend.
            if row.get("backend"):
                entry["backend"] = row["backend"]
            # For fileshare/s3 witnesses, the storage comes from a shared
            # storage_endpoints row.
            if row.get("endpoint_id"):
                entry["endpoint_id"] = row["endpoint_id"]
            out["witnesses"][row["witness_id"]] = entry

        # storage_endpoints — the consolidated S3/SMB/NFS definition shared by
        # backup_targets + witnesses. SECRETS (s3_secret_key_enc,
        # fs_password_enc) are NOT projected into the snapshot: a consumer that
        # must mount/connect reads + unseals them on-demand directly from
        # rqlite. The snapshot carries only the location + a has-secret flag.
        try:
            for row in client.query(
                "SELECT endpoint_id, type, label, s3_endpoint, s3_bucket, "
                "s3_region, s3_prefix, s3_disable_tls, "
                "s3_disable_tls_verification, s3_access_key, fs_server, "
                "fs_share, fs_options, fs_username, s3_secret_key_enc, "
                "fs_password_enc FROM storage_endpoints",
                level=level,
            ):
                out["storage_endpoints"][row["endpoint_id"]] = {
                    "type": row["type"],
                    "label": row.get("label", ""),
                    "s3_endpoint": row.get("s3_endpoint", ""),
                    "s3_bucket": row.get("s3_bucket", ""),
                    "s3_region": row.get("s3_region", ""),
                    "s3_prefix": row.get("s3_prefix", ""),
                    "s3_disable_tls": bool(row.get("s3_disable_tls")),
                    "s3_disable_tls_verification": bool(row.get("s3_disable_tls_verification")),
                    "s3_access_key": row.get("s3_access_key", ""),
                    "fs_server": row.get("fs_server", ""),
                    "fs_share": row.get("fs_share", ""),
                    "fs_options": row.get("fs_options", ""),
                    "fs_username": row.get("fs_username", ""),
                    "has_s3_secret": bool(row.get("s3_secret_key_enc")),
                    "has_fs_password": bool(row.get("fs_password_enc")),
                }
        except Exception as e:
            log.warning("view_builder: storage_endpoints projection skipped "
                        "(table missing on a pre-unification cluster?): %s", e)

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
            "master_eph_pubkey, ciphertext, nonce, reason, "
            "node_cert_pem, ca_cert_pem "
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
                # Cluster CA + joiner's signed TLS cert (mTLS).
                # Projected into cluster.json so /api/join/status —
                # which reads from cluster.json, not directly from
                # rqlite — can surface them to the joiner.
                entry["node_cert_pem"] = row.get("node_cert_pem", "")
                entry["ca_cert_pem"]   = row.get("ca_cert_pem", "")
            elif row["state"] == "rejected":
                entry["reason"] = row.get("reason", "")
            out["join_requests"][row["request_id"]] = entry

        # vms (current declared/state) + per-VM backups history
        vm_rows = client.query(
            "SELECT vm_name, vm_type, host, ram_mb, disk_gb, state, "
            "intent_index, fail_reason, backup_schedule, "
            "last_backup_error, last_restore, last_restore_err, "
            "failover_order "
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
            try:
                vm["failover_order"] = json.loads(
                    row.get("failover_order") or "[]")
            except (TypeError, json.JSONDecodeError):
                vm["failover_order"] = []
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
        # capped at 200 per VM.
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
            "filesystem_path, override_source_prefix, cache_directory, "
            "is_mirror "
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
                # is_mirror: a sync-to destination, never independently created
                "is_mirror":      bool(row.get("is_mirror")),
                # multi-target mirrors (filled below from backup_target_sync)
                "sync_to":        [],
                "delete_orphans": False,
            }

        # backup multi-target mirrors — attach the ordered secondary list onto
        # each primary target's dict (no new top-level key; the dashboard/API
        # see it inline on the target). Defensive: backup_target_sync is a
        # newer table; on a cluster whose schema predates it the SELECT would
        # raise "no such table" and brick the whole snapshot build (load_cluster
        # is load-bearing, incl. failover paths). So tolerate its absence — log
        # LOUD (so the operator re-applies the schema) but keep building; the
        # only effect is targets report no mirrors until the table exists.
        try:
            sync_rows = list(client.query(
                "SELECT primary_id, secondary_id, position, delete_orphans "
                "FROM backup_target_sync ORDER BY primary_id, position",
                level=level,
            ))
        except Exception as e:
            if "no such table" in str(e).lower():
                log.warning("view_builder: backup_target_sync table missing — "
                            "re-apply bedrock_schema.sql; backup mirrors are "
                            "inert until then (%s)", e)
                sync_rows = []
            else:
                raise
        for row in sync_rows:
            tgt = out["backup_targets"].get(row["primary_id"])
            if tgt is None:
                continue   # primary repo was deleted; edge is orphaned, skip
            tgt["sync_to"].append(row["secondary_id"])
            if bool(row.get("delete_orphans")):
                tgt["delete_orphans"] = True

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
        "storage_endpoints": v.get("storage_endpoints", {}),
        "params":         v["params"],
        "vms":            v.get("vms", {}),
        "backup_targets": v.get("backup_targets", {}),
        "paths":          v.get("paths", {}),
        "operators":      v.get("operators", {}),
        "join_requests":  v.get("join_requests", {}),
        "obs_backends":   v.get("obs_backends", {"metrics": [], "logs": []}),
        # Field is named 'log_index' but holds the rqlite revision.
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
        "loopback_ip":  me.get("loopback_ip", ""),
        "mgmt_url":     f"https://{master_host}:8443" if master_host else "",
        "witness_host": master_host,
    }


# ─────────────────────────────────────────────────────────────────────
# No cluster.json projection here: consumers query rqlite directly via
# cluster_state.load_cluster() (level='none', works without quorum), and
# state.json is projected inline by mgmt/orchestrator.py:_apply_revision.
# ─────────────────────────────────────────────────────────────────────


def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically with a per-call unique tmp file, then
    os.replace. The unique tmp name (not a fixed one) is required
    because concurrent writers sharing a single tmp+rename race and
    can produce concatenated JSON — see lesson_orchestrator_atomic_write."""
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
# fold_into — rebuild the snapshot in place from rqlite
# ─────────────────────────────────────────────────────────────────────


def fold_into(out: dict, entries: list) -> dict:
    """Rebuild `out` in place from current rqlite state.

    The snapshot IS the database, so there is nothing to fold
    incrementally: `entries` is ignored, build_snapshot() is called,
    and `out` is replaced with the result. Callers that observe a
    revision change can call build_snapshot() directly instead.
    """
    log.debug("view_builder.fold_into is deprecated under rqlite; "
              "rebuilding from current state instead")
    new = build_snapshot()
    out.clear()
    out.update(new)
    return out
