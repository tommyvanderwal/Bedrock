# Ports and networks

Every port Bedrock listens on, speaks to, or crosses between nodes.

## Host ports (every node)

| Port | Protocol | Service | Bound on | Notes |
|---|---|---|---|---|
| 22 | TCP | sshd | all IPs | Operator + inter-node orchestration + `qemu+ssh` migration |
| 9090 | TCP | Cockpit | all IPs | Per-node web console |
| 9100 | TCP | node-exporter | all IPs | Prometheus metrics, scraped via `bedrock-vmagent` |
| 9177 | TCP | vm-exporter | all IPs | libvirt + DRBD metrics |
| 7700-7799 | TCP | DRBD replication | per-NIC mesh paths + loopback `/32` | Port = `7700 + (minor - 1100)`. Cluster singleton = minor 1101 → 7701; per-VM disks = minors 1102-1189 → 7702-7789 (minors 1132/1133/1134 skipped so DRBD never lands on the mesh UDP ports 7732/7733/7734). The mesh-fold renderer emits one `path {}` per direct `(nic_a, nic_b)` pair (using the NICs' 169.254 link-local addrs) plus an always-last loopback-`/32` fallback path; DRBD fails over between paths itself, the loopback path rides the kernel route as the catch-all. See `docs/06-mesh-network.md`. |
| 7732 | UDP | bedrock-net probe (multicast) | all mesh NICs | **Protocol 1.** Signed multicast on `239.7.7.7`, TTL=1, every 1 s per up NIC. HMAC-SHA256 over `cluster.key`. Discovery only — carries no RTT or routes. `netd.PROBE_PORT`. |
| ICMP | echo | bedrock-net latency | per-NIC link-local | **Protocol 2.** Unprivileged kernel ICMP (`SOCK_DGRAM/IPPROTO_ICMP`) every 2 s per `(peer, my_nic)`; kernel timestamps drive the EWMA. Needs `/proc/sys/net/ipv4/ping_group_range` to cover the daemon's gid. |
| 7733 | UDP | bedrock-net advertisement (unicast) | peer loopback `/32` | **Protocol 3.** Signed UDP unicast every 2 s per peer (one per peer, not per link — kernel picks the NIC). Path-vector payload with `via_chain` for loop prevention. HMAC-SHA256 over `cluster.key`. `netd.ADV_PORT`. |
| 7734 | UDP | bedrock-net election heartbeat (unicast) | peer loopback `/32` | **Protocol 4.** One signed heartbeat per peer per 1 s election tick: `believed_master`, `transitioning`, advertised arbiter-DRBD UUID, `ack_target`. Drives `MASTER_LOSS_MISSES` leader-loss detection. `netd.HB_PORT`. |
| EtherType 0x88CC | L2 raw | bedrock-net LLDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET` listener on LLDP multicast MAC `01:80:c2:00:00:0e`. Decoded into `/run/bedrock/switch_neighbors.json` + `NIC_SWITCH` journal lines. Never sends LLDP. |
| SNAP 0x2000 | 802.3 LLC | bedrock-net CDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET/ETH_P_802_2` listener on `01:00:0c:cc:cc:cc`. Same processing path as LLDP. Never sends CDP. |
| 5678 | UDP | bedrock-net MNDP listener | all IPs | **L2 discovery sidecar.** Receive-only UDP-broadcast listener for MikroTik Neighbor Discovery. `IP_PKTINFO` for per-NIC attribution. Same processing path as LLDP/CDP. Never sends MNDP. |
| 4001 | TCP HTTPS (mTLS) | rqlite HTTP API | `0.0.0.0` per node | Raft-replicated SQLite — cluster source of truth. mTLS via `node.crt` / `node.key.pem` / `ca.crt`. Local code dials `127.0.0.1:4001`. |
| 4002 | TCP | rqlite Raft | per node | Raft transport between rqlited peers. |
| 9333 | TCP | SeaweedFS master | Raft on the `min(3,N)` lowest-octet nodes | Volume/collection metadata + master Raft (`seaweedfs.MASTER_PORT`). |
| 8080 | TCP | SeaweedFS volume | `0.0.0.0` per node | Blob storage (`seaweedfs.VOLUME_PORT`). This is the weed volume; the dashboard is on 8443. |
| 8333 | TCP | SeaweedFS S3 gateway | `0.0.0.0` per node | S3 API that external clients connect to (`seaweedfs.S3_PORT`). |
| 5900-5999 | TCP | QEMU VNC | `0.0.0.0` | One per running VM; libvirt autoport (display :0 → 5900, :1 → 5901, ...). |
| 49152-49215 | TCP | QEMU live-migrate | per-NIC mesh paths via peer loopback `/32` | libvirt's default migration port range; migrate-uri is `tcp://<peer-loopback>`, the kernel route to that `/32` picks the NIC. |

## Control-plane and singleton-role ports

The mgmt dashboard + API run inside `bedrock-d` on every node. The filer and
arbiter rqlite are singleton roles on whichever node holds the `.254` arbiter VIP.

| Port | Protocol | Service | Bound | Notes |
|---|---|---|---|---|
| 8443 | TCP HTTPS | FastAPI mgmt dashboard + LAN API | `0.0.0.0` per node | Operator-authenticated (`operator_auth.py`). `/ws` WebSocket + `/vnc/{vm}` VNC proxy. `/api/topology` returns the physical-topology rollup (per-node `switch_neighbors.json` grouped by chassis ID; cached at `/run/bedrock/physical_topology.json`). Before a TLS cert exists, `bedrock-d` serves the LAN API on `0.0.0.0:8444` (plain HTTP, bootstrap-only); a `bedrock-d` restart flips it to 8443 once the cert is present. |
| 8001 | TCP HTTP | local CLI / intra-process mgmt API | `127.0.0.1` per node | The `bedrock` CLI dials this; always bound regardless of cert state. **Auth-exempt** — loopback is trusted local root; LAN requests on 8443/8444 still require an operator token. |
| 80 | TCP | `bedrock-redirect` | `0.0.0.0` per node | 302 → `https://<dashed-ip>.my.local-ip.co:8443<path>`. Binds 80 via `CAP_NET_BIND_SERVICE` under `DynamicUser`. |
| 4011 | TCP HTTPS (mTLS) | rqlite-arbiter HTTP API | `.254` VIP | Arbiter rqlite voter co-located with the `.254` arbiter; 4011/4012 so it coexists with the per-node rqlited on 4001/4002. |
| 4012 | TCP | rqlite-arbiter Raft | `.254` VIP | Arbiter Raft transport. |
| 8428 | TCP | VictoriaMetrics backend | the 2 metrics backends | `/api/v1/query`, `/api/v1/write`, `/-/reload`. Only on `obs_backends.metrics` nodes; `bedrock-vmagent` elsewhere dual-writes here. |
| 9428 | TCP | VictoriaLogs backend HTTP | the 2 logs backends | `/internal/insert` (writes), `/select/logsql/query` (reads). Only on `obs_backends.logs` nodes; `bedrock-vlagent` elsewhere dual-writes here. |
| 5140 | TCP | VictoriaLogs **agent** syslog (`bedrock-vlagent`) | every node | RFC 5424 syslog ingest; the local vlagent listens here and forwards to both VL backends. |
| 5141 | TCP | VictoriaLogs **backend** syslog (`bedrock-vl`) | the 2 logs backends | Backend-side syslog listener, distinct from the agent's 5140. |
| 8888 | TCP | SeaweedFS filer (HTTP + ISO library namespace) | `.254` VIP | POSIX filer namespace, DRBD-backed on the `cluster` singleton; FUSE-mounted (`weed mount`) at `/mnt/bedrock` on every node, so an ISO uploaded to `/mnt/bedrock/iso/` resolves identically cluster-wide. `seaweedfs.FILER_PORT`. |

## External

| Port | Service | Where | Notes |
|---|---|---|---|
| 12321 | UDP | BedRock Echo witness | LAN appliance / any 3rd host | ChaCha20-Poly1305 AEAD over msgpack (`witness.WITNESS_PORT`). Passive per-node K/V slot store used by the failover quorum. One slot per node, keyed by `node_id` = last octet of its `100.X.Y.N/32` loopback (1-250); the node holding the `.254` arbiter VIP publishes its `cluster`-singleton DRBD current-UUID marker into that same slot. `.254` is the arbiter VIP / rqlite node-id, not a witness slot. |

## Networks (mesh model)

Every NIC is a path candidate. bedrock-net discovers them, picks the best
per-peer at the kernel routing layer, and feeds the multi-path table to DRBD —
there is no fixed mgmt-vs-DRBD split. See `docs/06-mesh-network.md` for the full
design. The address spaces involved:

```
  Cluster identity (loopback /32, one per node)
     - 100.X.Y.<node_index>/32 on lo
     - X.Y derived deterministically from sha256(cluster_uuid),
       carved from RFC 6598 Shared Address Space (100.64.0.0/10).
       Operator LANs can't be in this range — IANA-reserved for
       ISP-to-CPE only.
     - Destination address for every cluster-internal protocol:
       DRBD (loopback-fallback path), libvirt migrate-uri, rqlite,
       SeaweedFS, SSH-from-scripts, inter-node mgmt API.
     - Never leaves the cluster; not routed past the mesh.

  Per-NIC link-local (one per up mesh NIC)
     - 169.254.X.Y/16, assigned by NetworkManager via
       bedrock-mesh-<nic> profiles (ipv4.method=link-local).
     - RFC 3927 ARP-probe + retry within each L2 segment. Cross-
       segment collisions handled by bedrock-net via ARP defense
       (see docs/06-mesh-network.md §cross-segment LL collision).
     - DRBD's direct path blocks list these addresses so DRBD does
       its own path-level failure detection.

  Operator LAN (br0)
     - Real LAN IP from operator DHCP, untouched by bedrock-net.
     - Carries dashboard HTTPS/WS (8443), SSH, Cockpit, VM bridges,
       all operator-facing traffic.

  Secondary planes (bedrock-mesh-*)
     - Direct cables or isolated bridges; treated identically by
       bedrock-net (one of many path candidates per peer pair).
```

## Why this works

- **Bandwidth**: a VM migrate picks the fastest direct link (USB4 / 10G / 2.5G)
  with no operator config; DRBD pushes replication across every direct link in
  parallel via its own multi-path.
- **Latency**: per-peer routes via the fastest link, kernel auto-failover on
  link-down at metric 10..14, panic-neighbour catch-all at metric 999.
- **Failure isolation**: any single cable or NIC can die; routing reconverges in
  seconds via metric-ordered host routes + the heartbeat-driven route delete on
  black-hole detection. The cluster keeps serving as long as any one path between
  any two peers remains (possibly via transit through a third node).

## Firewall policy

Bootstrap disables `firewalld` (`systemctl disable --now firewalld`): the LAN and
DRBD ring are physically operator-controlled, so the node runs without a host
firewall. A hardening allowlist for internet-exposed deployments would be:

- In from operator LAN: 22, 80, 8443, 9090
- In from any cluster peer (LAN): 9100, 9177, 4001, 4002, 9333, 8080, 8333, 8888
- In from any cluster peer (DRBD ring): 7700-7799, 49152-49215
- Block everything else.

## NetworkManager connections per node

```
  br0                 Linux bridge (primary NIC slaved via br0-<nic>)
                      ipv4.method = auto   (LAN DHCP)

  br0-<nic>           bridge-slave connection for the physical uplink

  bedrock-mesh-<nic>  ethernet connection on every non-bridge-slave NIC
                      ipv4.method = link-local
                      ipv6.method = ignore
                      connection.autoconnect = yes
                      (created on demand by bedrock-d's netd thread when
                      it first sees the NIC; NM does the RFC 3927 ARP
                      probe + claim)
```
