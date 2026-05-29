# Architecture

This is the high-level orientation. For load-bearing detail, see:
- **Witness + arbiter takeover:**
  [`cluster-quorum-spec.md`](cluster-quorum-spec.md) (passive
  AEAD K/V slot store on UDP/12321; exact-UUID takeover gate).
- **Storage stack:**
  [`storage-architecture.md`](storage-architecture.md)
  (LVM thinpool, per-resource thin meta LV, SeaweedFS topology).
- **Daemon unification:**
  [`daemon-unification.md`](daemon-unification.md) (single
  `bedrock-d` Python process — netd thread + mgmt/orchestrator asyncio).

Bedrock runs on every node. There is no external control plane; each node is
self-sufficient and can become the management node in a pinch. A node has
three roles, which can overlap:

- **compute** — runs VMs (KVM + DRBD)
- **mgmt** — runs the dashboard, metrics, logs, and cluster state
- **witness** — passive K/V slot store (BedRock Echo on an ESP32 or
  a tiny container on a MikroTik); UDP/12321, ChaCha20-Poly1305 AEAD. See
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
    │ FastAPI + Svelte (mgmt-dashboard) :8443 HTTPS    │            │
    │ Cockpit :9090                                    │            │
    └──────────────────────────────────────────────────┘            │
                          ▲                                         │
                          │ rqlite consensus (4001/4002)            │
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
        │ <═══════════════════════════════════ │            │   load_cluster()
        │   (push_log: WS first, VL second)      │  ┌─────────▼─────────┐
        │                                        │  │ rqlite (Raft):    │
        │   POST /api/vms/X/convert              │  │ nodes, vms,       │
        │ ─────────────────────────────────────> │  │ drbd_resources    │
        │                                        │  └───────────────────┘
        │                                        │
        │                                        │   ┌─ orchestrator ─┐
        │                                        │ ──│ saga / reactor │
        │                                        │   │ on each node:  │
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
go **through mgmt → rqlite write → orchestrator saga/reactor**. The canonical
(and only) cluster state store is **rqlite** (Raft-replicated SQLite, see
[`01-rqlite-state-store.md`](01-rqlite-state-store.md)); code reads topology
directly via `cluster_state.load_cluster()` (rqlite read-level `none`, so it
works even without quorum). There is no `cluster.json` cache file — it was
deleted 2026-05-26. Compute nodes are stateless orchestration targets that
observe rqlite changes and converge their local state accordingly; the only
per-node local cluster file is `/etc/bedrock/state.json` (this-node identity).

The earlier "bedrock-rust hash-chained log" model was retired in the
post-0.8-alpha rewrite, and `bedrock-rust` itself was deleted. Its
responsibilities (election, witness IO, self-demote on NoQuorum, the `.254`
arbiter, routing) now live in the **netd thread inside `bedrock-d`**. See
[`daemon-unification.md`](daemon-unification.md) and
[`cluster-quorum-spec.md`](cluster-quorum-spec.md).

## Components in one paragraph each

### mgmt dashboard (`mgmt/app.py`, ports 8443 HTTPS + 8001 loopback)

FastAPI server (run by `bedrock-d`) with an embedded WebSocket hub. It runs two
uvicorn listeners: **8443 HTTPS** (`0.0.0.0`) for the operator dashboard + LAN
mgmt API (operator-authenticated, Ed25519; see `installer/lib/operator_auth.py`),
and **127.0.0.1:8001 HTTP** for the local CLI / intra-process API (loopback is
auth-exempt). It serves the Svelte build, exposes a REST API for actions
(`/api/vms/{name}/{start,shutdown,migrate,convert}`, `/api/nodes/register`),
and pushes live state every 3s on the `cluster` channel plus instant log
events on the `event` channel. Proxies noVNC WebSockets at `/vnc/{vm}` to the
VM's host:VNC-port (see
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
`node`, `vm`. For VM lifecycle ops it is a thin HTTP client to the local mgmt
API on `127.0.0.1:8001`; install/join paths read `/etc/bedrock/state.json`
(this-node state) and call into `installer/lib/*.py` for the heavy lifting.
Fetched at install time from the install repo (the dev box or another serving
`installer/` over HTTP).

### witness (optional, UDP 12321)

**BedRock Echo** — a passive K/V slot store (one slot per node, keyed by the
node's loopback last octet; slot 254 = arbiter VIP). Each cluster node writes
its slot and reads peers' slots over UDP/12321 using ChaCha20-Poly1305 AEAD
(msgpack payload, shared `cluster.key`). The witness never decides anything; it
just holds state for the election + arbiter-takeout logic in `bedrock-d`'s netd
thread. Runs on an ESP32 or a tiny container on a MikroTik — not part of this
repo; configured as a witness host in rqlite. See
[`cluster-quorum-spec.md`](cluster-quorum-spec.md).

## Directory layout on a mgmt+compute node

```
/etc/bedrock/
    state.json            per-node identity, mgmt_url, loopback_ip, hardware
                          (the ONLY local cluster-related file; cluster
                          topology lives in rqlite, not on disk)
    cluster.key           32-byte shared key (HMAC-SHA256 for bedrock-net
                          probes/adverts; AEAD key for the Echo witness)
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
    bedrock-d.service          unified daemon (netd thread + mgmt/orchestrator
                               asyncio); starts the dashboard, VictoriaMetrics,
                               VictoriaLogs, and exporters
    bedrock-rqlited.service    per-node rqlite (consensus foundation)
    bedrock-rqlited-arbiter.service   arbiter rqlite voter on .254 (started on
                                      takeover)
    bedrock-weed-master.service       SeaweedFS master (Raft)
    bedrock-weed-volume.service       SeaweedFS volume (every node)
    bedrock-weed-filer.service        SeaweedFS filer (on .254, DRBD-backed)
    bedrock-weed-s3.service           SeaweedFS S3 gateway
    bedrock-mdns.service       mDNS responder
    bedrock-redirect.service   HTTP :80 → HTTPS :8443
    bedrock-cert-refresh.service      TLS cert renewal

/root/.ssh/
    id_ed25519, id_ed25519.pub   cluster identity
    authorized_keys              all cluster peers' pubkeys
    known_hosts                  pre-seeded at join time
    config                       StrictHostKeyChecking=accept-new for LAN/DRBD IPs
```

See [`reference/files.md`](reference/files.md) for the full list, including
files on compute-only nodes.

## The 10-second mental model

1. Every node runs `bedrock-d`, KVM + DRBD, and exporters.
2. Cluster truth lives in rqlite (Raft-replicated); the mgmt dashboard
   (active on whichever node holds `.254`) renders it.
3. The mgmt dashboard pushes state to browsers over WebSocket every 3 s,
   and pushes log events the instant they happen.
4. Operator actions (convert, migrate, etc.) write rqlite; the orchestrator
   saga/reactor converges each node. Compute nodes observe rqlite and act
   locally — they carry no operator-facing orchestration logic.
5. Data lives in DRBD, which replicates synchronously over the 100.X.Y
   ring. VMs pivot between nodes via `virsh migrate` without touching disk.
