# Mesh networking — bedrock-net

The mesh layer runs inside `bedrock-d`'s netd thread (`installer/lib/netd.py`).
It owns the layer between "L2 cable plugged in" and "DRBD / libvirt / rqlite /
SeaweedFS can talk to a peer's cluster identity."

The architecture is **three small protocols**, each doing exactly
one job, never overlapping:

| Concern | Protocol | Cadence | Transport |
|---|---|---|---|
| **Link discovery** | Signed UDP multicast probe | per-NIC, every 1 s | `239.7.7.7:7732`, TTL=1 |
| **Latency measurement** | ICMP echo | per direct neighbour, every 2 s | Unprivileged ICMP (`SOCK_DGRAM/IPPROTO_ICMP`), monotonic-clock RTT |
| **Routing advertisement** | Signed UDP unicast | per-peer (not per-link), every 2 s | UDP to peer's loopback `/32` on port 7733 |

A fourth UDP unicast, the election heartbeat on port 7734, rides the
same per-peer routing as the advertisement but answers a different
question (who is master / who am I acking); it belongs to the election
layer, not the mesh, and is documented in `installer/lib/election.py`.

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
- Per-cluster `/24` from `sha256(cluster_uuid)`:
  second octet `64 + h[0]%64` (keeps it inside `100.64.0.0/10`), third
  octet `h[1]`. 64 × 256 = 16,384 distinct prefixes; two-cluster
  collision ≈ 0.006 %. (`cluster_addr.cluster_loopback_prefix`.)
- Master is index `.1`. A joiner gets the lowest free index, allocated
  by the mgmt master during the join-approve handshake (it scans the
  `nodes` table for taken loopbacks). `cluster_addr.node_loopback_ip`.
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
The neighbour table is updated every ~250 ms tick from in-memory state
fed by the three protocols above; `emit_routes()` recomputes the
desired table each tick and applies only the delta when it changes.

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

**Purpose**: clean RTT per direct path, measured against one node's
own clock so it needs no NTP/PTP between nodes.

**Mechanism**: every 2 s, send an ICMP echo request to every logged-up
neighbour's `peer_link_addr`. One unprivileged ICMP socket per local
NIC, bound to that NIC's link-local address so the kernel uses it as
the source; replies drain non-blocking each tick. Send time is stamped
with `time.monotonic_ns()` and stashed against the echo's sequence
number; the reply is matched back by sequence and source, and the RTT
is `now - send_ts`. Both timestamps come from this node's monotonic
clock, so the subtraction is single-clock regardless of peer time.

```python
# One socket per local NIC (sockets pooled per my_nic).
sock = socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)  # unprivileged
sock.bind((my_link_addr, 0))
send_at = time.monotonic_ns()
pending[seq] = (peer_link_addr, send_at)
sock.sendto(echo_request(seq=N), (peer_link_addr, 0))
# … later, on drain: match reply by seq + source …
rtt_ns = time.monotonic_ns() - pending[seq].send_at
```

**Why ICMP, not a custom UDP protocol**:
- 40 years of testing; `tcpdump`/`mtr` can validate from outside.
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

## Protocol 3 — Routing advertisement (unicast UDP)

**Purpose**: tell every cluster peer how this node sees the
cluster, so receivers can compute their own best paths to every
destination including transit through other nodes.

**Mechanism**: every 2 s, send one UDP unicast per peer to peer's
loopback `/32` on port 7733. Kernel routes the unicast over
whichever NIC its current best-path metric picks — so it sends
**one advertisement per peer regardless of how many physical links
connect them**.

Payload:

```
msgpack({
  v: 1,
  body: msgpack({
    cluster_uuid,
    advertiser: "bedrock-X",
    seq: 42,                              # monotonic; receiver dedups
    ts: <float>,
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

**Loop prevention** — receiver invariants (`process_advertisement`):
- The advertiser MUST be a direct logged-up neighbour. A
  transit-borne advertisement (C hearing A's advert relayed by B) is
  dropped — only direct neighbours' adverts drive routing. This makes
  the protocol loop-free by induction regardless of topology depth.
- `seq` must advance (wrap-aware); replays and older adverts drop.
- Drop the advert if `via_chain` contains the receiver's own node
  name (would install a loop); `via_chain[0] == advertiser`.

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
def local_metric(bw_mbps, latency_us, loss_rate=0.0, age_s=1e9):
    bw_cost  = 1_000_000 / max(bw_mbps, 1)        # 12@80G 100@10G 400@2.5G 1000@1G
    lat_cost = max(0, latency_us - 1000) / 100    # 0 below 1 ms (LAN noise
                                                  #   floor), then 1 per 100 µs
    flap     = 50 if age_s < 60 else 0            # additive anti-flap
    loss     = 500 * min(1.0, loss_rate * 20)     # graded, not binary
    return int(bw_cost + lat_cost + flap + loss)
```

Sub-millisecond latency is scheduler noise on a healthy LAN, so the
latency term floors at 1 ms and bandwidth dominates at local scale.

**Path selection** per receiver per destination Z:
1. Collect every valid advertisement claiming a path to Z.
2. Compute local metric for each.
3. Install one `/32 via <Y's link_addr on best my_nic to Y>`
   with the lowest metric.
4. Refresh on every advertisement; withdraw if no fresh
   advertisement in 6 s (`ADV_STALE_S`, 3× cadence).

## Routing layer (kernel-route emission)

`emit_routes()` runs every ~250 ms tick. Reads in-memory state (direct
neighbours + advertised paths), produces the desired routing table,
and applies only the delta (it owns only routes in this cluster's
`/24` plus the per-NIC `169.254.x.y` `/32`s — operator routes are
never touched).

Three classes of routes (metric constants: `METRIC_DIRECT_BASE = 10`,
`METRIC_TRANSIT_BASE = 100`, `METRIC_PANIC = 999`; lower wins):

1. **Per-peer-link `/32` host routes** (one per direct path):
   `<peer_link_addr>/32 dev <my_nic> scope link`. The kernel's auto
   `169.254.0.0/16` connected route is ambiguous when multiple NICs
   hold link-local addresses; the `/32` is more specific and wins
   by longest-prefix-match. DRBD's `path { address ... }` blocks
   resolve correctly to the right wire.

2. **Per-peer loopback `/32`** with monotonic metrics:
   `<peer_loopback>/32 via <next-hop-link-addr> dev <my_nic> metric N`.
   For each cluster peer:
   - Metric 10+i: direct paths, grouped by tied `local_metric`.
     Paths in the same tier (identical cost after the sub-ms latency
     floor + bucketed bandwidth) emit as a single ECMP multipath route
     (`nexthop ... weight 1` per path); the kernel L4-hashes flows
     across them (`fib_multipath_hash_policy=1`, set in
     `ensure_routing_sysctls`). Lower-cost tiers get lower metrics, so
     the kernel auto-fails-over to the next tier on link-down.
   - Metric 100+i: best transit paths from neighbour advertisements
     (only the single best per dest, not every option).

3. **Panic catch-all** for the whole cluster `/24` at metric 999:
   `<cluster_prefix>.0/24 via <best path to the mgmt master>`. The
   arbiter's `.254` VIP lives at the top of the `/24` and reaches the
   master this way without a dedicated advertisement. The master
   itself installs no `/24`-via-self (loop); it terminates `.254`
   traffic locally via the secondary `/32` on its `lo`. If the master
   is unknown or unreachable this tick (bootstrap, master-down
   transient), the route falls back to the freshest direct neighbour.
   Loops bounded by IP TTL.

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
cluster-state change (subscribed via the orchestrator). It rewrites
the `cluster` singleton's `/etc/drbd.d/cluster.res` from the snapshot's
mesh `paths` table (`render_drbd_res_mesh`), then `drbdadm adjust`s it;
in-flight replication survives. It no-ops at N=1 or if the `.res` file
is absent / the tier isn't DRBD-backed.

The singleton resource:

- One `connection` per peer pair.
- One `path` block per direct `(nic_a, nic_b)` link the path table
  observed, using the actual per-NIC `link_addr_a`/`link_addr_b`. DRBD
  runs its own per-path TCP + keepalive + carrier detection,
  independent of kernel routing. Paths are ordered speed-desc,
  RTT-asc (the same order kernel routes use).
- A final loopback-fallback `path` (peer `/32`s) is always appended
  last, so DRBD still connects when every direct path is down — the
  kernel routes it via the panic catch-all, including transit through
  a third node.

Per-VM resources (`vm-<name>-disk0`) use loopback-only `path` blocks
(`render_drbd_res`); the kernel routing layer picks the physical NIC.

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

# Mesh status (neighbours, advertisers, transit dests, RTT blips):
journalctl -u bedrock-d | grep "bedrock-net: status"

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
