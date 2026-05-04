"""Typed payload schema for entries in the bedrock-rust log.

Every payload is MessagePack with a `t` tag that names the entry type.
Materialised views (cluster.json, state.json) are derived by folding
these in order — this is the mechanism that replaces L28 / L30
workarounds with structural correctness.

This file defines:
  - constants for each entry type
  - constructor helpers that return MessagePack bytes
  - `decode(payload_bytes) -> dict`

Entry types (v0.1):
  - cluster_init               first non-bootstrap entry, sets cluster name + uuid
  - node_register              a node joined the cluster
  - node_unregister            a node left
  - mgmt_master                mgmt master changed
  - tier_state                 tier (scratch/bulk/critical) mode/peers/master changed
  - drbd_node_id_assigned      sticky DRBD node-id assignment (replaces L27 brittleness)
  - witness_register           a witness was added (witness_id + encrypted key)
  - witness_unregister         a witness was removed
  - param_change               cluster parameter (TTL, etc.) changed; drives leader-only-mode
"""

from __future__ import annotations

import msgpack


# ── entry types ──

# The bootstrap entry is the first entry in every log (index 1). Its
# `uuid` field is the cluster_uuid the cluster identifies itself by;
# the entry's hash chains every other entry in the cluster's history.
# Per design §4: a re-initialised cluster is distinguishable from a
# continued one because the bootstrap entry's hash includes the new
# uuid → the chain forks at index 1.
BOOTSTRAP             = "bootstrap"
CLUSTER_INIT          = "cluster_init"
NODE_REGISTER         = "node_register"
NODE_UNREGISTER       = "node_unregister"
MGMT_MASTER           = "mgmt_master"
TIER_STATE            = "tier_state"
DRBD_NODE_ID          = "drbd_node_id_assigned"
DRBD_NODE_ID_FREED    = "drbd_node_id_freed"
WITNESS_REGISTER      = "witness_register"
WITNESS_UNREGISTER    = "witness_unregister"
PARAM_CHANGE          = "param_change"

# Maintenance mode (planned downtime). When a node is in maintenance,
# its peers treat its silence as expected — surviving node keeps
# running without witness arbitration. Reverse on `off`.
NODE_MAINTENANCE      = "node_maintenance_set"

# VM task lifecycle (L47). Facts/events: append history, sequence
# matters. After a crash these let "what was running here when it
# went down?" be answered from the log alone.
VM_CREATE_INTENT      = "vm_create_intent"
VM_CREATED            = "vm_created"
VM_CREATE_FAILED      = "vm_create_failed"
VM_DESTROYED          = "vm_destroyed"
VM_MIGRATED           = "vm_migrated"
VM_STATE_CHANGE       = "vm_state_change"

# Backup target + per-VM backup history. Backup data lives in the
# Kopia repository (S3 / FS / etc.); the log records intent + outcome
# + which kopia snapshot id corresponds to which Bedrock backup, so
# the dashboard can show backup history per VM and the operator can
# initiate restores.
BACKUP_TARGET_SET     = "backup_target_set"      # idempotent: latest wins
BACKUP_TARGET_REMOVED = "backup_target_removed"
BACKUP_DONE           = "backup_done"
BACKUP_FAILED         = "backup_failed"
BACKUP_DELETED        = "backup_deleted"
RESTORE_DONE          = "restore_done"
RESTORE_FAILED        = "restore_failed"


def encode(t: str, **fields) -> bytes:
    """Encode a typed entry payload."""
    return msgpack.packb({"t": t, **fields}, use_bin_type=True)


def decode(payload: bytes) -> dict:
    """Decode a typed entry payload. Returns the original mapping.

    Bootstrap entries (`Hello World! <uuid>`) and any free-form opaque
    payloads (`bedrock-rust log append --text "..."`) are not MessagePack
    — return them with a synthetic `_free` tag so the fold can ignore
    them while still keeping the raw bytes inspectable.
    """
    if not payload:
        return {}
    try:
        obj = msgpack.unpackb(payload, raw=False)
    except (msgpack.exceptions.ExtraData,
            msgpack.exceptions.UnpackException,
            ValueError):
        return {"t": "_free", "raw": payload}
    if not isinstance(obj, dict) or "t" not in obj:
        return {"t": "_free", "raw": payload}
    return obj


# ── constructors ──

def bootstrap(uuid: str) -> bytes:
    return encode(BOOTSTRAP, uuid=uuid)


def cluster_init(name: str, uuid: str) -> bytes:
    return encode(CLUSTER_INIT, name=name, uuid=uuid)


def node_register(node_name: str, host: str, drbd_ip: str, role: str = "compute",
                  pubkey: str = "") -> bytes:
    return encode(NODE_REGISTER, node_name=node_name, host=host,
                  drbd_ip=drbd_ip, role=role, pubkey=pubkey)


def node_unregister(node_name: str, reason: str = "") -> bytes:
    """Mark a node as removed from the cluster. `reason` is operator-
    facing context (e.g. "leave", "decommission", "remove-peer drain")
    captured for the journal — fold drops the node from the snapshot
    regardless. Empty string is allowed for backward-compatible
    invocations."""
    return encode(NODE_UNREGISTER, node_name=node_name, reason=reason)


def mgmt_master(node_name: str) -> bytes:
    return encode(MGMT_MASTER, node_name=node_name)


def tier_state(tier: str, mode: str, master: str | None = None,
               peers: list[str] | None = None,
               backend_path: str | None = None,
               garage_endpoint: str | None = None) -> bytes:
    return encode(
        TIER_STATE, tier=tier, mode=mode,
        master=master, peers=peers or [],
        backend_path=backend_path,
        garage_endpoint=garage_endpoint,
    )


def drbd_node_id_assigned(tier: str, node_name: str, node_id: int) -> bytes:
    return encode(DRBD_NODE_ID, tier=tier, node_name=node_name, node_id=node_id)


def drbd_node_id_freed(tier: str, node_name: str, node_id: int,
                       reason: str = "") -> bytes:
    """Released node-id slot, written when a peer is removed (e.g.
    via remove-peer or node leave). Fold drops the assignment from the
    snapshot's drbd_node_ids map. `reason` is operator-facing."""
    return encode(DRBD_NODE_ID_FREED, tier=tier, node_name=node_name,
                  node_id=node_id, reason=reason)


def witness_register(witness_id: str, addr: str,
                     witness_pubkey_hex: str,
                     encrypted_witness_key_hex: str) -> bytes:
    return encode(WITNESS_REGISTER, witness_id=witness_id, addr=addr,
                  witness_pubkey=witness_pubkey_hex,
                  encrypted_witness_key=encrypted_witness_key_hex)


def witness_unregister(witness_id: str, reason: str = "") -> bytes:
    return encode(WITNESS_UNREGISTER, witness_id=witness_id, reason=reason)


def param_change(key: str, value) -> bytes:
    return encode(PARAM_CHANGE, key=key, value=value)


def vm_create_intent(name: str, vm_type: str, host: str, ram_mb: int,
                     disk_gb: int, requested_by: str = "") -> bytes:
    return encode(VM_CREATE_INTENT, name=name, vm_type=vm_type, host=host,
                  ram_mb=ram_mb, disk_gb=disk_gb, requested_by=requested_by)


def vm_create_failed(name: str, reason: str) -> bytes:
    return encode(VM_CREATE_FAILED, name=name, reason=reason)


def vm_created(name: str, vm_type: str, host: str, ram_mb: int, disk_gb: int) -> bytes:
    return encode(VM_CREATED, name=name, vm_type=vm_type, host=host,
                  ram_mb=ram_mb, disk_gb=disk_gb)


def vm_destroyed(name: str, reason: str = "") -> bytes:
    return encode(VM_DESTROYED, name=name, reason=reason)


def vm_migrated(name: str, src_host: str, dst_host: str) -> bytes:
    return encode(VM_MIGRATED, name=name, src_host=src_host, dst_host=dst_host)


def vm_state_change(name: str, host: str, state: str) -> bytes:
    """state: 'running', 'shut off', 'paused', etc. — verbatim libvirt label."""
    return encode(VM_STATE_CHANGE, name=name, host=host, state=state)


def node_maintenance(node_name: str, on: bool) -> bytes:
    return encode(NODE_MAINTENANCE, node_name=node_name, on=bool(on))


# ── backup ─────────────────────────────────────────────────────────────

def backup_target_set(target_id: str, kind: str, *,
                      s3_endpoint: str = "",
                      s3_bucket: str = "",
                      s3_region: str = "",
                      s3_disable_tls: bool = False,
                      s3_disable_tls_verification: bool = False,
                      filesystem_path: str = "",
                      override_source_prefix: str = "",
                      cache_directory: str = "",
                      reason: str = "") -> bytes:
    """Operator-set backup target. `target_id` is operator-chosen
    (e.g. "main"). `kind` is "kopia-s3" or "kopia-fs" for v1.

    Credentials (S3 access/secret keys, encryption password) live in
    /etc/bedrock/backup-credentials/<target_id>.env on each node, NOT
    in the log. The log records connection metadata only — endpoint,
    bucket, region, paths, prefix — enough for the dashboard to
    show the target and for any node to know where to point kopia
    after the credentials file is in place.

    `s3_disable_tls` swaps to plain HTTP (insecure). `s3_disable_tls_
    verification` keeps HTTPS but skips cert validation — typically
    needed for self-hosted S3 (QNAP, MinIO with self-signed certs)
    on a private LAN. Both default to False; turning them on is an
    explicit choice the operator makes per target."""
    return encode(
        BACKUP_TARGET_SET,
        target_id=target_id, kind=kind,
        s3_endpoint=s3_endpoint, s3_bucket=s3_bucket, s3_region=s3_region,
        s3_disable_tls=bool(s3_disable_tls),
        s3_disable_tls_verification=bool(s3_disable_tls_verification),
        filesystem_path=filesystem_path,
        override_source_prefix=override_source_prefix,
        cache_directory=cache_directory,
        reason=reason,
    )


def backup_target_removed(target_id: str, reason: str = "") -> bytes:
    return encode(BACKUP_TARGET_REMOVED, target_id=target_id, reason=reason)


def backup_done(vm: str, target_id: str, kopia_snapshot_id: str, *,
                source_node: str = "", bytes_added: int = 0,
                duration_s: float = 0.0, label: str = "") -> bytes:
    """A backup completed successfully. `kopia_snapshot_id` is what
    kopia returned (e.g. "k1234abcd"); the operator uses this to
    invoke a restore."""
    return encode(
        BACKUP_DONE, vm=vm, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id,
        source_node=source_node,
        bytes_added=int(bytes_added),
        duration_s=float(duration_s),
        label=label,
    )


def backup_failed(vm: str, target_id: str, reason: str, *,
                  source_node: str = "", label: str = "") -> bytes:
    return encode(
        BACKUP_FAILED, vm=vm, target_id=target_id, reason=reason,
        source_node=source_node, label=label,
    )


def backup_deleted(vm: str, target_id: str, kopia_snapshot_id: str,
                   reason: str = "") -> bytes:
    return encode(
        BACKUP_DELETED, vm=vm, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id, reason=reason,
    )


def restore_done(vm: str, target_id: str, kopia_snapshot_id: str, *,
                 dest_node: str = "", duration_s: float = 0.0) -> bytes:
    return encode(
        RESTORE_DONE, vm=vm, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id,
        dest_node=dest_node, duration_s=float(duration_s),
    )


def restore_failed(vm: str, target_id: str, kopia_snapshot_id: str,
                   reason: str, *, dest_node: str = "") -> bytes:
    return encode(
        RESTORE_FAILED, vm=vm, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id,
        reason=reason, dest_node=dest_node,
    )
