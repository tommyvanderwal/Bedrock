# `cluster_addr.py`

**Module purpose.** Compute the per-cluster CGNAT `/24` from
`cluster_uuid` and the per-node loopback `/32` from
`(cluster_uuid, node_index)`. Pure functions — no state, no I/O.

Bedrock uses RFC 6598 Shared Address Space (`100.64.0.0/10`,
4M addresses). The cluster prefix is a deterministic
`sha256(cluster_uuid)` projection into that space, so:

- Two Bedrock clusters on the same LAN get different /24s with
  overwhelming probability (collision after ~2¹¹ clusters per the
  birthday bound on a 22-bit prefix space).
- The same cluster_uuid always gives the same /24, so a
  re-installed cluster member computes the right addresses
  without operator input.
- The /24 doesn't overlap RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`,
  `192.168.0.0/16`) or RFC 3927 link-local (`169.254.0.0/16`), so
  operator-LAN routing never collides with cluster-mesh routing.

## Functions

- `cluster_loopback_prefix(cluster_uuid) -> str` — returns
  `"100.X.Y"` where (X, Y) are bytes derived from a
  truncated SHA-256 of the cluster_uuid, masked to ensure the
  result is inside `100.64.0.0/10`. Same input always returns
  same output.
- `cluster_loopback_net(cluster_uuid) -> str` — returns
  `"100.X.Y.0/24"`. Used by netd's panic catch-all route.
- `node_loopback_ip(cluster_uuid, node_index) -> str` — returns
  `"100.X.Y.<node_index>"` where `1 ≤ node_index ≤ 254`. The
  master gets `.1`, joiner gets the lowest free index per
  `mgmt /api/nodes/register`. `.254` is reserved for the cluster
  master VIP (the floating IP that follows whichever node holds
  the mgmt role).
- `arbiter_loopback_ip(cluster_uuid) -> str` — returns
  `"100.X.Y.254"`. Convenience for `cluster_arbiter`.

## Address allocation

Indices 1..240 are operator-allocatable nodes. 241..253 are
reserved for cluster-singleton service VIPs (currently unused).
254 is the master VIP. 0 is the network address (unusable).

`.254` is also used internally by the arbiter rqlite unit and
by the SeaweedFS filer dial. So in practice the master always
binds two addresses on `lo`: its own `.<i>/32` (permanent
identity) AND `.254/32` (master role VIP, added/removed by
`cluster_arbiter.promote/demote_arbiter_host`).
