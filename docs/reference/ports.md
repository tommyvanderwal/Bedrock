# Ports and networks

All ports Bedrock listens on, speaks to, or crosses between nodes.

## Host ports (every node)

| Port | Protocol | Service | Bound on | Notes |
|---|---|---|---|---|
| 22 | TCP | sshd | all IPs | Operator + inter-node orchestration + `qemu+ssh` migration |
| 9090 | TCP | Cockpit | all IPs | Optional per-node web console |
| 9100 | TCP | node_exporter | all IPs | Prometheus metrics, scraped by mgmt |
| 9177 | TCP | vm_exporter | all IPs | Bedrock-specific libvirt + DRBD metrics |
| 7000-7999 | TCP | DRBD replication | per-NIC mesh paths | Port = `7000 + minor`; multi-path via mesh layer — DRBD opens one path per direct `(nic_a, nic_b)` pair the bedrock-net path table observed, plus a loopback fallback as last resort. See `docs/06-mesh-network.md`. |
| 7732 | UDP | bedrock-net probe (multicast) | all mesh NICs | **Protocol 1.** Signed multicast on `239.7.7.7`, TTL=1, every 1 s per up NIC. HMAC-SHA256-signed by cluster_key. Discovery only — carries no RTT data or routes. `netd.PROBE_PORT`. |
| ICMP | echo | bedrock-net latency | per-NIC link-local | **Protocol 2.** Unprivileged kernel ICMP (`SOCK_DGRAM/IPPROTO_ICMP`) every 2 s per `(peer, my_nic)`; kernel timestamps drive the EWMA. Needs `/proc/sys/net/ipv4/ping_group_range` to cover the daemon's gid. |
| 7733 | UDP | bedrock-net advertisement (unicast) | peer loopback `/32` | **Protocol 3.** Signed UDP unicast every 2 s per peer (NOT per link — kernel picks the NIC). Path-vector payload with `via_chain` for loop prevention. HMAC-SHA256-signed by cluster_key. `netd.ADV_PORT`. |
| 7734 | UDP | bedrock-net heartbeat | mesh | **Protocol 4.** Drives `MASTER_LOSS_MISSES` / black-hole route deletion. `netd.HB_PORT`. |
| EtherType 0x88CC | L2 raw | bedrock-net LLDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET` listener on the LLDP multicast MAC `01:80:c2:00:00:0e`. Decoded into `/run/bedrock/switch_neighbors.json` + `NIC_SWITCH` journal lines. We never send LLDP frames. |
| SNAP 0x2000 | 802.3 LLC | bedrock-net CDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET/ETH_P_802_2` listener on `01:00:0c:cc:cc:cc`. Same processing path as LLDP. We never send CDP. |
| 5678 | UDP | bedrock-net MNDP listener | all IPs | **L2 discovery sidecar.** Receive-only UDP broadcast listener for MikroTik Neighbor Discovery Protocol. `IP_PKTINFO` for per-NIC attribution. Same processing path as LLDP / CDP. We never send MNDP. |
| 4001 | TCP HTTPS (mTLS) | rqlite HTTP API | per-node `0.0.0.0` | Raft-replicated SQLite — the cluster's source of truth. mTLS via `node.crt` / `node.key.pem` / `ca.crt`. Local code dials `127.0.0.1:4001`. |
| 4002 | TCP | rqlite Raft | per-node | Raft consensus transport between rqlited peers. |
| 9333 | TCP | SeaweedFS master | Raft on the `min(3,N)` lowest-octet nodes | Volume/collection metadata + master Raft (`MASTER_PORT`). |
| 8080 | TCP | SeaweedFS volume | every node `0.0.0.0` | Blob storage. **This is the weed volume, NOT the dashboard** (`VOLUME_PORT`). |
| 8333 | TCP | SeaweedFS S3 gateway | every node `0.0.0.0` | S3 API external clients connect to (`S3_PORT`). |
| 5900-5999 | TCP | QEMU VNC | all IPs | One per running VM (display :0 → 5900, :1 → 5901, ...) |
| 49152-49215 | TCP | QEMU live-migrate | per-NIC mesh paths via peer's `loopback_ip` | libvirt's default migration port range; the kernel route to the peer's `/32` picks the best NIC. |

## Control-plane and singleton-role ports

The mgmt dashboard + API run inside `bedrock-d` on **every** node (there is no
separate `bedrock-mgmt.service`). The filer and arbiter rqlite are singleton
roles that live on whichever node holds the `.254` arbiter VIP.

| Port | Protocol | Service | Bound | Notes |
|---|---|---|---|---|
| 8443 | TCP HTTPS | FastAPI mgmt dashboard + LAN API | `0.0.0.0` (every node) | Operator-authenticated. `/ws` WebSocket + `/vnc/{vm}` VNC proxy. `/api/topology` returns the physical-topology rollup (per-node `switch_neighbors.json` grouped by chassis ID; cached at `/run/bedrock/physical_topology.json`). `bedrock-d` serves on `0.0.0.0:8444` pre-cert, then flips to 8443 once the TLS cert exists. |
| 8001 | TCP HTTP | local CLI / intra-process mgmt API | `127.0.0.1` (every node) | The `bedrock` CLI dials this. **Auth-exempt** — loopback is trusted local root; LAN requests on 8443 still require an operator token. |
| 80 | TCP | `bedrock-redirect` | `0.0.0.0` (every node) | 302 → `https://<host>:8443/`. |
| 4011 | TCP HTTPS (mTLS) | rqlite-arbiter HTTP API | `.254` VIP | Arbiter rqlite voter co-located with the `.254` arbiter; chosen so it coexists with the per-node rqlited on 4001. |
| 4012 | TCP | rqlite-arbiter Raft | `.254` VIP | Arbiter Raft transport. |
| 8428 | TCP | VictoriaMetrics backend | the 2 metrics backends | `/api/v1/query`, `/api/v1/query_range`, `/-/reload`. Only on `obs_backends.metrics` nodes; agents elsewhere dual-write here via `bedrock-vmagent`. |
| 9428 | TCP | VictoriaLogs backend HTTP | the 2 logs backends | `/insert/jsonline` (push_log writes), `/select/logsql/query` (reads). Only on `obs_backends.logs` nodes; `bedrock-vlagent` elsewhere dual-writes here. |
| 5140 | TCP | VictoriaLogs **agent** syslog (`bedrock-vlagent`) | every node | RFC 5424 syslog ingest; the local node's vlagent listens here and forwards to both VL backends. |
| 5141 | TCP | VictoriaLogs **backend** syslog (`bedrock-vl`) | the 2 logs backends | Backend-side syslog listener (`_vl_unit`), distinct from the agent's 5140. |
| 8888 | TCP | SeaweedFS filer (HTTP + ISO library namespace) | `.254` VIP | POSIX filer namespace, DRBD-backed on the `cluster` singleton; FUSE-mounted at `/mnt/bedrock` on every node so uploaded ISOs at `/mnt/bedrock/iso/` resolve identically cluster-wide. Replaced the old NFS-on-2049 export of `/opt/bedrock/iso`. |

## External

| Port | Service | Where | Notes |
|---|---|---|---|
| 12321 | UDP | BedRock Echo witness | LAN appliance / any 3rd host | ChaCha20-Poly1305 AEAD over msgpack. Passive per-node K/V slot store used by the failover quorum (`witness.WITNESS_PORT`). One slot per node, keyed by the node's own `node_id` = last octet of its `100.X.Y.N/32` loopback (1–250); the node currently holding the `.254` arbiter VIP publishes its DRBD current-UUID marker into that same slot. (`.254` is the arbiter's VIP / rqlite node-id, not a witness slot. The old HTTP `:9443` witness path in `discovery.py` is dead legacy.) |

## Networks (mesh model)

Bedrock no longer presumes a fixed mgmt-vs-DRBD network split.
Every NIC is a path candidate; bedrock-net discovers them, picks
the best per-peer at the kernel routing layer, and feeds the
multi-path table to DRBD. See `docs/06-mesh-network.md` for the
full design. Summary of the address spaces involved:

```
  Cluster identity (loopback /32, one per node)
     - 100.X.Y.<node_index>/32 on lo
     - X.Y derived deterministically from sha256(cluster_uuid),
       carved from RFC 6598 Shared Address Space (100.64.0.0/10).
       Operator LANs can't be in this range — IANA-reserved for
       ISP-to-CPE only.
     - Used as the destination address by every cluster-internal
       protocol (DRBD via path-block loopback fallback, libvirt
       migrate-uri, rqlite, SeaweedFS, SSH-from-scripts, inter-node
       mgmt API).
     - Never leaves the cluster; not routed past the mesh.

  Per-NIC link-local (one per up mesh NIC)
     - 169.254.X.Y/16, assigned by NetworkManager via
       bedrock-mesh-<nic> profiles (ipv4.method=link-local).
     - RFC 3927 ARP-probe + retry within each L2 segment. Cross-
       segment collisions handled by bedrock-net via ARP defense
       (see docs/06-mesh-network.md §cross-segment LL collision).
     - DRBD's per-link path blocks list these addresses so DRBD
       can do its own path-level failure detection.

  Operator LAN (br0)
     - Real LAN IP from operator's DHCP, untouched by bedrock-net.
     - Carries dashboard HTTPS/WS (8443), SSH, Cockpit, VM bridges,
       all operator-facing traffic.

  Optional secondary planes (bedrock-drbd, bedrock-mesh-*)
     - Direct cables or isolated bridges; treated identically by
       bedrock-net (one of many path candidates per peer pair).
```

## Why this works

- **Bandwidth**: a 10 GB VM migrate picks the fastest direct link
  (USB4 / 10G / 2.5G) without any operator config; DRBD pushes
  replication across every direct link in parallel via its own
  multi-path.
- **Latency**: per-peer routes via fastest link, kernel auto-
  failover on link-down at metric 10..14, panic-neighbour route
  at metric 999 as catch-all.
- **Failure isolation**: any single cable or NIC can die; routing
  reconverges in seconds via metric-ordered host routes + the
  bedrock-net heartbeat-driven route delete on black-hole
  detection. Cluster keeps serving as long as any one path between
  any two peers remains (possibly via transit through a third node).

## Firewall policy

Bootstrap disables `firewalld` entirely (`systemctl disable --now
firewalld`). The rationale is operator trust of the LAN and DRBD ring
being physically controlled; adding firewall rules that permit exactly
the ports above is a hardening follow-up.

On a node you'd harden for internet-exposed ops (not the current
Bedrock target environment), the allowlist would be:

- In from operator LAN: 22, 80, 8443, 9090 (Cockpit)
- In from any cluster peer (LAN): 9100, 9177, 4001, 4002, 9333, 8080, 8333, 8888
- In from any cluster peer (DRBD ring): 7000-7999, 49152-49215
- Block everything else.

## NetworkManager connections per node

```
  br0                         Linux bridge  (primary NIC slaved to it via br0-<nic>)
                              ipv4.method = auto   (LAN DHCP)

  br0-<nic>                   bridge-slave connection for the physical uplink

  bedrock-mesh-<nic>          ethernet connection on every non-bridge-slave NIC
                              ipv4.method = link-local
                              ipv6.method = ignore
                              connection.autoconnect = yes
                              (created on-demand by bedrock-d's netd thread
                              when it first sees the NIC; NM does the RFC 3927
                              ARP probe + claim)
```

## Open issues / follow-ups

- Firewall allowlist script (not currently shipped).
- Per-node syslog → VL:5140 rsyslog config.
