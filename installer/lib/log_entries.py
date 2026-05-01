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
WITNESS_REGISTER      = "witness_register"
WITNESS_UNREGISTER    = "witness_unregister"
PARAM_CHANGE          = "param_change"


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


def node_unregister(node_name: str) -> bytes:
    return encode(NODE_UNREGISTER, node_name=node_name)


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


def witness_register(witness_id: str, addr: str,
                     witness_pubkey_hex: str,
                     encrypted_witness_key_hex: str) -> bytes:
    return encode(WITNESS_REGISTER, witness_id=witness_id, addr=addr,
                  witness_pubkey=witness_pubkey_hex,
                  encrypted_witness_key=encrypted_witness_key_hex)


def witness_unregister(witness_id: str) -> bytes:
    return encode(WITNESS_UNREGISTER, witness_id=witness_id)


def param_change(key: str, value) -> bytes:
    return encode(PARAM_CHANGE, key=key, value=value)
