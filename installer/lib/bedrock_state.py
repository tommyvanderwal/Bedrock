"""Cluster-state write helpers — rqlite edition.

Replaces installer/lib/log_entries.py. Where the old module produced
MessagePack payload bytes that were appended to bedrock-rust's hash-
chained log, this module performs the SQL upserts directly against
rqlite — and bumps the revision counter so watchers see the change.

API surface deliberately mirrors log_entries.py so the call-site
migration is mechanical: every place that used to do

    payload = le.node_register("sim-1", "192.168.2.18")
    with rust_ipc.Daemon() as d:
        d.append(payload)

becomes

    bedrock_state.node_register("sim-1", "192.168.2.18")

Each function takes the same arguments as the old encoder, opens a
short-lived rqlite client by default (or accepts one for batch
operations), runs the appropriate INSERT/UPDATE, and bumps
bedrock_meta.revision so subscribers wake.

Single-writer discipline: per D-20, only the elected mgmt-master
should be calling the mutation helpers in this module. The role
check lives in callers (mgmt/app.py, orchestrator.py) — this
module doesn't gate, trusting the caller to have verified role.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from . import rqlite_client

log = logging.getLogger("bedrock.state")


def _now() -> int:
    return int(time.time())


def _client(client: Optional[rqlite_client.RqliteClient]) -> tuple[rqlite_client.RqliteClient, bool]:
    """Return (client, owns_it). owns_it=True means caller didn't
    pass one and we should close it after."""
    if client is not None:
        return (client, False)
    return (rqlite_client.RqliteClient(), True)


def _bump_and_close(client: rqlite_client.RqliteClient, owns: bool) -> int:
    try:
        rev = rqlite_client.bump_revision(client)
        return rev
    finally:
        if owns:
            client.close()


# ─────────────────────────────────────────────────────────────────────
# Cluster identity
# ─────────────────────────────────────────────────────────────────────


def cluster_init(cluster_uuid: str, cluster_name: str,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Set the singleton cluster_info row. Called once at
    `bedrock init` on the fresh master. Idempotent (re-runs are
    no-ops if values unchanged)."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO cluster_info(id, cluster_uuid, cluster_name, updated_at) "
            "VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "cluster_uuid = excluded.cluster_uuid, "
            "cluster_name = excluded.cluster_name, "
            "updated_at = excluded.updated_at",
            params=[cluster_uuid, cluster_name, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def set_mgmt_master(node_name: str,
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Atomically (a) set cluster_info.mgmt_master and (b) update
    the per-node role columns so downstream reads see a consistent
    snapshot — old master's role drops back to 'compute', new
    master gets 'mgmt+compute'."""
    c, owns = _client(client)
    try:
        ts = _now()
        c.execute([
            ["UPDATE cluster_info SET mgmt_master = ?, updated_at = ? "
             "WHERE id = 1", node_name, ts],
            # Old mgmt+compute nodes (other than the new master) → compute
            ["UPDATE nodes SET role = 'compute', updated_at = ? "
             "WHERE role = 'mgmt+compute' AND node_name <> ?",
             ts, node_name],
            # Mark new master as mgmt+compute
            ["UPDATE nodes SET role = 'mgmt+compute', updated_at = ? "
             "WHERE node_name = ?", ts, node_name],
        ])
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Membership
# ─────────────────────────────────────────────────────────────────────


def node_register(node_name: str, host: str,
                  role: str = "compute", pubkey: str = "",
                  bedrock_pubkey: str = "",
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a node. Keeps the existing row's loopback_ip across
    re-registers (loopback is allocated separately by mgmt at join
    approval; this helper is for keeping host/role/pubkey current)."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO nodes(node_name, host, role, pubkey, "
            "bedrock_pubkey, loopback_ip, maintenance, updated_at) "
            "VALUES(?, ?, ?, ?, ?, '', 0, ?) "
            "ON CONFLICT(node_name) DO UPDATE SET "
            "host = excluded.host, role = excluded.role, "
            "pubkey = excluded.pubkey, "
            "bedrock_pubkey = excluded.bedrock_pubkey, "
            "updated_at = excluded.updated_at",
            params=[node_name, host, role, pubkey,
                    bedrock_pubkey, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def node_unregister(node_name: str, reason: str = "",
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Drop a node from membership. Also drops its tier-peer
    membership and DRBD node-id entries to keep the snapshot
    consistent (matches the old fold-side behaviour)."""
    c, owns = _client(client)
    try:
        c.execute([
            # Remove the node row itself
            ["DELETE FROM nodes WHERE node_name = ?", node_name],
            # Drop DRBD node-id assignments referencing it
            ["DELETE FROM tier_drbd_node_ids WHERE node_name = ?", node_name],
            # Tiers store peers as a JSON array column — we'd need
            # a JSON-aware update. SQLite's json_remove() is available
            # in modern builds (rqlite ships with one). Filter the
            # peer out if present, leave others.
            ["UPDATE tiers SET peers = "
             "(SELECT json_group_array(value) FROM "
             "json_each(peers) WHERE value <> ?), "
             "updated_at = ? "
             "WHERE EXISTS(SELECT 1 FROM json_each(peers) WHERE value = ?)",
             node_name, _now(), node_name],
        ])
        log.info("bedrock_state: node_unregister %s (reason=%r)", node_name, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def node_loopback(node_name: str, loopback_ip: str,
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Set the cluster-identity loopback /32 for a node. Called once
    at register-time; the value never changes for the life of the
    node's membership."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE nodes SET loopback_ip = ?, updated_at = ? "
            "WHERE node_name = ?",
            params=[loopback_ip, _now(), node_name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def node_maintenance(node_name: str, on: bool,
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE nodes SET maintenance = ?, updated_at = ? "
            "WHERE node_name = ?",
            params=[1 if on else 0, _now(), node_name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Storage tiers
# ─────────────────────────────────────────────────────────────────────


def tier_state(tier: str, mode: str, master: str | None = None,
               peers: list[str] | None = None,
               backend_path: str | None = None,
               client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a tier row. version is auto-incremented on every
    update so consumers can use it as an optimistic-concurrency
    token if needed."""
    c, owns = _client(client)
    try:
        peers_json = json.dumps(peers or [])
        c.execute(
            "INSERT INTO tiers(tier_name, mode, master, peers, "
            "backend_path, version, updated_at) "
            "VALUES(?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(tier_name) DO UPDATE SET "
            "mode = excluded.mode, master = excluded.master, "
            "peers = excluded.peers, backend_path = excluded.backend_path, "
            "version = version + 1, updated_at = excluded.updated_at",
            params=[tier, mode, master, peers_json,
                    backend_path, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def drbd_node_id_assigned(tier: str, node_name: str, node_id: int,
                          client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Per L3: node-ids are permanent for a resource. Upsert here so
    a re-register doesn't shift the assignment (the ON CONFLICT
    branch updates node_id to the same value — no-op semantically,
    refreshes updated_at)."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO tier_drbd_node_ids(tier_name, node_name, "
            "node_id, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(tier_name, node_name) DO UPDATE SET "
            "node_id = excluded.node_id, updated_at = excluded.updated_at",
            params=[tier, node_name, node_id, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def drbd_node_id_freed(tier: str, node_name: str, node_id: int,
                       reason: str = "",
                       client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "DELETE FROM tier_drbd_node_ids WHERE tier_name = ? "
            "AND node_name = ?",
            params=[tier, node_name],
        )
        log.info("bedrock_state: drbd_node_id_freed tier=%s node=%s id=%d reason=%r",
                 tier, node_name, node_id, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Witnesses
# ─────────────────────────────────────────────────────────────────────


def witness_register(witness_id: str, addr: str,
                     witness_pubkey_hex: str,
                     encrypted_witness_key_hex: str,
                     backend: str = "echo",
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a witness row. D-17 adds `backend` so the operator UI
    (post-v1.0 plumbing) can show which kind. Default 'echo' for
    today's UDP/12321 path."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO witnesses(witness_id, addr, witness_pubkey, "
            "encrypted_witness_key, backend, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(witness_id) DO UPDATE SET "
            "addr = excluded.addr, "
            "witness_pubkey = excluded.witness_pubkey, "
            "encrypted_witness_key = excluded.encrypted_witness_key, "
            "backend = excluded.backend, "
            "updated_at = excluded.updated_at",
            params=[witness_id, addr, witness_pubkey_hex,
                    encrypted_witness_key_hex, backend, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def witness_unregister(witness_id: str, reason: str = "",
                       client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "DELETE FROM witnesses WHERE witness_id = ?",
            params=[witness_id],
        )
        log.info("bedrock_state: witness_unregister %s reason=%r",
                 witness_id, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Params
# ─────────────────────────────────────────────────────────────────────


def param_change(key: str, value: Any,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Set a cluster-wide parameter. value can be any JSON-encodable
    type (string, int, float, bool, list, dict); stored as JSON."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO params(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            params=[key, json.dumps(value), _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Operators (dashboard auth)
# ─────────────────────────────────────────────────────────────────────


def operator_set(username: str, salt: str, password_hash: str,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO operators(username, salt, password_hash, updated_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET "
            "salt = excluded.salt, password_hash = excluded.password_hash, "
            "updated_at = excluded.updated_at",
            params=[username, salt, password_hash, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def operator_remove(username: str,
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "DELETE FROM operators WHERE username = ?",
            params=[username],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Join handshake
# ─────────────────────────────────────────────────────────────────────


def join_request(request_id: str, node_name: str, host: str,
                 bedrock_pubkey: str, x25519_eph_pubkey: str,
                 fingerprint: str,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO join_requests(request_id, node_name, host, "
            "bedrock_pubkey, x25519_eph_pubkey, fingerprint, "
            "state, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, 'pending', ?) "
            "ON CONFLICT(request_id) DO NOTHING",
            params=[request_id, node_name, host, bedrock_pubkey,
                    x25519_eph_pubkey, fingerprint, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def join_resolved(request_id: str, decision: str,
                  master_eph_pubkey: str = "",
                  ciphertext: str = "", nonce: str = "",
                  reason: str = "",
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """decision is 'approved' or 'rejected'."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE join_requests SET state = ?, "
            "master_eph_pubkey = ?, ciphertext = ?, nonce = ?, "
            "reason = ?, resolved_at = ? "
            "WHERE request_id = ?",
            params=[decision, master_eph_pubkey, ciphertext, nonce,
                    reason, _now(), request_id],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Observability backend assignments
# ─────────────────────────────────────────────────────────────────────


def obs_backends_set(metrics: list[str], logs: list[str],
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Replace the entire obs_backends assignment. Two stacks
    ('metrics' and 'logs'), up to 2 designated backend nodes per
    stack (the dual-write pattern)."""
    c, owns = _client(client)
    try:
        statements: list[list[Any]] = [["DELETE FROM obs_backends"]]
        for i, n in enumerate(metrics or []):
            statements.append([
                "INSERT INTO obs_backends(stack, node_name, position, updated_at) "
                "VALUES('metrics', ?, ?, ?)",
                n, i, _now(),
            ])
        for i, n in enumerate(logs or []):
            statements.append([
                "INSERT INTO obs_backends(stack, node_name, position, updated_at) "
                "VALUES('logs', ?, ?, ?)",
                n, i, _now(),
            ])
        c.execute(statements)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Mesh path table — bedrock-net's LINK_UP/DOWN/QUALITY equivalent
# ─────────────────────────────────────────────────────────────────────


def _path_key(node_a: str, nic_a: str, node_b: str, nic_b: str) -> str:
    """Canonical-order key — same algorithm as today's view_builder
    _path_key so the same physical path is identified the same way
    regardless of which side observed it first."""
    a = (node_a, nic_a)
    b = (node_b, nic_b)
    if a > b:
        a, b = b, a
    return f"{a[0]}|{a[1]}|{b[0]}|{b[1]}"


def link_up(node_a: str, nic_a: str, node_b: str, nic_b: str,
            link_addr_a: str = "", link_addr_b: str = "",
            speed_mbps: int = 0, rtt_us: int = 0,
            observed_at: float = 0.0,
            client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """A path between (node_a, nic_a) and (node_b, nic_b) has been
    continuously reachable past the up-hysteresis window."""
    # Canonicalise (a, b) so the row written matches the order
    # other observers would write.
    if (node_a, nic_a) > (node_b, nic_b):
        node_a, nic_a, link_addr_a, node_b, nic_b, link_addr_b = \
            node_b, nic_b, link_addr_b, node_a, nic_a, link_addr_a
    key = _path_key(node_a, nic_a, node_b, nic_b)
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO paths(path_key, node_a, nic_a, link_addr_a, "
            "node_b, nic_b, link_addr_b, speed_mbps, rtt_us, "
            "observed_at, up_since, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path_key) DO UPDATE SET "
            "link_addr_a = excluded.link_addr_a, "
            "link_addr_b = excluded.link_addr_b, "
            "speed_mbps = excluded.speed_mbps, "
            "rtt_us = excluded.rtt_us, "
            "observed_at = excluded.observed_at, "
            "updated_at = excluded.updated_at",
            params=[key, node_a, nic_a, link_addr_a,
                    node_b, nic_b, link_addr_b,
                    int(speed_mbps), int(rtt_us),
                    float(observed_at), float(observed_at), _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def link_down(node_a: str, nic_a: str, node_b: str, nic_b: str,
              reason: str = "", observed_at: float = 0.0,
              client: Optional[rqlite_client.RqliteClient] = None) -> int:
    if (node_a, nic_a) > (node_b, nic_b):
        node_a, nic_a, node_b, nic_b = node_b, nic_b, node_a, nic_a
    key = _path_key(node_a, nic_a, node_b, nic_b)
    c, owns = _client(client)
    try:
        c.execute("DELETE FROM paths WHERE path_key = ?", params=[key])
        log.info("bedrock_state: link_down %s reason=%r", key, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def link_quality(node_a: str, nic_a: str, node_b: str, nic_b: str,
                 link_addr_a: str = "", link_addr_b: str = "",
                 speed_mbps: int = 0, rtt_us: int = 0,
                 observed_at: float = 0.0,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Updated speed/RTT for an existing path. Only updates if the
    path is already present (matches view_builder's old fold
    behaviour: LINK_QUALITY doesn't resurrect a torn-down path)."""
    if (node_a, nic_a) > (node_b, nic_b):
        node_a, nic_a, link_addr_a, node_b, nic_b, link_addr_b = \
            node_b, nic_b, link_addr_b, node_a, nic_a, link_addr_a
    key = _path_key(node_a, nic_a, node_b, nic_b)
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE paths SET link_addr_a = ?, link_addr_b = ?, "
            "speed_mbps = ?, rtt_us = ?, observed_at = ?, "
            "updated_at = ? "
            "WHERE path_key = ?",
            params=[link_addr_a, link_addr_b,
                    int(speed_mbps), int(rtt_us),
                    float(observed_at), _now(), key],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# VMs — declared state and lifecycle transitions
# ─────────────────────────────────────────────────────────────────────


def vm_create_intent(name: str, vm_type: str, host: str, ram_mb: int,
                     disk_gb: int, requested_by: str = "",
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Record a pre-create VM intent. State='creating'; a subsequent
    vm_created() or vm_create_failed() resolves the outcome."""
    c, owns = _client(client)
    try:
        # Bump first to get the new revision, then write the row
        # carrying that revision as `intent_index`.
        rev = rqlite_client.bump_revision(c)
        c.execute(
            "INSERT INTO vms(vm_name, vm_type, host, ram_mb, disk_gb, "
            "state, intent_index, updated_at) "
            "VALUES(?, ?, ?, ?, ?, 'creating', ?, ?) "
            "ON CONFLICT(vm_name) DO UPDATE SET "
            "vm_type = excluded.vm_type, host = excluded.host, "
            "ram_mb = excluded.ram_mb, disk_gb = excluded.disk_gb, "
            "state = 'creating', intent_index = excluded.intent_index, "
            "updated_at = excluded.updated_at",
            params=[name, vm_type, host, int(ram_mb), int(disk_gb),
                    rev, _now()],
        )
        return rev
    finally:
        if owns:
            c.close()


def vm_created(name: str, vm_type: str, host: str, ram_mb: int,
               disk_gb: int,
               client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO vms(vm_name, vm_type, host, ram_mb, disk_gb, "
            "state, updated_at) VALUES(?, ?, ?, ?, ?, 'created', ?) "
            "ON CONFLICT(vm_name) DO UPDATE SET "
            "vm_type = excluded.vm_type, host = excluded.host, "
            "ram_mb = excluded.ram_mb, disk_gb = excluded.disk_gb, "
            "state = 'created', fail_reason = NULL, "
            "updated_at = excluded.updated_at",
            params=[name, vm_type, host, int(ram_mb), int(disk_gb), _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_create_failed(name: str, reason: str,
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET state = 'create_failed', fail_reason = ?, "
            "updated_at = ? WHERE vm_name = ?",
            params=[reason, _now(), name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_destroyed(name: str, reason: str = "",
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute("DELETE FROM vms WHERE vm_name = ?", params=[name])
        log.info("bedrock_state: vm_destroyed %s reason=%r", name, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_migrated(name: str, src_host: str, dst_host: str,
                client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET host = ?, updated_at = ? WHERE vm_name = ?",
            params=[dst_host, _now(), name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_state_change(name: str, host: str, state: str,
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """state: 'running', 'shut off', 'paused', etc. — verbatim
    libvirt label."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET state = ?, host = COALESCE(?, host), "
            "updated_at = ? WHERE vm_name = ?",
            params=[state, host or None, _now(), name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


# ─────────────────────────────────────────────────────────────────────
# Backups — targets + history + schedules
# ─────────────────────────────────────────────────────────────────────


def backup_target_set(target_id: str, kind: str, *,
                      s3_endpoint: str = "", s3_bucket: str = "",
                      s3_region: str = "",
                      s3_disable_tls: bool = False,
                      s3_disable_tls_verification: bool = False,
                      filesystem_path: str = "",
                      override_source_prefix: str = "",
                      cache_directory: str = "", reason: str = "",
                      client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO backup_targets(target_id, kind, "
            "s3_endpoint, s3_bucket, s3_region, "
            "s3_disable_tls, s3_disable_tls_verification, "
            "filesystem_path, override_source_prefix, cache_directory, "
            "updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_id) DO UPDATE SET "
            "kind = excluded.kind, "
            "s3_endpoint = excluded.s3_endpoint, "
            "s3_bucket = excluded.s3_bucket, "
            "s3_region = excluded.s3_region, "
            "s3_disable_tls = excluded.s3_disable_tls, "
            "s3_disable_tls_verification = excluded.s3_disable_tls_verification, "
            "filesystem_path = excluded.filesystem_path, "
            "override_source_prefix = excluded.override_source_prefix, "
            "cache_directory = excluded.cache_directory, "
            "updated_at = excluded.updated_at",
            params=[target_id, kind, s3_endpoint, s3_bucket, s3_region,
                    1 if s3_disable_tls else 0,
                    1 if s3_disable_tls_verification else 0,
                    filesystem_path, override_source_prefix,
                    cache_directory, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def backup_target_removed(target_id: str, reason: str = "",
                          client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute("DELETE FROM backup_targets WHERE target_id = ?",
                  params=[target_id])
        log.info("bedrock_state: backup_target_removed %s reason=%r",
                 target_id, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def backup_done(vm: str, target_id: str, *,
                disks: list | None = None,
                source_node: str = "",
                duration_s: float = 0.0,
                label: str = "",
                fs_freeze_used: bool = False,
                kopia_snapshot_id: str | None = None,
                bytes_added: int = 0,
                client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Same multi-disk schema as the old encoder. Persists one row
    in vm_backups with the disks JSON column carrying the full
    array."""
    if disks is None:
        if kopia_snapshot_id is None:
            raise ValueError("backup_done: pass disks=[…] or legacy "
                             "kopia_snapshot_id=…")
        disks = [{
            "target_dev": "disk0",
            "lv_path": "",
            "kopia_snapshot_id": kopia_snapshot_id,
            "bytes_added": int(bytes_added),
        }]
    norm_disks = []
    total = 0
    for d in disks:
        nd = {
            "target_dev": str(d.get("target_dev", "")),
            "lv_path":    str(d.get("lv_path", "")),
            "kopia_snapshot_id": str(d.get("kopia_snapshot_id", "")),
            "bytes_added": int(d.get("bytes_added", 0)),
        }
        norm_disks.append(nd)
        total += nd["bytes_added"]
    primary_kid = norm_disks[0]["kopia_snapshot_id"] if norm_disks else ""

    c, owns = _client(client)
    try:
        rev = rqlite_client.bump_revision(c)
        c.execute(
            "INSERT INTO vm_backups(vm_name, target_id, source_node, "
            "disks, primary_kopia_id, bytes_added, duration_s, "
            "label, fs_freeze_used, ts_index, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            params=[vm, target_id, source_node, json.dumps(norm_disks),
                    primary_kid, total, float(duration_s),
                    label, 1 if fs_freeze_used else 0,
                    rev, _now()],
        )
        return rev
    finally:
        if owns:
            c.close()


def backup_failed(vm: str, target_id: str, reason: str, *,
                  source_node: str = "", label: str = "",
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        err_json = json.dumps({
            "ts_index": rqlite_client.bump_revision(c),
            "target_id": target_id,
            "reason": reason,
        })
        c.execute(
            "UPDATE vms SET last_backup_error = ?, updated_at = ? "
            "WHERE vm_name = ?",
            params=[err_json, _now(), vm],
        )
        log.info("bedrock_state: backup_failed vm=%s target=%s reason=%r",
                 vm, target_id, reason)
        return rqlite_client.bump_revision(c)
    finally:
        if owns:
            c.close()


def backup_deleted(vm: str, target_id: str, kopia_snapshot_id: str,
                   reason: str = "",
                   client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "DELETE FROM vm_backups WHERE vm_name = ? AND "
            "primary_kopia_id = ? AND target_id = ?",
            params=[vm, kopia_snapshot_id, target_id],
        )
        log.info("bedrock_state: backup_deleted vm=%s kid=%s reason=%r",
                 vm, kopia_snapshot_id, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def restore_done(vm: str, target_id: str, kopia_snapshot_id: str, *,
                 dest_node: str = "", duration_s: float = 0.0,
                 client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        rev = rqlite_client.bump_revision(c)
        info_json = json.dumps({
            "ts_index": rev,
            "kopia_snapshot_id": kopia_snapshot_id,
            "target_id": target_id,
            "dest_node": dest_node,
        })
        c.execute(
            "UPDATE vms SET last_restore = ?, updated_at = ? "
            "WHERE vm_name = ?",
            params=[info_json, _now(), vm],
        )
        return rev
    finally:
        if owns:
            c.close()


def restore_failed(vm: str, target_id: str, kopia_snapshot_id: str,
                   reason: str, *, dest_node: str = "",
                   client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        rev = rqlite_client.bump_revision(c)
        info_json = json.dumps({
            "ts_index": rev,
            "kopia_snapshot_id": kopia_snapshot_id,
            "target_id": target_id,
            "reason": reason,
        })
        c.execute(
            "UPDATE vms SET last_restore_err = ?, updated_at = ? "
            "WHERE vm_name = ?",
            params=[info_json, _now(), vm],
        )
        return rev
    finally:
        if owns:
            c.close()


def backup_schedule_set(vm: str, target_id: str, cron_expr: str, *,
                        label_prefix: str = "auto",
                        retention_count: int = 0,
                        reason: str = "",
                        client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        rev = rqlite_client.bump_revision(c)
        sched_json = json.dumps({
            "target_id": target_id,
            "cron_expr": cron_expr,
            "label_prefix": label_prefix,
            "retention_count": int(retention_count),
            "set_at_index": rev,
        })
        c.execute(
            "UPDATE vms SET backup_schedule = ?, updated_at = ? "
            "WHERE vm_name = ?",
            params=[sched_json, _now(), vm],
        )
        return rev
    finally:
        if owns:
            c.close()


def backup_schedule_removed(vm: str, reason: str = "",
                            client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET backup_schedule = NULL, updated_at = ? "
            "WHERE vm_name = ?",
            params=[_now(), vm],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise
