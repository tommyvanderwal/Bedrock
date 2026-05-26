# Architecture

> **⚠ Sections of this doc are out of date.** Specifically, the
> witness model described here (HTTPS on 9443, HMAC, active
> blessing) and the `bedrock-rust` references are superseded.
> Current authoritative sources:
> - **Witness + arbiter takeover:**
>   [`cluster-quorum-spec.md`](cluster-quorum-spec.md) (passive
>   AEAD K/V slot store on UDP/12321; exact-UUID takeover gate).
> - **Storage stack:**
>   [`storage-architecture.md`](storage-architecture.md)
>   (three thinpools, per-resource thin meta LV, SeaweedFS
>   topology, three collections).
> - **Daemon unification:**
>   [`daemon-unification.md`](daemon-unification.md) (single
>   `bedrock-d` Python process; no separate bedrock-rust /
>   bedrock-netd / bedrock-mgmtd / bedrock-orchd).
>
> The system layout, ports, and data-flow sections below are
> still useful as a high-level orientation; trust the linked docs
> for the load-bearing details.

Bedrock runs on every node. There is no external control plane; each node is
self-sufficient and can become the management node in a pinch. A node has
three roles, which can overlap:

- **compute** — runs VMs (KVM + DRBD)
- **mgmt** — runs the dashboard, metrics, logs, and cluster state
- **witness** — passive K/V slot store (bedrock-echo on ESP32 or
  a tiny container on a MikroTik); UDP/12321, AEAD-encrypted. See
  [`cluster-quorum-spec.md`](cluster-quorum-spec.md).

A 3-node cluster typically looks like this:

```
                          ┌───────── LAN (192.168.2.0/24) ──────────┐
                          │                                         │
    ┌────────── node1 (mgmt+compute, default) ─────────┐            │
    │                                                  │            │
    │ KVM + libvirtd                                   │            │
    │ DRBD 9.3                                         │ br0        │
    │ node_exporter :9100   vm_exporter :9177          ├────────────┤
    │ VictoriaMetrics :8428  VictoriaLogs :9428        │            │
    │ FastAPI + Svelte (mgmt-dashboard) :8080          │            │
    │ Cockpit :9090                                    │            │
    └──────────────────────────────────────────────────┘            │
                          ▲                                         │
                          │ SSH (cluster.json)                      │
                          │ Prometheus scrape (9100/9177)           │
                          │ VictoriaLogs syslog :5140               │
                          │                                         │
    ┌────────── node2 (compute) ─────────┐                          │
    │ KVM + libvirtd  DRBD 9.3           ├──────── br0 ─────────────┤
    │ node_exporter :9100                │                          │
    │ vm_exporter :9177  Cockpit :9090   │                          │
    └────────────────────────────────────┘                          │
                          │                                         │
    ┌────────── node3 (compute) ─────────┐                          │
    │ same as node2                      ├──────── br0 ─────────────┘
    └────────────────────────────────────┘
                          │
                          │   ═══ mesh underlay (bedrock-net) ═══
                          │   per-cluster /24 in 100.64.0.0/10 +
                          │   per-NIC 169.254.x.y link-local
                          │
                ┌─────────┴──────────┐
         node1 ═╪════════════════════╪═ node2
         100.X.Y.1                    100.X.Y.2
                          ╪
                       node3
                      100.X.Y.3
```

**Cluster identity** — each node has one stable `/32` on `lo` derived
from `cluster_uuid` (per-cluster `/24` carved deterministically from
RFC 6598 Shared Address Space, `100.64.0.0/10`). All cluster-internal
traffic targets that `/32`; the kernel routes it through whichever
physical NIC is best per the bedrock-net path table.

**Per-NIC reachability** — every directly-attached interface gets a
`169.254.x.y` link-local via NetworkManager (RFC 3927). DRBD's
multi-path config lists each direct-link address pair as a separate
`path` block; a loopback fallback path catches everything if every
direct link fails. The mgmt LAN (br0, 192.168.2.x) keeps its DHCP
address and carries operator-facing traffic — dashboard HTTP, SSH,
metrics, VM bridges — bedrock-net just observes it as one of many
path candidates per peer pair.

See [`06-mesh-network.md`](06-mesh-network.md) for the full mesh-layer
design, RFC choices, cross-segment collision handling, and
verification commands.

## Workload types

```
 ┌─────────┬──────────────────┬───────────────┬────────────────────────┐
 │ Type    │ Replicas         │ Min nodes     │ Semantics              │
 ├─────────┼──────────────────┼───────────────┼────────────────────────┤
 │ cattle  │ 1 local LV       │ 1             │ No DRBD, no migrate    │
 │ pet     │ 2-way DRBD       │ 2             │ Live migrate, failover │
 │ ViPet   │ 3-way DRBD       │ 3             │ Pet that keeps 2 live  │
 │         │ (full mesh)      │               │ copies during outage   │
 └─────────┴──────────────────┴───────────────┴────────────────────────┘
```

A VM can be promoted/demoted online in either direction; see
[`actions/vm-convert.md`](actions/vm-convert.md).

## Data plane — how a VM's disk is stored

```
cattle:                 pet (2-way):                  ViPet (3-way):

  node1                  node1 (P)       node2 (S)      node1 (P)   node2 (S)   node3 (S)
  ┌───────────┐          ┌───────────┐   ┌───────────┐  ┌───────────┐ ┌───────────┐ ┌───────────┐
  │ thin LV   │          │ DRBD 1000 │═══│ DRBD 1000 │  │ DRBD 1000 │═│ DRBD 1000 │═│ DRBD 1000 │
  │ (raw)     │          └─────┬─────┘   └─────┬─────┘  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
  └───────────┘                │               │              │             │             │
                            thin LV         thin LV       thin LV      thin LV      thin LV
                            + meta LV       + meta LV     + meta LV    + meta LV    + meta LV
```

**P** = DRBD Primary (VM runs here), **S** = Secondary.
`meta LV` is a per-resource thin LV holding DRBD external metadata,
sized to the data volume + `max-peers=7`. See
[`storage-architecture.md`](storage-architecture.md) for the
LV-pair layout and growth semantics.

## Control plane — how state flows

```
Operator (browser)                       mgmt node (node1)
        │                                        │
        │   HTTP GET /                           │
        │ ─────────────────────────────────────> │  Svelte bundle
        │   WS /ws                               │  ┌───────────────────────┐
        │ ────────────────────────────────────── │  │ state_push_loop (3s)  │
        │   ws.on('cluster', ...)                │  │   build_cluster_state │
        │ <═══════════════════════════════════ │  │   ThreadPoolExecutor  │
        │   (json: nodes, vms, witness)        │  │   fan-out SSH to all  │
        │                                        │  │   nodes + all VMs     │
        │                                        │  │   (was ~3 s seq,      │
        │                                        │  │    now ~0.7 s)        │
        │                                        │  └─────────┬─────────────┘
        │   ws.on('event', ...)  ◀ instant       │            │
        │ <═══════════════════════════════════ │            │   SSH
        │   (push_log: WS first, VL second)      │  ┌─────────▼─────────┐
        │                                        │  │ /etc/bedrock/     │
        │   POST /api/vms/X/convert              │  │   cluster.json    │
        │ ─────────────────────────────────────> │  └───────────────────┘
        │                                        │
        │                                        │   ┌─ orchestrator ─┐
        │                                        │ ──│ SSH to each    │
        │                                        │   │ cluster node   │
        │                                        │   │ drbdadm,       │
        │                                        │   │ lvcreate,      │
        │                                        │   │ virsh ...      │
        │                                        │   └────────────────┘
        │   (200 OK + status JSON)               │
        │ <───────────────────────────────────── │
        │                                        │
        │                                        │   VictoriaLogs insert
        │                                        │   + WS 'event' broadcast
```

The operator never talks to compute nodes directly. All state-changing actions
go **through mgmt → rqlite write → orchestrator reactor**. The canonical
state store is **rqlite** (Raft-replicated SQLite, see
[`01-rqlite-state-store.md`](01-rqlite-state-store.md)); `/etc/bedrock/cluster.json`
is a regenerated cache projection of the rqlite tables and is
rewritten on every revision change. Compute nodes are stateless
orchestration targets that observe rqlite changes via the
`rqlite_subscriber` task and converge their local state accordingly.

The earlier "bedrock-rust hash-chained log" model was retired in the
post-0.8-alpha rewrite. bedrock-rust itself was subsequently
removed; its responsibilities (election, witness IO, self-demote on NoQuorum)
moved into the netd thread inside `bedrock-d`. See
[`daemon-unification.md`](daemon-unification.md) and
[`cluster-quorum-spec.md`](cluster-quorum-spec.md).

## Components in one paragraph each

### mgmt dashboard (`mgmt/app.py`, port 8080)

FastAPI server with an embedded WebSocket hub. Serves the Svelte build, exposes
a small REST API for actions (`/api/vms/{name}/{start,shutdown,migrate,convert}`,
`/api/nodes/register`), and pushes live state every 3s on the `cluster`
channel plus instant log events on the `event` channel. Proxies noVNC
WebSockets at `/vnc/{vm}` to the VM's host:VNC-port (see
[`components/mgmt-dashboard.md`](components/mgmt-dashboard.md)).

### VictoriaMetrics + VictoriaLogs (ports 8428 / 9428)

Metrics and logs backend. VM scrapes `{ip}:9100` (node_exporter) and
`{ip}:9177` (vm_exporter) across every node; scrape config is regenerated
by the mgmt app whenever a node registers and reloaded via HTTP `/-/reload`.
VL accepts `_time`-stamped JSON lines from `push_log()` and syslog from
cluster nodes on port 5140. Both live under `/opt/bedrock/data/`.

### node_exporter + vm_exporter (9100 / 9177)

`node_exporter` is stock Prometheus (CPU, memory, disk, network, load).
`vm_exporter` (`mgmt/vm_exporter.py`) is a ~100-line Python http.server that
parses `virsh domstats` + `drbdadm status` and emits text-format
`libvirt_*` and `drbd_*` metrics. Deployed via `installer/lib/exporters.py`
on every node at `bedrock init` / `bedrock join`.

### DRBD 9.3 (`kmod-drbd9x` from ELRepo)

Block-level replication. Bedrock provisions resources with **external**
meta-disks (so `/dev/drbdN` matches the data LV size byte-for-byte) and
`--max-peers=7` (so peers can be added later without re-creating metadata).
Resources are named `vm-<name>-disk0`. Minor numbers start at 1000.
Ports = `7000 + minor`. See [`storage-architecture.md`](storage-architecture.md).

### bedrock CLI (`installer/bedrock`)

Entry point on each node. Subcommands: `bootstrap`, `init`, `join`, `status`,
`node`, `vm`. Reads `/etc/bedrock/state.json` (this-node state) and calls
into `installer/lib/*.py` for the heavy lifting. Fetched at install time from
the install repo (the dev box or another serving `installer/` over HTTP).

### witness (optional, port 9443)

Tiny Rust container (on MikroTik or any small box) that 2-of-3 quorum
logic on each cluster node polls for liveness. Not part of this repo —
referenced as an external host in `cluster.json`.

## Directory layout on a mgmt+compute node

```
/etc/bedrock/
    state.json            per-node identity, mgmt_url, loopback_ip, hardware
    cluster.json          cluster topology — nodes (with loopback_ip),
                          witnesses, tiers, paths (mesh path table)
    cluster.key           32-byte HMAC key (bedrock-net probes + Echo witness)
    daemon.toml           bedrock-rust config; regenerated on every snapshot
                          change by mgmt/orchestrator's subscriber
    installer.env         BEDROCK_REPO=... (used by bedrock CLI subcommands)

/opt/bedrock/
    bin/
        victoria-metrics
        victoria-logs
        node_exporter
        vm_exporter.py
    data/
        vm/               VictoriaMetrics storage
        vl/               VictoriaLogs storage
    mgmt/                 full mgmt app (extracted from mgmt.tar.gz)
        app.py
        ws.py  victoria.py  vm_exporter.py
        novnc/            static HTML/JS for browser VNC
        ui/build/         Svelte production bundle
    scrape.yml            VM scrape config (regenerated on register)

/etc/drbd.d/
    global_common.conf
    vm-<name>-disk0.res   per-VM resource, written by mgmt during convert

/etc/systemd/system/
    bedrock-mgmt.service    FastAPI dashboard
    bedrock-vm.service      VictoriaMetrics
    bedrock-vl.service      VictoriaLogs
    node-exporter.service
    vm-exporter.service

/root/.ssh/
    id_ed25519, id_ed25519.pub   cluster identity
    authorized_keys              all cluster peers' pubkeys
    known_hosts                  pre-seeded at join time
    config                       StrictHostKeyChecking=accept-new for LAN/DRBD IPs
```

See [`reference/files.md`](reference/files.md) for the full list, including
files on compute-only nodes.

## The 10-second mental model

1. Every node runs KVM + DRBD + exporters.
2. One node additionally runs the mgmt dashboard, which is the single
   source of cluster truth.
3. The mgmt dashboard pushes state to browsers over WebSocket every 3 s,
   and pushes log events the instant they happen.
4. Operator actions (convert, migrate, etc.) are orchestrated by the mgmt
   node fanning out via SSH. Compute nodes carry no orchestration logic.
5. Data lives in DRBD, which replicates synchronously over the 100.X.Y
   ring. VMs pivot between nodes via `virsh migrate` without touching disk.
