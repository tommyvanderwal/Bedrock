# installer/lib/cluster_addr.py

Owns the cluster-identity addressing scheme: derives a per-cluster `/24` from the
cluster UUID and computes each node's stable `/32` cluster-identity address inside
it. That `/32` lives on `lo` and is what everything cluster-internal binds to and
targets — DRBD endpoints, SeaweedFS volume/filer peering, libvirt migration, SSH
between nodes, the dashboard's inter-node calls. The mesh layer (bedrock-net)
routes those packets over whichever physical NIC has the best path. Callers across
the installer/daemon ask this module "what `/24` does this cluster use, and what
`/32` does node N get?". Pure computation — no I/O, no side effects.

The scheme lives in RFC 6598 Shared Address Space (`100.64.0.0/10`): IANA-reserved
for carrier-grade NAT, so enterprise mgmt LANs are told not to use it and won't
collide; and since cluster-internal traffic never leaves the cluster, upstream ISP
use of the range is irrelevant.

## Functions / Classes

### `cluster_loopback_prefix(cluster_uuid: str) -> str`
First three octets of this cluster's `/24`, derived from `sha256(cluster_uuid)`.
- **In:** `cluster_uuid` — the cluster's UUID string.
- **Out:** `'100.<X>.<Y>'` (e.g. `'100.97.42'`). Pure. Raises `ValueError` if
  `cluster_uuid` is empty.

### `cluster_loopback_net(cluster_uuid: str) -> str`
The cluster's `/24` in CIDR form.
- **In:** `cluster_uuid`.
- **Out:** `'<prefix>.0/24'` (e.g. `'100.97.42.0/24'`). Pure; raises `ValueError`
  (via prefix) on empty UUID. Used by the panic-neighbour route and the
  route-table filter that decides which kernel routes bedrock-net manages.

### `node_loopback_ip(cluster_uuid: str, node_index: int) -> str`
The `/32` cluster-identity address for a given node index.
- **In:** `cluster_uuid`; `node_index` (1..254).
- **Out:** `'<prefix>.<node_index>'`. Pure. Raises `ValueError` if `node_index`
  is `< 1` or `> 254`.

### `host_in_cluster_block(addr: str, cluster_uuid: str) -> bool`
Whether an address falls in this cluster's loopback `/24`.
- **In:** `addr` — an IPv4 string; `cluster_uuid`.
- **Out:** `True` if `addr` starts with `<prefix>.`, else `False`. Pure;
  string-prefix test, not a CIDR/netmask check.

## How it works

Derivation hangs entirely off `sha256(cluster_uuid).digest()`:

```
cluster_uuid ──sha256──> digest h
                          │
        h[0] % 64 ─────► second = 64 + (h[0] % 64)   → 64..127
        h[1]      ─────► third  = h[1]                → 0..255
                          │
              prefix = "100.<second>.<third>"
                          │
   node_index (1..254) ─► /32  = "<prefix>.<node_index>"
```

The second octet is forced into `64..127` so the `/24` always sits inside RFC
6598's `100.64.0.0/10`. With `second` over 64 values and `third` over 256, there
are `64 × 256 = 16,384` distinct `/24`s, so two Bedrock clusters in the same
operator network collide on a `/24` with probability `1/16,384 ≈ 0.006%`.

Guards and failure handling:
- `cluster_loopback_prefix` rejects an empty `cluster_uuid` with `ValueError`
  rather than emitting a placeholder prefix — an empty UUID is a caller bug
  (bootstrap must mint the UUID before any addressing decision), and a silent
  fallback would let two clusters derive the same colliding block. Both
  `cluster_loopback_net` and `host_in_cluster_block` inherit this guard by going
  through the prefix.
- `node_loopback_ip` range-checks `node_index` against `1..254` and raises on
  anything outside it, keeping each node to a single valid host octet in the
  `/24`. Node indices are assigned by registration order: the node that runs
  `bedrock init` gets `1`, and joiners get the lowest free index per
  `mgmt /api/nodes/register`. A node's index is independent of the mgmt-master
  role, which can move between nodes on failover — so `node_index == 1` does not
  imply current leader.
- `host_in_cluster_block` is a literal `startswith(prefix + ".")` test on the
  string, not a numeric subnet computation.

## Why

The cluster identity is a loopback `/32`, not a NIC/bridge IP, so a NIC change
never moves the address; the mesh routes it over whatever physical link has the
best path. The `/24` is keyed off the cluster UUID so every node computes the
same block deterministically with no central allocator, and lands in RFC 6598
space precisely because operator mgmt LANs are told to steer clear of it.
