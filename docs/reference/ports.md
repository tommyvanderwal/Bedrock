# Ports and networks

All ports Bedrock listens on, speaks to, or crosses between nodes.

## Host ports (every node)

| Port | Protocol | Service | Bound on | Notes |
|---|---|---|---|---|
| 22 | TCP | sshd | all IPs | Operator + inter-node orchestration + `qemu+ssh` migration |
| 9090 | TCP | Cockpit | all IPs | Optional per-node web console |
| 9100 | TCP | node_exporter | all IPs | Prometheus metrics, scraped by mgmt |
| 9177 | TCP | vm_exporter | all IPs | Bedrock-specific libvirt + DRBD metrics |
| 7000-7999 | TCP | DRBD replication | **DRBD NIC only** | Port = `7000 + minor`; one per resource |
| 5900-5999 | TCP | QEMU VNC | all IPs | One per running VM (display :0 → 5900, :1 → 5901, ...) |
| 49152-49215 | TCP | QEMU live-migrate | **DRBD NIC** via `--migrateuri` | libvirt's default migration port range |

## Mgmt-node additional ports

Only the node running `bedrock-mgmt.service` (init'd node, or a future HA mgmt):

| Port | Protocol | Service | Bound | Notes |
|---|---|---|---|---|
| 8080 | TCP | FastAPI mgmt dashboard | all IPs | HTTP + `/ws` WebSocket + `/vnc/{vm}` VNC proxy |
| 8428 | TCP | VictoriaMetrics | all IPs | `/api/v1/query`, `/api/v1/query_range`, `/-/reload` |
| 9428 | TCP | VictoriaLogs HTTP | all IPs | `/insert/jsonline` (push_log writes), `/select/logsql/query` (reads) |
| 5140 | TCP | VictoriaLogs syslog | all IPs | RFC 5424 syslog from cluster nodes (follow-up: auto-config per node) |
| 2049 | TCP | NFS server (ISO library) | all IPs | Exports `/opt/bedrock/iso` read-only to cluster LAN + DRBD ring; automounted on each compute node at `/mnt/isos`. |

## External

| Port | Service | Where | Notes |
|---|---|---|---|
| 9443 | bedrock-witness | MikroTik container / any 3rd host | `/health`, `/cluster-info`, `/register`, `/status`; used by failover quorum |

## Networks

Bedrock assumes two distinct networks per node, with br0 and eth1
separation:

```
  br0 (bridge, primary LAN)
     - 192.168.x.y / operator LAN
     - carries: SSH, dashboard HTTP/WS, metrics scrape, mgmt API, cockpit,
       VNC sessions (proxied via mgmt /vnc/…), LAN DHCP from router
     - in the testbed: bridged through KVM to the KPN router's DHCP
     - VMs inherit br0 via their libvirt <interface type='bridge'/>

  eth1 (or any secondary NIC)
     - 10.99.0.0/24 / DRBD ring
     - carries: DRBD replication (port 7000+minor), QEMU live-migrate
       traffic (migrateuri = tcp://<drbd_ip>)
     - physical lab: direct 2.5G ethernet cross-connect between two nodes,
       with a third cable for the 3-node ring
     - testbed: libvirt isolated network (no DHCP, static IPs from cloud-init)
     - ideal topology: dedicated VLAN or direct cable, no switch uplink
```

## Why two networks

- **Bandwidth separation**: a 10 GB VM migrate would saturate a 1 G mgmt
  LAN. DRBD writes from a busy database would do the same. The ring
  absorbs the heavy traffic; the LAN stays responsive.
- **Latency**: DRBD protocol C waits for peer ACK. 0.1 ms more latency
  on writes is perceptible; dedicated link keeps it deterministic.
- **Failure isolation**: a mgmt-LAN switch failure doesn't stop DRBD
  replication — data plane survives a partial network outage. Combined
  with the witness on a third domain, this lets the cluster keep
  serving even with degraded networking.

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

  bedrock-drbd                ethernet connection on eth1
                              ipv4.method = manual  addresses=10.99.0.X/24
                              (written by cloud-init in the testbed, by the
                              operator on physical hw)

  "Wired connection 1"        auto-created by NM on first boot before we could
                              inject bedrock-drbd → deleted in bootstrap
                              (cloud-init runcmd: `nmcli con delete ...`)
```

## Open issues / follow-ups

- Firewall allowlist script (not currently shipped).
- Per-node syslog → mgmt:5140 rsyslog config.
- Dashboard HA via floating VIP: needs a reserved IP in the LAN range
  that the mgmt role can assume on whichever node holds it.
