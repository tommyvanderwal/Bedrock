# `netd.py` — bedrock-net daemon

Companion spec for `installer/lib/netd.py`, the implementation of
the cluster mesh discovery + routing daemon. The high-level design
(why this layer exists, how it sits relative to RFC 3927 / RFC
6598 / DRBD multi-path) lives in `docs/06-mesh-network.md`. This
file is the implementation reference — what each function does,
state shapes, kernel state it touches, invariants.

## Lifecycle

Started by `/etc/systemd/system/bedrock-net.service`
(`Type=simple`, `Restart=on-failure`, `RestartSec=2`). The unit
runs `/usr/local/bin/bedrock-net`, a thin wrapper that
`netd.main()` → `netd.run_daemon()`.

`run_daemon()`:

  1. `load_state()` reads cluster_key + cluster_uuid + node_name +
     loopback_ip from `/etc/bedrock/{cluster.key,state.json,
     cluster.json}`. Raises if any required pre-init state is
     missing — systemd will restart on backoff until init completes.
  2. `ensure_loopback_ip(loopback_ip)` adds the cluster identity
     /32 to `lo`. Idempotent.
  3. `open_recv_socket()` binds the AF_INET/SOCK_DGRAM socket to
     port 7732 with IP_PKTINFO (protocol 1 — discovery multicast).
  4. `open_adv_recv_socket()` + `open_adv_send_socket()` bind UDP
     port 7733 (protocol 3 — unicast routing advertisement).
  5. Per-NIC unprivileged ICMP sockets are lazy-created on first
     send (protocol 2 — latency measurement).
  6. Main loop (`tick()` every ~250 ms + probe send every 1 s +
     ICMP round every 2 s + adv round every 2 s + route emit
     every 1 s + status log every 30 s).

## State shapes

```python
@dataclass class Daemon:
    cluster_key:        bytes (32)        # HMAC key
    cluster_uuid:       str
    my_node:            str (hostname)
    my_loopback:        str ("100.X.Y.N")
    neighbours:         dict[(peer_node, peer_nic, my_nic) -> Neighbour]
    nic_addrs:          dict[nic_name -> ipv4_address]
    probe_send_socks:   dict[nic_name -> socket]
    recv_sock:          socket            # single, IP_PKTINFO (protocol 1)
    last_routes_signature: str            # diff cache for ip-route writes
    last_arp_renumber:  dict[(addr, my_nic) -> last-fire ts]
    icmp_pingers:       dict[my_nic -> IcmpPinger]   # protocol 2
    adv_send_sock:      socket            # protocol 3 outgoing
    adv_recv_sock:      socket            # protocol 3 incoming, 0.0.0.0:7733
    adv_seq:            int               # monotonic
    adv_table:          dict[advertiser -> {seq, ts_local, sender_addr, paths}]
    best_transit_paths: dict[dest -> {metric, advertiser, neighbour, bw, lat, via_chain}]
    stopped:            bool

@dataclass class Neighbour:
    peer_node:          str
    peer_nic:           str
    peer_loopback:      str
    peer_link_addr:     str
    my_nic:             str
    first_seen:         float
    last_seen:          float
    speed_mbps:         int
    rtt_us:             int               # EWMA-smoothed from protocol 2
    rtt_var_us:         int               # TCP RFC 6298 variance
    rtt_outlier_streak: int               # consecutive rejections; 3 ⇒ accept
    # Blip telemetry — rejected samples are still counted, surfaced
    # on the 30 s status line, and emitted as structured journal
    # lines (rate-limited to one per neighbour per 5 min).
    rtt_blip_total:        int
    rtt_last_blip_us:      int
    rtt_last_blip_at:      float
    rtt_last_blip_log_at:  float
    logged_up:          bool              # crossed up-hysteresis threshold
    last_quality_log:   float
```

## Kernel state touched

  * `lo`: adds `<my_loopback>/32`.
  * Per mesh NIC: NetworkManager creates `bedrock-mesh-<nic>`
    connection profile via `nmcli con add ... ipv4.method link-local`.
    NM handles ARP probe + claim (RFC 3927).
  * Routing table: `ip route replace` per peer (loopback `/32`) +
    per peer link (LL `/32`) + cluster-prefix panic catch-all.
  * Multicast group `239.7.7.7` joined on each mesh NIC.
  * Raw socket on `AF_PACKET/SOCK_RAW/ETH_P_ARP` when firing the
    collision countermeasure (ephemeral, closed after the 3-frame
    burst).

## Functions (entry points)

| Function | Purpose |
|---|---|
| `run_daemon()` | Main entry; never returns under normal operation. |
| `tick(d, …)` | One iteration of the main loop. Drains probes, refreshes interface set, sweeps hysteresis. |
| `process_probe(d, body, …)` | Update/insert a Neighbour from a verified probe payload. |
| `sweep_hysteresis(d)` | Emit LINK_UP / LINK_DOWN / LINK_QUALITY entries when thresholds crossed. |
| `emit_link_event(kind, d, n, …)` | Append the entry via `rust_ipc`. Master-only; returns True on success so the caller can update `logged_up`. |
| `emit_routes(d)` | Diff desired vs current routes; apply delta with `ip route del`/`replace`. |
| `compute_routes(d)` | Build the desired route list from in-memory neighbour table. Calls `_detect_and_handle_ll_collision`. |
| `_detect_and_handle_ll_collision(routes, seen, n, d)` | Discriminate first-seen vs merge vs real collision vs duplicate; fire countermeasure on real collision. |
| `arp_force_renumber(addr, dev)` | 3× gratuitous ARP on `dev` claiming `addr` from our MAC. Triggers RFC 3927 defense on the loser. |
| `ensure_link_local(nic)` | Idempotent: create/keep NM `bedrock-mesh-<nic>` profile, return assigned IP. |
| `i_am_mgmt_master(d)` | True if `state.json` role contains `"mgmt"`. Gates log writes. |
| `current_cluster_routes(uuid)` | Read existing `ip route` entries that we own (cluster `/24` + our `169.254.x.y` `/32`s). |
| `icmp_send_round(d, now_ns)` | Protocol 2: send ICMP echo to every logged-up neighbour through its specific `my_nic`. |
| `icmp_drain_replies(d, now_ns)` | Non-blocking recv on every per-NIC ICMP socket; match by seq; update Neighbour RTT via `_update_neighbour_rtt`. |
| `_update_neighbour_rtt(d, key, sample_us)` | TCP RFC 6298 EWMA with 3-rule outlier rejection (statistical / 10× multiplicative / 100 ms absolute). |
| `local_metric(bw_mbps, latency_us, loss, age)` | EIGRP-style composite metric; receiver-side, format-decoupled. |
| `adv_send_round(d, now_ts)` | Protocol 3: send one signed unicast advertisement per peer (kernel picks NIC). |
| `adv_drain(d, now_ts)` | Drain incoming advertisements non-blocking; call `process_advertisement` on each. |
| `process_advertisement(d, body, addr, ts)` | Validate (advertiser must be direct neighbour; seq must advance); store in `adv_table`. |
| `recompute_best_transit_paths(d, ts)` | Path-vector selection per destination: drop loops, compose `bw=min`/`lat=sum`, rank by `local_metric`. |
| `build_advertisement_paths(d)` | Compose paths[] for outgoing advertisement: direct neighbours + selected transit destinations. |

## Invariants

1. **Single-writer**: only the mgmt master appends `LINK_*` to the
   bedrock-rust log. `emit_link_event` early-returns True (without
   writing) on followers so the in-memory `logged_up` state still
   tracks correctly.
2. **Loopback `/32` persistence**: `ensure_loopback_ip` is
   idempotent and called on every tick once `my_loopback` is known.
   Survives `bedrock-net` restart without state on disk.
3. **NM profile uniqueness**: at most one `bedrock-mesh-<nic>`
   profile per NIC. `_nmcli_con_exists` guards `nmcli con add`.
4. **Route ownership**: bedrock-net only touches routes whose dest
   is in this cluster's `/24` (derived from cluster_uuid) plus the
   `/32`s we install in `169.254.0.0/16` with `scope link`. All
   other routes are operator's.
5. **Collision cooldown**: per `(addr, my_nic)`, 30 s minimum
   between successive `arp_force_renumber` calls from this node.
   Multiple discoverers in parallel each maintain their own cooldown.
6. **One advertisement per peer per cycle** (protocol 3). The
   kernel picks the egress NIC from the cluster `/32` route — we
   deliberately do NOT send one per `(my_nic, peer)`. If a peer is
   reachable on 5 NICs, that's still 1 unicast every 2 s, not 5.
7. **Advertiser must be a direct neighbour**. Transit-borne
   advertisements (e.g. C receiving A's adv via B) are dropped.
   This is what makes the path-vector loop-free regardless of
   topology depth.
8. **Outlier rejection before EWMA update** (protocol 2). A 230 ms
   transient on a 100 µs link is rejected, srtt stays put, no
   operator-visible event, no route reshuffle. After 3 consecutive
   outliers the filter relents — that's a genuine degradation.

## Failure modes + recovery

| What | Symptom | Recovery |
|---|---|---|
| IPC to bedrock-rust unreachable | `emit_link_event` returns False; `logged_up` stays False; next sweep retries | Self-healing — next sweep after rust comes back |
| NM not installed | `ensure_link_local` falls back to direct `ip addr add` with MAC-derived LL | Degraded; operator should install NM or systemd-networkd |
| Bridge multicast snooping enabled | Probes don't cross bridge → paths never come up | Operator must `echo 0 > /sys/class/net/<bridge>/bridge/multicast_snooping` (the testbed's `spawn.py` does this) |
| LL collision across segments | `/32 dev <nic>` install fails (EEXIST with different dev) | `_detect_and_handle_ll_collision` triggers 3× gratuitous ARP; loser renumbers within ~5 s |
| Interface flap | Neighbour times out at 30 s hysteresis; routes via that NIC removed | Self-healing; new probes after recovery install fresh routes |

## Reference

  * Design doc: `docs/06-mesh-network.md`
  * Uncertainties: `docs/mesh-network-v1-uncertainties.md`
  * Lessons: `docs/lessons-log.md` (L32–L35)
  * Address scheme: `installer/lib/cluster_addr.py`
