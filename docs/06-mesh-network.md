# Mesh networking — bedrock-net

The mesh layer runs inside `bedrock-d`'s netd thread (`installer/lib/netd.py`).
It owns the layer between "L2 cable plugged in" and "DRBD / libvirt / NFS can
talk to a peer's cluster identity."

The architecture is **three small protocols**, each doing exactly
one job, never overlapping:

| Concern | Protocol | Cadence | Transport |
|---|---|---|---|
| **Link discovery** | Signed UDP multicast probe | per-NIC, every 1 s | `239.7.7.7:7732`, TTL=1 |
| **Latency measurement** | ICMP echo | per-`(peer, my_nic)`, every 2 s | Unprivileged kernel ICMP (`SOCK_DGRAM/IPPROTO_ICMP`), kernel timestamps |
| **Routing advertisement** | Signed UDP unicast | per-peer (not per-link), every 2 s | UDP to peer's loopback `/32` on port 7733 |

Three independent failure modes. If ICMP gets blocked, latency goes
blank but discovery and routing keep working. If advertisement is
delayed, cached routes hold until the next message. If discovery
breaks, the affected peer falls out of the per-NIC neighbour table
in 10 s and its `/32`s are withdrawn — exactly the behaviour we want.

## Identity, reachability, and cluster membership

### Identity (one per node)

Every node has one `/32` on `lo`, derived from `cluster_uuid` and
node index:

- Range: `100.X.Y.0/24` in RFC 6598 Shared Address Space
  (`100.64.0.0/10`). IANA-reserved for non-public use, so it cannot
  collide with an operator LAN.
- Per-cluster `/24` from `sha256(cluster_uuid)[0..1]`: 16,384
  distinct prefixes, two-cluster collision ≈ 0.006 %.
- Master `.1`, joiners get the lowest free index from
  `mgmt /api/nodes/register`.
- Stored as a `/32` on `lo`. **All cluster-internal protocols bind
  to or address this identity** — DRBD `path` blocks, libvirt
  migrate-uri, rqlite peer dial, SSH-from-scripts, dashboard
  inter-node. The kernel route to the `/32` picks the best physical NIC.

### Reachability (one per NIC)

Every directly-attached NIC that isn't blocklisted gets an IPv4
link-local address (`169.254.0.0/16`, RFC 3927) via NetworkManager
with `ipv4.method=link-local` (NM does the ARP probe + retry per
the standard). A kernel-only fallback exists for hosts without
NetworkManager.

Blocklist: `lo`, `virbr*`, `docker*`, `br-*`, `veth*`, `tap*`,
`tun*`, `wg*`, `kube*`, `cali*`, `cni*`, plus any interface that's
enslaved to a bridge.

### Cluster membership

rqlite (Raft-replicated SQLite) carries the authoritative list of
nodes in its `nodes` table. The mesh layer **consumes** membership —
read via `cluster_state.load_cluster()` (read-level `none`, so it
works even without quorum) — to know which nodes are legitimate
cluster members, but **does not** depend on it for routing decisions.
Routes are recomputed locally on every node, every ~250 ms, from
in-memory state fed by the three protocols above.

## Protocol 1 — Link discovery (multicast probe)

**Purpose**: confirm "a known cluster member is on this segment, at
this link address." Liveness + L2-local identity.

**Mechanism**: signed UDP multicast every 1 s per up non-blocklisted
NIC. Payload:

```
msgpack({
  v: 1,
  body: msgpack({
    cluster_uuid, node, nic, loopback, link_addr, ts
  }),
  sig: HMAC-SHA256(cluster_key, body)
})
```

`recvmsg(IP_PKTINFO)` tells the receiver which local NIC received
each probe — necessary because multiple mesh NICs share the same
`169.254.0.0/16` and source-IP alone doesn't disambiguate.

The probe carries **no RTT data and no path advertisement**. Those
are jobs 2 and 3.

In-memory state per `(peer_node, peer_nic, my_nic)`:
- `first_seen`, `last_seen`, `peer_link_addr`, `peer_loopback`.

Hysteresis: `LINK_UP` after 5 s continuous (`UP_HYSTERESIS_S`),
`LINK_DOWN` after 10 s silent (`DOWN_HYSTERESIS_S`). Routing acts on
the in-memory state on every node; only the
mgmt master records observed paths to rqlite for the dashboard
topology view (single-writer invariant).

## Protocol 2 — Latency measurement (ICMP echo)

**Purpose**: clean, kernel-timestamped RTT per direct path.

**Mechanism**: every 2 s per `(peer, my_nic)`, send an ICMP echo
request to `peer_link_addr` on the local NIC that received the
peer's discovery probe. The kernel handles both send and receive
timestamping; userspace never enters the measurement path.

```python
# Pseudocode
sock = socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)  # unprivileged
sock.bind((my_link_addr, 0))
send_at = time.monotonic_ns()
sock.sendto(echo_request(seq=N), (peer_link_addr, 0))
# … wait for reply on recv path with cmsg-attached timestamp …
recv_at = parse_timestamp_cmsg(reply)
rtt_ns = recv_at - send_at
```

**Why ICMP, not a custom UDP protocol**:
- Kernel hot-path; reply latency is single-digit µs.
- 40 years of testing.
- `tcpdump`/`mtr` can validate from outside.
- Unprivileged via `/proc/sys/net/ipv4/ping_group_range`.
- No userspace echoer needed on the peer; peer's kernel responds.

**Smoothing** — TCP RFC 6298 EWMA + variance:

```python
alpha, beta = 0.125, 0.25
if first_sample:
    srtt, rttvar = sample, sample / 2
elif not is_outlier(sample, srtt, rttvar):
    rttvar = (1 - beta) * rttvar + beta * abs(sample - srtt)
    srtt   = (1 - alpha) * srtt   + alpha * sample
```

**Outlier rejection** — three rules:

```python
def is_outlier(sample, srtt, rttvar):
    # Statistical: outside TCP's normal envelope
    if sample > srtt + 4 * rttvar:
        return True
    # Multiplicative: 10× the running mean once we have one
    if srtt > 100 and sample > 10 * srtt:
        return True
    # Absolute: on a sub-ms LAN, 100 ms+ is almost always
    # kernel-scheduler noise, buffer-bloat, or a CPU spike
    if srtt < 5_000 and sample > 100_000:
        return True
    return False
```

A 230 ms hiccup on a 100 µs link hits all three rules — sample
rejected, `srtt` stays at 100 µs, no route reshuffling. After
3 consecutive outliers (~6 s), the filter relents — the path
has genuinely degraded.

**Blip telemetry** — even rejected samples are counted, because
on a cluster that should be perfect most of the time a transient
outlier IS information:

- Per-`Neighbour` counter `rtt_blip_total` bumps on every
  rejected sample (also captures `rtt_last_blip_us`,
  `rtt_last_blip_at`).
- The daemon's 30 s status line aggregates cluster-wide:
  `blips_total=N last=<us>@<age>s_ago(peer/nic)`.
- A structured journal entry is emitted on each blip — but
  rate-limited to one per (peer, my_nic) per 5 minutes so a
  flapping path can't flood the log server:

  ```
  bedrock-net: BLIP peer=bedrock-X my_nic=enp3s0 sample_us=230000
               srtt_us=120 rule=absolute streak=1 total=7
  ```

  Each node's VLagent forwards the line to both redundant
  VictoriaLogs backends. `_msg:BLIP peer:bedrock-X | stats by
  (my_nic) count()` in LogsQL gives an operator the per-link
  blip rate over any window.

Optional: expose smoothed values as Prometheus gauges (e.g.
`bedrock_path_rtt_us{peer, nic}`, `bedrock_path_blip_total{peer,
nic}`) via `vm_exporter.py`. Operators get latency-over-time +
blip-rate graphs per peer-link.

## Protocol 3 — Routing advertisement (unicast UDP)

**Purpose**: tell every cluster peer how this node sees the
cluster, so receivers can compute their own best paths to every
destination including transit through other nodes.

**Mechanism**: every 2 s, send one UDP unicast per peer to peer's
loopback `/32` on port 7733. Kernel routes the unicast over
whichever NIC its current best-path metric picks — so we send
**one advertisement per peer regardless of how many physical links
connect us** (the architectural fix the previous design botched).

Payload:

```
msgpack({
  v: 1,
  body: msgpack({
    cluster_uuid,
    advertiser: "bedrock-X",
    seq: 42,                              # monotonic; receiver dedups
    paths: [
      { dest:      "bedrock-Z",
        via_chain: ["bedrock-X", "bedrock-Y", "bedrock-Z"],
        bottleneck_bw_mbps: 9400,         # min over the path
        cumulative_latency_us: 250 },     # sum over the path
      ...
    ]
  }),
  sig: HMAC-SHA256(cluster_key, body)
})
```

Why `bw + latency` as separate observables (not a compound metric):
bandwidth composes as **min** along a path; latency composes as
**sum**. A compound metric conflates them. Keep both raw; let
each receiver compute its own local metric.

**Loop prevention** — receiver invariants:
- Drop the advertisement if `via_chain` contains the receiver's
  own node name (would be a loop if installed).
- Accept the path if `via_chain[0] == advertiser` (advertiser
  vouches it can reach `via_chain[1]` directly).

**Receiver composition** — when node R receives an advertisement
from neighbour Y about path to Z:

```python
bw  = min(adv.bottleneck_bw_mbps,        R.bw_to(Y))
lat = adv.cumulative_latency_us + R.rtt_to(Y)
metric_R_to_Z_via_Y = local_metric(bw, lat, loss, age)
```

**Local metric function** (purely receiver-side, format-decoupled,
EIGRP-style composite tuned for modern speeds):

```python
def local_metric(bw_mbps, latency_us, loss_rate, age_s):
    bw_cost  = 1_000_000 / max(bw_mbps, 1)       # 12 at 80G, 400 at 2.5G
    lat_cost = latency_us / 100                   # 1 unit per 100 µs
    flap     = 50 if age_s < 60 else 0            # additive anti-flap
    loss     = 500 * min(1.0, loss_rate * 20)     # graded, not binary
    return bw_cost + lat_cost + flap + loss
```

**Path selection** per receiver per destination Z:
1. Collect every valid advertisement claiming a path to Z.
2. Compute local metric for each.
3. Install one `/32 via <Y's link_addr on best my_nic to Y>`
   with the lowest metric.
4. Refresh on every advertisement; withdraw if no fresh
   advertisement in 6 s (3× cadence).

## Routing layer (kernel-route emission)

`emit_routes()` runs every ~1 s. Reads in-memory state (direct
neighbours + advertised paths), produces the desired routing table,
diffs against current state, applies the delta.

Three classes of routes:

1. **Per-peer-link `/32` host routes** (one per direct path):
   `<peer_link_addr>/32 dev <my_nic> scope link`. The kernel's auto
   `169.254.0.0/16` connected route is ambiguous when multiple NICs
   hold link-local addresses; the `/32` is more specific and wins
   by longest-prefix-match. DRBD's `path { address ... }` blocks
   resolve correctly to the right wire.

2. **Per-peer loopback `/32`** with monotonic metrics:
   `<peer_loopback>/32 via <next-hop-link-addr> dev <my_nic> metric N`.
   For each cluster peer:
   - Metric 10..N: every direct path, ordered by local metric.
   - Metric 50..M: best transit paths via neighbour advertisements
     (only the single best per (dest, next-hop), not every option).
   - Kernel auto-fails-over on link-down.

3. **Panic-neighbour catch-all**:
   `<cluster_prefix>.0/24 via <freshest direct neighbour> metric 999`.
   Last resort. Loops bounded by IP TTL.

## Cross-segment LL collision detection

RFC 3927's ARP probe is L2-local — two peers on different mesh
planes can independently negotiate the same `169.254.X.Y`.
bedrock-net notices because both peers' discovery probes reach
shared discoverers.

Trigger: in `compute_routes`, the same `peer_link_addr` appears
with two different `(peer_node, peer_nic)` tuples (genuinely two
different peer interfaces).

Discriminator: if the same `(peer_node, peer_nic)` shows up with
multiple of our local NICs, that's a *segment merge* (operator
cabled two switches together) — not a collision. Skip; firing the
countermeasure would chase a legitimate peer off its address.

Countermeasure: 3× gratuitous ARP announcement on the loser's
segment from our own MAC claiming the colliding address, 0.5 s
apart. Loser's NM stack sees a different MAC asserting its IP,
defends once per RFC 3927 §2.5, sees the announcement persist on
retry, renumbers via fresh ARP probe.

Cooldown: per `(addr, my_nic)`, 30 s before re-fire. Multiple
discoverers fire in parallel safely — RFC 3927 defense is
idempotent against multiple defends.

## DRBD multi-path integration

`tier_storage.regen_drbd_configs_from_snapshot()` runs on every
cluster-state change (subscribed via `mgmt/orchestrator.py`). For
every DRBD resource — the `cluster` singleton and each per-VM
`vm-<name>-disk0` — it regenerates the resource file:

- One `connection` per peer pair.
- One `path` block per direct link observed by bedrock-net, with
  the actual per-NIC `link_addr` pair. DRBD does its own
  path-level failure detection independent of kernel routing.
- Final `path` block uses loopback `/32`s — catch-all that
  survives even when every direct path is down (kernel routes via
  panic-neighbour through any healthy peer).

`drbdadm adjust` after each regen; in-flight replication survives.

**Per-protocol path ordering**: DRBD sorts its `path` blocks by
latency (smallest RTT first) because DRBD-ack-per-write is
latency-sensitive. The kernel routing table sorts by bandwidth
(metric = 1_000_000/Mbps + lat/100) because libvirt-migrate and
bulk NFS dominate by volume. Same physical mesh, two preference
orders, applied at the protocol layer — no two-routing-tables
complexity needed.

## Lifecycle (single node, simplified)

```
   t=0   bedrock-d starts the netd thread
         └─→ load_state(): cluster_uuid, my_loopback, cluster_key
         └─→ ensure_loopback_ip(): /32 on lo
         └─→ open recv sockets (discovery + advertisement)

   t≈1s  for each up non-blocklisted NIC:
           ensure_link_local(nic)         ← NM creates bedrock-mesh-<nic>
           open send sockets

   every 1s (per NIC):
     send discovery probe                  ← protocol 1

   every 2s (per direct neighbour, per my_nic):
     send ICMP echo, parse cmsg timestamps ← protocol 2
     update srtt, rttvar

   every 2s (per peer):
     send routing advertisement            ← protocol 3
     (unicast to peer's /32; kernel picks the NIC)

   every ~250ms (tick):
     drain receive sockets
     update neighbour table (protocol 1) and
     advertisement table (protocol 3)
     sweep_hysteresis() — emit LINK_UP/DOWN/QUALITY if master
     emit_routes() — compute & apply /32s
     detect cross-segment LL collisions; fire countermeasure if any
```

## Operational verification

```bash
# Per-node addressing:
ip -br -4 addr | grep -v "127\|UNKNOWN.*lo\b"
# expected: br0 with DHCP LAN IP, enpXs0 with 169.254.x.y/16,
#           lo with the cluster's /32

# Latency per peer-link (in-memory smoothed):
journalctl -u bedrock-d | grep "srtt"            # or future Prometheus

# Per-peer kernel routes:
ip -4 route show | grep "^100\." | head -20
# expected: one /32 per peer per direct path + transit /32s + panic /24

# DRBD resource (after promote to N>=2):
cat /etc/drbd.d/cluster.res
# expected: one connection block per peer pair, one path block per
# direct link with distinct per-NIC addresses, loopback fallback last
```

## Design invariants (the no-compromise list)

1. **Three protocols, one job each.** Discovery never carries
   latency. Latency never carries advertisements. Advertisements
   never carry link-local discovery. Failure in one channel doesn't
   cascade.
2. **One advertisement per peer per cycle**, regardless of how many
   physical NICs connect them. Multiplying by NIC count is waste.
3. **rqlite is membership-of-record, not routing-of-record.**
   Routing decisions are local, in-memory, sub-second. Membership
   changes propagate at the consensus (Raft) pace (~seconds).
4. **Single-clock measurement** — every RTT is computed from one
   kernel's timestamps on both ends of the subtraction. No
   NTP/PTP dependency.
5. **Outlier rejection before smoothing**. A 230 ms transient
   never reaches the route layer.
6. **Loop-free by construction**: advertisements include the
   `via_chain`; nodes never accept advertisements whose chain
   contains themselves. IP TTL is the safety net, not the design.
7. **Multi-link to same peer**: the kernel picks the physical NIC
   via routing-metric ordering. Protocols above stay protocol-level;
   no bond, no LACP, no source-bind.
