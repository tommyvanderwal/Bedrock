"""Cluster-state write helpers against rqlite.

Each function performs the SQL upserts/deletes for one kind of
cluster-state mutation and bumps bedrock_meta.revision so subscribers
wake. Functions open a short-lived rqlite client by default, or accept
one for batch operations.

Usage:

    bedrock_state.node_register("sim-1", "192.168.2.18")

Single-writer discipline: only the elected mgmt-master calls the
mutation helpers here. The role check lives in callers (mgmt/app.py,
orchestrator.py) — this module doesn't gate, trusting the caller to
have verified role.
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


def set_cluster_name(cluster_name: str,
                     client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Update the singleton ``cluster_info.cluster_name`` — the
    display tag every node projects into ``state.json`` and the mDNS
    TXT record (runtime consumers read rqlite via
    ``cluster_state.load_cluster()``). The ``cluster_uuid`` is
    immutable; only the name changes.

    Bumps ``bedrock_meta.revision`` so every node's
    ``rqlite_subscriber`` re-projects within ~2 s and the mDNS
    responder picks up the new TXT field on its next refresh tick
    (≤60 s).
    """
    c, owns = _client(client)
    try:
        c.execute([
            ["UPDATE cluster_info SET cluster_name = ?, updated_at = ? "
             "WHERE id = 1", cluster_name, _now()],
        ])
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
                  state: str = "active",
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a node. Keeps the existing row's loopback_ip across
    re-registers (loopback is allocated separately by mgmt at join
    approval; this helper is for keeping host/role/pubkey current).

    `state` is the lifecycle gate the election denominator reads:
    a node registered as
    'joining' is excluded from the active-node count until it
    self-activates (node_set_active) at the end of its join saga, so a
    mid-join node never tips the master into NoQuorum. cluster_init
    self-registers the master 'active' (the default). On re-register
    the existing state is PRESERVED (not reset to the default) — a
    re-register of an already-active node must not demote it to
    'joining'."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO nodes(node_name, host, role, pubkey, "
            "bedrock_pubkey, loopback_ip, maintenance, state, updated_at) "
            "VALUES(?, ?, ?, ?, ?, '', 0, ?, ?) "
            "ON CONFLICT(node_name) DO UPDATE SET "
            "host = excluded.host, role = excluded.role, "
            "pubkey = excluded.pubkey, "
            "bedrock_pubkey = excluded.bedrock_pubkey, "
            "updated_at = excluded.updated_at",
            params=[node_name, host, role, pubkey,
                    bedrock_pubkey, state, _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def node_set_active(node_name: str,
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Flip a node's lifecycle state to 'active'. Called by the join
    saga's final step once rqlited has joined Raft + bedrock-d is up,
    so the node now counts toward the election denominator.
    Idempotent — UPDATE to 'active' on an already-active node is a
    no-op write."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE nodes SET state = 'active', updated_at = ? "
            "WHERE node_name = ?",
            params=[_now(), node_name],
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
    consistent."""
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
    """DRBD node-ids are permanent for a resource. Upsert here so
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
    """Upsert a witness row. `backend` records which kind of witness
    this is so the operator UI can show it. Default 'echo' for the
    BedRock Echo UDP/12321 path."""
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
                  node_cert_pem: str = "",
                  ca_cert_pem: str = "",
                  client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """decision is 'approved' or 'rejected'. node_cert_pem + ca_cert_pem
    are the joiner's freshly-signed TLS cert + the cluster CA cert,
    returned to the joiner via /api/join/status so it can configure
    rqlited mTLS immediately."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE join_requests SET state = ?, "
            "master_eph_pubkey = ?, ciphertext = ?, nonce = ?, "
            "reason = ?, node_cert_pem = ?, ca_cert_pem = ?, "
            "resolved_at = ? "
            "WHERE request_id = ?",
            params=[decision, master_eph_pubkey, ciphertext, nonce,
                    reason, node_cert_pem, ca_cert_pem,
                    _now(), request_id],
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
# Mesh path table — per-link up/down/quality from bedrock-net
# ─────────────────────────────────────────────────────────────────────


def _path_key(node_a: str, nic_a: str, node_b: str, nic_b: str) -> str:
    """Canonical-order key, so the same physical path is identified
    the same way regardless of which side observed it first."""
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
    path is already present: a quality update never resurrects a
    torn-down path."""
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


def vm_set_failover_order(name: str, order: list[str],
                          client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Record this VM's predetermined failover sequence as a JSON
    array of node_names. Order is meaningful: index 0 is the
    primary, 1 is the secondary, 2 is the tertiary (vipet only).
    Cattle VMs pass `[]`. Used by the failover orchestrator on a
    surviving node to decide whether it's next in line after a
    dead primary. Written at VM creation by the create saga; only
    changed via an explicit operator-issued saga afterwards."""
    import json as _json
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET failover_order = ?, updated_at = ? "
            "WHERE vm_name = ?",
            params=[_json.dumps(list(order)), _now(), name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_set_libvirt_xml(name: str, xml: str,
                       client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Store the VM's libvirt domain XML in cluster state so any node — even
    one that joined AFTER the VM was created — can re-`virsh define` it before
    taking it over on failover. Written by the create saga; the failover path
    reads it via vm_get_libvirt_xml. Not projected into the snapshot."""
    c, owns = _client(client)
    try:
        c.execute("UPDATE vms SET libvirt_xml = ?, updated_at = ? "
                  "WHERE vm_name = ?",
                  params=[xml or "", _now(), name])
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def vm_get_libvirt_xml(name: str,
                       client: Optional[rqlite_client.RqliteClient] = None,
                       level: str = "strong") -> str:
    """Read the VM's stored libvirt domain XML (for failover re-define).
    Returns '' if unknown. Defaults to a STRONG read — a takeover define is a
    decision that must use authoritative state, not a possibly-stale local
    replica."""
    c, owns = _client(client)
    try:
        row = c.query_one("SELECT libvirt_xml FROM vms WHERE vm_name = ?",
                          params=[name], level=level)
        return (row or {}).get("libvirt_xml") or ""
    finally:
        if owns:
            c.close()


def vm_set_priority(name: str, priority: str,
                    client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Record this VM's HA-importance ('low'|'normal'|'high'). The
    self-heal repair loop reads this to order replica restoration
    after a permanent host loss (high before normal before low).
    Set at create time and whenever the operator changes priority."""
    if priority not in ("low", "normal", "high"):
        priority = "normal"
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE vms SET priority = ?, updated_at = ? WHERE vm_name = ?",
            params=[priority, _now(), name],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def drbd_resource_uuid_set(resource_name: str, uuid: str,
                           client: Optional[rqlite_client.RqliteClient] = None
                           ) -> int:
    """Record the post-promote DRBD current-uuid for a resource.
    Called by a node that just ran `drbdadm primary` on this
    resource, BEFORE starting any service (VM, filer) that uses
    the underlying disk. The write goes through Raft normally
    (single-statement transaction) so quorum confirms before the
    function returns; callers that need strict linearizability
    can chain a level='strong' SELECT afterwards. Updates
    `uuid_ts_set` to now()."""
    c, owns = _client(client)
    try:
        c.execute(
            "UPDATE drbd_resources SET current_uuid = ?, "
            "uuid_ts_set = ?, updated_at = ? WHERE name = ?",
            params=[uuid, _now(), _now(), resource_name],
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


def _seal_secret(plaintext: str) -> str:
    """AEAD-wrap a secret with the cluster key, return hex. '' → ''. So a
    plaintext password/secret-key never lands in an rqlite snapshot/backup;
    only a node holding the cluster key (and the mTLS cert to even reach
    rqlite) can recover it."""
    if not plaintext:
        return ""
    from . import witness as _w
    return _w._aead_seal(_w.load_cluster_key(), plaintext.encode()).hex()


def unseal_secret(blob_hex: str) -> str:
    """Inverse of _seal_secret. Returns '' on empty input or on auth failure
    (a caller that needs the secret must check for '' and fail loud)."""
    if not blob_hex:
        return ""
    from . import witness as _w
    pt = _w._aead_open(_w.load_cluster_key(), bytes.fromhex(blob_hex))
    return pt.decode() if pt is not None else ""


def storage_endpoint_set(endpoint_id: str, endpoint_type: str, *,
                         label: str = "",
                         s3_endpoint: str = "", s3_bucket: str = "",
                         s3_region: str = "", s3_prefix: str = "",
                         s3_disable_tls: bool = False,
                         s3_disable_tls_verification: bool = False,
                         s3_access_key: str = "", s3_secret_key: str = "",
                         fs_server: str = "", fs_share: str = "",
                         fs_options: str = "", fs_username: str = "",
                         fs_password: str = "", reason: str = "",
                         client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a consolidated storage endpoint (endpoint_type in
    's3'|'smb'|'nfs'). Secrets (s3_secret_key, fs_password) are AEAD-wrapped
    with the cluster key before storage. This setter writes exactly what it is
    given — the caller (mgmt API) is responsible for reading-existing-and-
    reusing a secret the operator did not re-type, so a label edit can't wipe a
    password."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO storage_endpoints(endpoint_id, type, label, "
            "s3_endpoint, s3_bucket, s3_region, s3_prefix, "
            "s3_disable_tls, s3_disable_tls_verification, "
            "s3_access_key, s3_secret_key_enc, "
            "fs_server, fs_share, fs_options, fs_username, fs_password_enc, "
            "updated_at) VALUES(?,?,?, ?,?,?,?, ?,?, ?,?, ?,?,?,?,?, ?) "
            "ON CONFLICT(endpoint_id) DO UPDATE SET "
            "type=excluded.type, label=excluded.label, "
            "s3_endpoint=excluded.s3_endpoint, s3_bucket=excluded.s3_bucket, "
            "s3_region=excluded.s3_region, s3_prefix=excluded.s3_prefix, "
            "s3_disable_tls=excluded.s3_disable_tls, "
            "s3_disable_tls_verification=excluded.s3_disable_tls_verification, "
            "s3_access_key=excluded.s3_access_key, "
            "s3_secret_key_enc=excluded.s3_secret_key_enc, "
            "fs_server=excluded.fs_server, fs_share=excluded.fs_share, "
            "fs_options=excluded.fs_options, fs_username=excluded.fs_username, "
            "fs_password_enc=excluded.fs_password_enc, "
            "updated_at=excluded.updated_at",
            params=[endpoint_id, endpoint_type, label,
                    s3_endpoint, s3_bucket, s3_region, s3_prefix,
                    1 if s3_disable_tls else 0,
                    1 if s3_disable_tls_verification else 0,
                    s3_access_key, _seal_secret(s3_secret_key),
                    fs_server, fs_share, fs_options, fs_username,
                    _seal_secret(fs_password), _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def storage_endpoint_removed(endpoint_id: str,
                             client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Delete a storage endpoint. (Caller guards against deleting one still
    referenced by a backup_target or witness.)"""
    c, owns = _client(client)
    try:
        c.execute("DELETE FROM storage_endpoints WHERE endpoint_id = ?",
                  params=[endpoint_id])
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def storage_endpoint_secret(endpoint_id: str, which: str,
                            client: Optional[rqlite_client.RqliteClient] = None) -> str:
    """Read + unseal one secret of a storage endpoint, on demand (the cluster
    view never carries the sealed blob). ``which`` is 's3_secret_key' or
    'fs_password'. Returns '' if the endpoint/secret is absent or auth fails —
    a caller that NEEDS the secret must check for '' and fail loud."""
    col = {"s3_secret_key": "s3_secret_key_enc",
           "fs_password": "fs_password_enc"}[which]
    c, owns = _client(client)
    try:
        rows = list(c.query(
            f"SELECT {col} AS enc FROM storage_endpoints WHERE endpoint_id = ?",
            params=[endpoint_id]))
        return unseal_secret(rows[0]["enc"]) if rows else ""
    finally:
        if owns:
            c.close()


def backup_target_set(target_id: str, kind: str, *,
                      s3_endpoint: str = "", s3_bucket: str = "",
                      s3_region: str = "",
                      s3_disable_tls: bool = False,
                      s3_disable_tls_verification: bool = False,
                      filesystem_path: str = "",
                      override_source_prefix: str = "",
                      cache_directory: str = "", is_mirror: bool = False,
                      repo_password: str = "",
                      s3_access_key: str = "", s3_secret_key: str = "",
                      reason: str = "",
                      client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Upsert a backup target. ``repo_password`` is the per-repo kopia encryption
    password and ``s3_secret_key`` the S3 secret — both AEAD-sealed before storage
    (rqlite is the cluster-internal source of truth for every secret). Pass '' for
    any secret to LEAVE THE EXISTING VALUE UNCHANGED (so a bucket/region edit can't
    silently wipe creds); on a NEW target '' password means "use the published
    PUBLIC default". ``s3_access_key`` ('' also = keep) is an identifier, stored
    in the clear."""
    c, owns = _client(client)
    try:
        c.execute(
            "INSERT INTO backup_targets(target_id, kind, "
            "s3_endpoint, s3_bucket, s3_region, "
            "s3_disable_tls, s3_disable_tls_verification, "
            "filesystem_path, override_source_prefix, cache_directory, "
            "is_mirror, repo_password_enc, s3_access_key, s3_secret_key_enc, "
            "updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
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
            "is_mirror = excluded.is_mirror, "
            # CASE-preserve every secret/cred: an empty incoming value KEEPS the
            # stored one (a partial update / reactor re-apply never wipes creds);
            # a non-empty one replaces it.
            "repo_password_enc = CASE WHEN excluded.repo_password_enc = '' "
            "THEN backup_targets.repo_password_enc "
            "ELSE excluded.repo_password_enc END, "
            "s3_access_key = CASE WHEN excluded.s3_access_key = '' "
            "THEN backup_targets.s3_access_key "
            "ELSE excluded.s3_access_key END, "
            "s3_secret_key_enc = CASE WHEN excluded.s3_secret_key_enc = '' "
            "THEN backup_targets.s3_secret_key_enc "
            "ELSE excluded.s3_secret_key_enc END, "
            "updated_at = excluded.updated_at",
            params=[target_id, kind, s3_endpoint, s3_bucket, s3_region,
                    1 if s3_disable_tls else 0,
                    1 if s3_disable_tls_verification else 0,
                    filesystem_path, override_source_prefix,
                    cache_directory, 1 if is_mirror else 0,
                    _seal_secret(repo_password),
                    s3_access_key, _seal_secret(s3_secret_key), _now()],
        )
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def backup_target_repo_password(target_id: str,
                                client: Optional[rqlite_client.RqliteClient] = None) -> str:
    """The unsealed per-repo kopia password for a target, or '' if none is set
    (meaning: the caller should use the published PUBLIC default). Read on demand
    — the cluster view never carries the sealed blob, only has_repo_password."""
    c, owns = _client(client)
    try:
        rows = list(c.query(
            "SELECT repo_password_enc AS enc FROM backup_targets "
            "WHERE target_id = ?", params=[target_id]))
        return unseal_secret(rows[0]["enc"]) if rows else ""
    finally:
        if owns:
            c.close()


def backup_target_s3_creds(target_id: str,
                           client: Optional[rqlite_client.RqliteClient] = None) -> tuple:
    """(access_key, secret_key) for a target, secret unsealed on demand. Either
    may be '' if unset. rqlite is the source of truth; the per-node .env is a
    cache materialized from this."""
    c, owns = _client(client)
    try:
        rows = list(c.query(
            "SELECT s3_access_key AS ak, s3_secret_key_enc AS sk "
            "FROM backup_targets WHERE target_id = ?", params=[target_id]))
        if not rows:
            return ("", "")
        return (rows[0]["ak"] or "", unseal_secret(rows[0]["sk"]))
    finally:
        if owns:
            c.close()


def backup_target_removed(target_id: str, reason: str = "",
                          client: Optional[rqlite_client.RqliteClient] = None) -> int:
    c, owns = _client(client)
    try:
        # Drop the target AND any mirror edges that reference it (as a
        # primary OR as a secondary) in one txn — otherwise the
        # backup_target_sync table would keep dangling rows pointing at a
        # repo that no longer exists.
        c.execute([
            ["DELETE FROM backup_targets WHERE target_id = ?", target_id],
            ["DELETE FROM backup_target_sync "
             "WHERE primary_id = ? OR secondary_id = ?", target_id, target_id],
        ])
        log.info("bedrock_state: backup_target_removed %s reason=%r",
                 target_id, reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def backup_target_sync_set(primary_id: str, secondary_ids: list,
                           *, delete_orphans: bool = False, reason: str = "",
                           client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Replace the mirror set for one PRIMARY backup target. `secondary_ids`
    is the ordered list of backup_targets.target_id that <primary_id> mirrors
    to via `kopia repository sync-to` after each backup. An empty list clears
    all mirrors for the primary. Idempotent (DELETE-then-INSERT the set in one
    txn), same shape as obs_backends_set."""
    c, owns = _client(client)
    try:
        statements: list[list[Any]] = [
            ["DELETE FROM backup_target_sync WHERE primary_id = ?", primary_id],
        ]
        for i, sec in enumerate(secondary_ids or []):
            statements.append([
                "INSERT INTO backup_target_sync(primary_id, secondary_id, "
                "position, delete_orphans, updated_at) VALUES(?, ?, ?, ?, ?)",
                primary_id, sec, i, 1 if delete_orphans else 0, _now(),
            ])
        c.execute(statements)
        log.info("bedrock_state: backup_target_sync_set %s -> %r reason=%r",
                 primary_id, list(secondary_ids or []), reason)
        return _bump_and_close(c, owns)
    except Exception:
        if owns:
            c.close()
        raise


def backup_target_sync_removed(primary_id: str, secondary_id: Optional[str] = None,
                               *, reason: str = "",
                               client: Optional[rqlite_client.RqliteClient] = None) -> int:
    """Remove mirror edges. With secondary_id=None, remove ALL mirrors for the
    primary; otherwise remove just that one primary->secondary edge."""
    c, owns = _client(client)
    try:
        if secondary_id is None:
            c.execute("DELETE FROM backup_target_sync WHERE primary_id = ?",
                      params=[primary_id])
        else:
            c.execute("DELETE FROM backup_target_sync "
                      "WHERE primary_id = ? AND secondary_id = ?",
                      params=[primary_id, secondary_id])
        log.info("bedrock_state: backup_target_sync_removed %s/%s reason=%r",
                 primary_id, secondary_id, reason)
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
    """Persists one row in vm_backups, with the disks JSON column
    carrying the full multi-disk array."""
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
