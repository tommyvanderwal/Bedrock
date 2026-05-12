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
| 7732 | UDP | bedrock-net probe (multicast) | all mesh NICs | **Protocol 1.** Signed multicast on `239.7.7.7`, TTL=1, every 1 s per up NIC. HMAC-SHA256-signed by cluster_key. Discovery only — carries no RTT data or routes. |
| ICMP | echo | bedrock-net latency | per-NIC link-local | **Protocol 2.** Unprivileged kernel ICMP (`SOCK_DGRAM/IPPROTO_ICMP`) every 2 s per `(peer, my_nic)`; kernel timestamps drive the EWMA. Needs `/proc/sys/net/ipv4/ping_group_range` to cover the daemon's gid. |
| 7733 | UDP | bedrock-net advertisement (unicast) | peer loopback `/32` | **Protocol 3.** Signed UDP unicast every 2 s per peer (NOT per link — kernel picks the NIC). Path-vector payload with `via_chain` for loop prevention. HMAC-SHA256-signed by cluster_key. |
| EtherType 0x88CC | L2 raw | bedrock-net LLDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET` listener on the LLDP multicast MAC `01:80:c2:00:00:0e`. Decoded into `/run/bedrock/switch_neighbors.json` + `NIC_SWITCH` journal lines. We never send LLDP frames. |
| SNAP 0x2000 | 802.3 LLC | bedrock-net CDP listener | all mesh + LAN NICs | **L2 discovery sidecar.** Receive-only `AF_PACKET/ETH_P_802_2` listener on `01:00:0c:cc:cc:cc`. Same processing path as LLDP. We never send CDP. |
| 5678 | UDP | bedrock-net MNDP listener | all IPs | **L2 discovery sidecar.** Receive-only UDP broadcast listener for MikroTik Neighbor Discovery Protocol. `IP_PKTINFO` for per-NIC attribution. Same processing path as LLDP / CDP. We never send MNDP. |
| 8200 | TCP | bedrock-rust peer link | per-NIC mesh paths | Cluster-protocol log replication. Each follower dials the master here. |
| 5900-5999 | TCP | QEMU VNC | all IPs | One per running VM (display :0 → 5900, :1 → 5901, ...) |
| 49152-49215 | TCP | QEMU live-migrate | per-NIC mesh paths via peer's `loopback_ip` | libvirt's default migration port range; the kernel route to the peer's `/32` picks the best NIC. |

## Mgmt-node additional ports

Only the node running `bedrock-mgmt.service` (init'd node, or a future HA mgmt):

| Port | Protocol | Service | Bound | Notes |
|---|---|---|---|---|
| 8080 | TCP | FastAPI mgmt dashboard | all IPs | HTTP + `/ws` WebSocket + `/vnc/{vm}` VNC proxy. `/api/topology` returns the physical-topology rollup (per-node `switch_neighbors.json` grouped by chassis ID; cached at `/run/bedrock/physical_topology.json`). |
| 8428 | TCP | VictoriaMetrics | all IPs | `/api/v1/query`, `/api/v1/query_range`, `/-/reload` |
| 9428 | TCP | VictoriaLogs HTTP | all IPs | `/insert/jsonline` (push_log writes), `/select/logsql/query` (reads) |
| 5140 | TCP | VictoriaLogs syslog | all IPs | RFC 5424 syslog from cluster nodes (follow-up: auto-config per node) |
| 2049 | TCP | NFS server (ISO library) | all IPs | Exports `/opt/bedrock/iso` read-only to cluster LAN + DRBD ring; automounted on each compute node at `/mnt/isos`. |

## External

| Port | Service | Where | Notes |
|---|---|---|---|
| 9443 | bedrock-witness | MikroTik container / any 3rd host | `/health`, `/cluster-info`, `/register`, `/status`; used by failover quorum |

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
       migrate-uri, NFS, SSH-from-scripts, dashboard inter-node).
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
     - Carries dashboard HTTP/WS, SSH, Cockpit, VM bridges, all
       operator-facing traffic.

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

- In from operator LAN: 22, 8080 (if mgmt), 9090 (Cockpit)
- In from any cluster peer (LAN): 9100, 9177
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
                              (created on-demand by bedrock-net.service when
                              it first sees the NIC; NM does the RFC 3927
                              ARP probe + claim)
```

## Open issues / follow-ups

- Firewall allowlist script (not currently shipped).
- Per-node syslog → mgmt:5140 rsyslog config.
- Dashboard HA via floating VIP: needs a reserved IP in the LAN range
  that the mgmt role can assume on whichever node holds it.
