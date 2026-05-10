"""Cluster identity addressing scheme.

Every Bedrock node has a stable cluster-identity IPv4 — a /32 on `lo`
that everything cluster-internal binds to (DRBD, libvirt migration,
NFS, SSH, dashboard inter-node calls). This module owns the question
"what /24 does this cluster use, and what /32 does node N get?"

Lives in RFC 6598 Shared Address Space (100.64.0.0/10):

  * IANA-reserved for the ISP-to-CPE link in carrier-grade NAT.
  * Enterprise LANs are explicitly told not to use it (RFC 6598 §3),
    so an operator's mgmt network won't conflict with our scheme.
  * Cluster-internal traffic never leaves the cluster, so we don't
    care that ISPs use these addresses for their own purposes
    upstream. Loopback /32s aren't advertised anywhere outside
    the cluster.

The /24 per cluster is derived from `sha256(cluster_uuid)`:
  * second octet  = 64 + (h[0] % 64)   → 64..127, the valid range
                                          inside RFC 6598's /10
  * third  octet  = h[1]                → 0..255

That's 64 × 256 = 16,384 distinct /24s available; the chance of two
Bedrock clusters in the same operator network deriving the same /24
is 1/16,384 ≈ 0.006%.

Backwards-compat: old clusters that pre-date this scheme have
loopback IPs already recorded in cluster.json (typically in the old
10.99.0.0/24 prefix). Those addresses keep working — bedrock-net
reads loopback_ip per-node from state.json/cluster.json directly
rather than re-deriving — so this scheme only governs newly-allocated
identities."""

from __future__ import annotations

import hashlib


def cluster_loopback_prefix(cluster_uuid: str) -> str:
    """Return '100.<X>.<Y>' for this cluster — the first three octets
    of the per-cluster /24. `node_loopback_ip(uuid, N)` appends the
    fourth octet (N = node_index)."""
    if not cluster_uuid:
        # Safe fallback — should never trigger in practice because
        # bootstrap creates a uuid before anyone calls this. Use the
        # legacy prefix so an old cluster.json on disk still resolves.
        return "10.99.0"
    h = hashlib.sha256(cluster_uuid.encode()).digest()
    second = 64 + (h[0] % 64)
    third  = h[1]
    return f"100.{second}.{third}"


def cluster_loopback_net(cluster_uuid: str) -> str:
    """The /24 in CIDR form, e.g. '100.97.42.0/24'. Used by the
    panic-neighbour route + the route-table filter that decides which
    kernel routes bedrock-net manages."""
    return f"{cluster_loopback_prefix(cluster_uuid)}.0/24"


def node_loopback_ip(cluster_uuid: str, node_index: int) -> str:
    """The /32 cluster identity address for `node_index` (1..250).
    Master gets index 1; joiners get the lowest free index per
    `mgmt /api/nodes/register`."""
    if node_index < 1 or node_index > 254:
        raise ValueError(f"node_index out of range (1..254): {node_index}")
    return f"{cluster_loopback_prefix(cluster_uuid)}.{node_index}"


def host_in_cluster_block(addr: str, cluster_uuid: str) -> bool:
    """True if `addr` is in this cluster's /24 (loopback) range."""
    prefix = cluster_loopback_prefix(cluster_uuid)
    return addr.startswith(prefix + ".")
