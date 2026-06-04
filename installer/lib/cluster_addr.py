"""Cluster identity addressing scheme.

Every Bedrock node has a stable cluster-identity IPv4 — a /32 on `lo`
that everything cluster-internal binds to (DRBD, libvirt migration,
SeaweedFS, SSH, dashboard inter-node calls). This module owns the question
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

ALL intra-cluster traffic — DRBD endpoints, SeaweedFS volume/filer
peering, libvirt migration, SSH between nodes, the dashboard's inter-
node calls — binds to and targets the loopback /32 inside this /24.
The mesh layer (bedrock-net) routes those packets over whichever
physical NIC has the best path."""

from __future__ import annotations

import hashlib


def cluster_loopback_prefix(cluster_uuid: str) -> str:
    """Return '100.<X>.<Y>' for this cluster — the first three octets
    of the per-cluster /24. `node_loopback_ip(uuid, N)` appends the
    fourth octet (N = node_index)."""
    if not cluster_uuid:
        # Caller bug — bootstrap must create the uuid before any
        # cluster-addressing decision. Surface it rather than silently
        # falling back to a colliding placeholder.
        raise ValueError("cluster_loopback_prefix: cluster_uuid is empty")
    h = hashlib.sha256(cluster_uuid.encode()).digest()
    second = 64 + (h[0] % 64)
    third  = h[1]
    return f"100.{second}.{third}"


def cluster_loopback_net(cluster_uuid: str) -> str:
    """The /24 in CIDR form, e.g. '100.97.42.0/24'. Used by the
    panic-neighbour route + the route-table filter that decides which
    kernel routes bedrock-net manages."""
    return f"{cluster_loopback_prefix(cluster_uuid)}.0/24"


# The arbiter / cluster-VIP lives at the top of the /24 (octet 254),
# above the node-index range (1..250). It is a pure function of
# cluster_uuid — every node can derive it without rqlite — which is
# what lets bedrock-net advertise it as an ordinary connected /32 and
# keeps the routing data-plane decoupled from "who is master".
ARBITER_VIP_OCTET = 254


def cluster_vip(cluster_uuid: str) -> str:
    """The cluster arbiter VIP `100.X.Y.254` for this cluster. Single
    source of truth for the octet; cluster_arbiter binds it, bedrock-net
    advertises a /32 to it, and receivers resolve it locally (no rqlite)."""
    return f"{cluster_loopback_prefix(cluster_uuid)}.{ARBITER_VIP_OCTET}"


def node_loopback_ip(cluster_uuid: str, node_index: int) -> str:
    """The /32 cluster identity address for `node_index` (1..250).
    Indices are assigned by registration order — the node that runs
    `bedrock init` gets 1, subsequent joiners get the lowest free
    index per `mgmt /api/nodes/register`. The mgmt-master role can
    move between nodes on failover, so don't assume
    `node_index == 1` means current leader."""
    if node_index < 1 or node_index > 254:
        raise ValueError(f"node_index out of range (1..254): {node_index}")
    return f"{cluster_loopback_prefix(cluster_uuid)}.{node_index}"


def host_in_cluster_block(addr: str, cluster_uuid: str) -> bool:
    """True if `addr` is in this cluster's /24 (loopback) range."""
    prefix = cluster_loopback_prefix(cluster_uuid)
    return addr.startswith(prefix + ".")
