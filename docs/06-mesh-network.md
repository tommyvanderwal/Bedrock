# Mesh networking — bedrock-net

A separate daemon per node that owns the layer between "L2 cable
plugged in" and "DRBD / libvirt / NFS can talk to a peer's cluster
identity." Everything in here is Bedrock-side; the kernel and
NetworkManager do the heavy lifting per their own RFCs.

## Why a separate layer

Traditional HCI products assume a managed switch fabric and let the
OS handle bonds / LACP / VLANs. That model breaks at Bedrock's
target shape — 2–6 nodes in an MSP rack, ad-hoc cabling, sometimes
USB4 cross-connects, sometimes a cheap unmanaged switch, sometimes
a mix. The operator wants to plug a cable into any port and have it
work; we want the system to *know* where each interface goes and
route per-peer through the best available path. That's mesh-routing
territory (Babel, Calico/Cilium-style underlays), not HCI bonding.

Bedrock-net is the layer that makes plug-any-cable-anywhere work.

## Identity vs reachability

**Identity** — every node gets one stable IPv4 `/32`, derived from
the cluster's UUID and a node index:

  * Range: `100.X.Y.0/24` in RFC 6598 Shared Address Space
    (`100.64.0.0/10`). IANA-reserved for non-public use, won't
    collide with operator LANs.
  * Per-cluster `/24` derived from `sha256(cluster_uuid)` so two
    Bedrock clusters in the same operator network can co-exist
    (16,384 distinct `/24`s; collision ≈ 0.006%).
  * Master gets `<prefix>.1`, joiners get the lowest free index
    allocated by `mgmt /api/nodes/register`.
  * Stored on the node as a `/32` on `lo`. Survives any NIC
    topology — protocols bind to this, not to a physical NIC's IP.

**Reachability** — every directly-attached NIC gets an IPv4
link-local address (`169.254.0.0/16`, RFC 3927).
NetworkManager is the preferred assignment path: bedrock-net
creates a per-NIC `bedrock-mesh-<nic>` profile with
`ipv4.method=link-local`, and NM does the ARP probe + retry on
collision per the standard.

When `nmcli` isn't on PATH (operator using systemd-networkd or a
bare-kernel setup), bedrock-net falls back to assigning a
deterministic `169.254.X.Y` derived from the NIC MAC via plain
`ip addr add` — no ARP probe, just enough to let the mesh layer
send/receive. Within-segment collisions in that fallback path are
caught the same way as cross-segment ones (by the cluster-protocol
collision logic below), so safety is preserved.

The DHCP-assigned LAN address on the mgmt bridge (`br0`) is left
alone; bedrock-net just picks it up. Interfaces matching the prefix
blocklist (`lo`, `virbr*`, `docker*`, `br-*`, `veth*`, `tap*`,
`tun*`, `wg*`, `kube*`, `cali*`, `cni*`) and bridge slaves
(interfaces with `/sys/class/net/<nic>/master` set) are skipped
entirely — operator-managed networks, container plumbing, and
bridge ports are not mesh path candidates.

## What flows on the wire

Each node sends a signed multicast probe every 1 s on every up
interface that isn't a bridge slave:

  * Destination: `239.7.7.7:7732` (link-local multicast, TTL=1).
  * Payload: msgpack `{cluster_uuid, node, nic, loopback, link_addr,
    ts}` HMAC-SHA256-signed by the cluster's pre-shared key.
  * Receivers verify the signature, drop anything not addressed to
    their `cluster_uuid` or claiming to be themselves, and update
    an in-memory `Neighbour` keyed by `(peer_node, peer_nic, my_nic)`.

`recvmsg(IP_PKTINFO)` tells the receiver which local NIC each probe
arrived on — necessary because multiple mesh NICs share the same
`169.254.0.0/16` and source-IP alone doesn't disambiguate.

## Hysteresis + log

In-memory neighbour state updates within ms. Only durable transitions
get appended to the bedrock-rust log:

  * `LINK_UP` after a path has been continuously reachable for ≥5 s.
  * `LINK_DOWN` after a path has been silent for ≥30 s.
  * `LINK_QUALITY` is **rate-limited first, change-sensitive second**:
    the sweep checks `(now − last_quality_log) ≥ QUALITY_REFRESH_S`
    (60 s default) before considering whether the speed bucket
    changed by ≥25 %. So a quality event is never emitted sooner
    than the rate limit, even on a big sudden change. The trade is
    log volume — at scale we'd rather miss a fast transient than
    flood the cluster log; large persistent changes get captured on
    the next sweep after the gate elapses.

The log carries `(node_a, nic_a, link_addr_a, node_b, nic_b,
link_addr_b, speed_mbps, rtt_us, observed_at)`. Fold canonicalises
to `(a, b)` lex-sorted so the same physical path is one entry
regardless of which end wrote it.

Single-writer: only the mgmt master appends `LINK_*` entries.
Followers keep their full in-memory neighbour table for routing
decisions but don't write to the log — otherwise their writes
diverge the hash chain and break master's replication of
subsequent membership entries.

Implementation nuance: on a follower, `emit_link_event()` returns
`True` (success) without actually appending. This is deliberate —
the caller in `sweep_hysteresis` flips `logged_up = True` on
success, which advances the local state machine past the
up-hysteresis edge. Without that, every sweep on a follower would
re-attempt the (impossible) append forever. The fact that the log
itself doesn't gain the entry is fine because master is observing
the same physical paths and writing them on master's side; followers
just need to know "I've crossed this threshold locally" so their
own routes get installed.

## Kernel routing

`emit_routes()` runs every ~1 s, builds the desired set of `ip
route` calls, diffs against current state, applies the delta:

  1. **Per-peer-link `/32` host routes**:
     `<peer_link_addr>/32 dev <my_nic> scope link`. Required
     because the kernel's auto-installed `169.254.0.0/16 dev <nic>`
     is ambiguous when multiple NICs hold link-local addresses —
     the `/32` is more specific and wins by longest-prefix-match.
     DRBD's `path { address ... }` blocks resolve correctly.

  2. **Per-peer loopback `/32`** with monotonic metrics:
     `<peer_loopback>/32 via <peer_link_addr> dev <my_nic> metric N`.
     One per physical link to the same peer, ordered by speed desc
     / RTT asc, so the kernel uses the fastest path first and
     auto-fails-over on link-down.

  3. **Panic-neighbour catch-all**:
     `<cluster_prefix>.0/24 via <freshest neighbour> metric 999`.
     Last resort for any cluster identity not specifically routed
     — survives weird topologies and gives DRBD a path even
     mid-renumber. Loops bounded by IP TTL.

## DRBD multi-path

The function `regen_drbd_configs_from_snapshot()` lives in
`installer/lib/tier_storage.py` (not in `netd.py`). The mgmt
orchestrator's subscriber calls it after every relevant log fold
— see `mgmt/orchestrator.py::_apply_entry`. When the path table
changes, every tier currently in DRBD mode regenerates its
resource file:

  * One `connection` per peer pair.
  * One `path { host A address <addr_a>:port; host B address <addr_b>:port; }`
    per direct link the path table observed, ordered fastest-first.
    The addresses are the real per-NIC link-local IPs, so DRBD
    does its own path-level failure detection independent of
    kernel routing.
  * Final `path` block points at the loopback `/32`s — guaranteed
    survival even when every direct path is down, because the
    panic-neighbour route catches anything in the cluster /24.

`drbdadm adjust tier-<resource>` after each regen — in-flight
replication survives.

## Cross-segment LL collision detection

RFC 3927's ARP probe is L2-local — two peers on different mesh
planes can independently negotiate the same `169.254.X.Y` and
neither knows. Bedrock-net notices because both probes arrive at
shared discoverers (any node with NICs on both segments).

Trigger: in `compute_routes`, the same `peer_link_addr` appears
with two different `(peer_node, peer_nic)` tuples — i.e. genuinely
two different peer interfaces.

Discriminator (the key safety check): if the same
`(peer_node, peer_nic)` shows up with multiple of our local
`my_nic`s, that's an L2 *merge* (operator cabled two switches
together, or a switch loop) — not a collision. We skip the
countermeasure; firing it would chase a legitimate peer off its
address.

Countermeasure on a real collision: open `AF_PACKET/SOCK_RAW` on
the loser's segment, emit 3× gratuitous ARP announcements claiming
the colliding address from our own MAC, 0.5 s apart. The loser's
NM/avahi stack sees a different MAC asserting its own IP, defends
once per RFC 3927 §2.5, sees the announcement persist, renumbers.

Cooldown: per `(addr, my_nic)`, 30 s before re-firing. Multiple
discoverers in parallel are fine — each maintains its own cooldown,
and RFC 3927 defense is idempotent against multiple defends.

## Lifecycle

```
                ┌────────────────────────────────────────────────────┐
                │  Node 1  (mgmt master)                            │
                │                                                    │
   bedrock      │   NM       bedrock-net           bedrock-rust      │
   bootstrap →  │  per-NIC ──┐                          ↓            │
   bedrock      │  LL profile│ probes (UDP 7732)   IPC append        │
   init      →  │  10.99.0.1  └──→ multicast group  LINK_UP/         │
                │                       │           LINK_DOWN/       │
                │                       │           LINK_QUALITY     │
                │                       │                  │         │
                │       gossip          ↓                  ↓         │
                │       table       neighbour table   replicated log │
                │                       │                  │         │
                │                       │           view_builder fold│
                │                       │                  │         │
                │       emit_routes ←───┘             render_drbd    │
                │            │                            │          │
                │            ↓                            ↓          │
                │       ip route replace ...        drbdadm adjust   │
                │            │                            │          │
                │            ↓                            ↓          │
                │       Linux FIB                  /etc/drbd.d/      │
                └────────────────────────────────────────────────────┘
```

## Operational verification

```bash
# Per-node addressing:
ip -br -4 addr | grep -v "127\|UNKNOWN.*lo\b"
# expected: br0 with DHCP LAN IP, enpXs0 with 169.254.x.y/16,
#           lo with the cluster's /32

# Path table:
cat /etc/bedrock/cluster.json | jq '.paths | length'
# expected: roughly (mesh-segments × peers) on the master

# Per-peer kernel routes:
ip -4 route show | grep "^100\." | head -20
# expected: one /32 per peer per NIC + a panic /24

# DRBD resource (after promote to N>=2):
cat /etc/drbd.d/tier-bulk.res
# expected: one connection block per peer pair, one path block per
# direct link with distinct per-NIC addresses, loopback fallback last
```

## Limits + known gaps

Documented in detail in `mesh-network-v1-uncertainties.md`. The
short list:

  * Followers don't POST observations to master, so cluster.json's
    path table is master-centric (inter-peer paths absent from log,
    but each follower's local routing decisions are correct).
  * `speed_mbps` / `rtt_us` are 0 (we don't measure yet); Dijkstra
    ties on speed and falls through to NIC-name lexicographic
    tiebreak.
  * orchestrator's render-on-entry restart loop hits systemd
    start-limit during init burst; bumped to 20/60s as a band-aid
    until proper debouncing.
