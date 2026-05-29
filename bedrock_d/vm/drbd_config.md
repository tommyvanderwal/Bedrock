# bedrock_d/vm/drbd_config.py

Single source of DRBD `.res` config text for every DRBD resource in the cluster
— the per-node `cluster` singleton and each per-VM disk. It renders the file as a
string; it does not touch disk or run `drbdadm`. DRBD storage saga steps call
`render()`, write the result to the path from `res_file_path()` on each peer, then
run `drbdadm create-md --max-peers=7`. Every resource uses **external metadata**
on a separate thin LV (data disk `/dev/<vg>/bedrock-data-<r>`, meta disk
`/dev/<vg>/bedrock-meta-<r>`, names from `vm/lvm.py`).

## Functions / Classes

### `class Peer` (frozen dataclass)
One node that hosts a given DRBD resource.
- **Fields:** `node_name` (str) · `host` (LAN IP, operator-routable, used for SSH
  from master) · `loopback_ip` (the node's cluster-identity `/32`, the DRBD path
  target) · `node_id` (int DRBD node-id, `0..max_peers-1`, stable per node).

### `drbd_port_for(minor: int) -> int`
Map a DRBD minor to its TCP port in the 7700–7799 band.
- **In:** `minor` — the resource's DRBD minor.
- **Out:** `7700 + (minor - 1100)`. Pure; no side effects. Same formula for the
  cluster singleton (minor 1101 → 7701) and per-VM disks (1102+ → 7702+).

### `render(resource, *, minor, peers, max_peers=7, vg=VG_NAME) -> str`
Render the full `.res` file text for one resource.
- **In:** `resource` — resource name (also the LV-name suffix and `resource {…}`
  label); `minor` — DRBD minor (sets device path and port); `peers` — list of
  `Peer` (1 for cattle, 2 for pet, 3 for vipet, up to `max_peers`); `max_peers` —
  cap (default 7); `vg` — volume-group name for the disk/meta-disk paths (defaults
  to the resolved `VG_NAME` from `lvm.py`).
- **Out:** the `.res` config as a string. Pure (no I/O). Raises `ValueError` on
  empty `peers`, on `len(peers) > max_peers`, or on duplicate `node_id`.

### `res_file_path(resource: str) -> str`
Canonical on-disk location for a resource's config.
- **In:** `resource` name.
- **Out:** `"/etc/drbd.d/<resource>.res"`. Pure; no side effects.

### Module constants
- `DRBD_PORT_BASE = 7700`, `DRBD_MINOR_BASE = 1100` — the port-mapping anchors.

## How it works

`render()` validates first, then builds two block sets and wraps them in a
`resource` header.

Validation guards (each raises `ValueError`):
1. `peers` non-empty.
2. `len(peers) <= max_peers`.
3. No two peers share a `node_id`.

It computes one shared `port = drbd_port_for(minor)` and resolves the two LV
names once via `data_lv_for(resource)` / `meta_lv_for(resource)`. Every peer's
data and meta disk in the file points at `/dev/<vg>/bedrock-data-<r>` and
`/dev/<vg>/bedrock-meta-<r>` (external metadata).

The `connection` blocks form a **full mesh**: every unordered pair of peers
`(i, j)` gets one block, so an N-peer resource emits `N*(N-1)/2` connections.
Each path lists both hosts by `node_name` at their `loopback_ip:port`.

```
resource <r> {
    protocol C;
    disk { on-io-error detach; }
    net {
        allow-two-primaries  no;            # single-primary
        after-sb-0pri  discard-zero-changes;
        after-sb-1pri  discard-secondary;   # split-brain auto-recovery
        after-sb-2pri  disconnect;          # both-primary -> bail, no data loss
    }
    on <node_name> {                        # one per peer
        device /dev/drbd<minor> minor <minor>;
        disk   /dev/<vg>/bedrock-data-<r>;
        meta-disk /dev/<vg>/bedrock-meta-<r>;
        node-id <node_id>;
    }
    ...
    connection { path {                     # one per peer-pair (full mesh)
        host <a> address <a.loopback_ip>:<port>;
        host <b> address <b.loopback_ip>:<port>;
    } }
    ...
}
```

The DRBD connection address is the peer's loopback `/32`, not a NIC address, so
the kernel routes the replication link over bedrock-net's path table and a NIC
change never moves the endpoint. The port stays inside 7700–7799 and clear of
the netd mesh ports (7732/7733/7734); the VM minor allocator skips minors
1132/1133/1134 to keep their derived ports out of that range.

## Why

Single-primary (`allow-two-primaries no`) plus conservative split-brain policies
keep at most one writer and prefer a clean disconnect over silently merging
divergent data. External metadata on its own LV lets `max-peers` (and thus the
bitmap) be pre-reserved at `create-md` time, so peers can be added later without
resizing or downtime.
