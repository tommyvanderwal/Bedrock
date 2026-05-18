"""Cluster-key bootstrap.

Historically this module rendered /etc/bedrock/daemon.toml and ran
the bedrock-rust daemon. The Rust daemon is gone (the Python
bedrock-net daemon absorbed witness + election + routing); only the
cluster_key bootstrap remains here, since witness HMAC signing needs
a stable shared secret on every node.

Keeping the module file (rather than moving the one surviving helper
into lib/witness.py) so existing callers (mgmt_install, agent_install)
keep working — they call `write_cluster_key(material)` at install time.
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
