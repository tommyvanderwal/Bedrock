"""Cluster-key bootstrap.

Writes /etc/bedrock/cluster.key, the 32-byte shared secret used for
witness HMAC signing, which needs a stable secret present on every
node. mgmt_install and agent_install call `write_cluster_key(material)`
at install time.
"""

from __future__ import annotations

import secrets
from pathlib import Path

CLUSTER_KEY = Path("/etc/bedrock/cluster.key")


def write_cluster_key(material: bytes | None = None) -> bytes:
    """Write the cluster's 32-byte HMAC key. Idempotent — preserves an
    existing key. Returns the key bytes (so the master can replicate
    it to peers during join handshake)."""
    if CLUSTER_KEY.exists():
        return CLUSTER_KEY.read_bytes()
    CLUSTER_KEY.parent.mkdir(parents=True, exist_ok=True)
    key = material or secrets.token_bytes(32)
    if len(key) != 32:
        raise ValueError("cluster key must be 32 bytes")
    CLUSTER_KEY.write_bytes(key)
    CLUSTER_KEY.chmod(0o600)
    return key
