# Architecture

High-level orientation. For load-bearing detail, see:
- **Witness + arbiter takeover:** [`cluster-quorum-spec.md`](cluster-quorum-spec.md)
  (passive AEAD K/V slot store on UDP/12321; exact-UUID takeover gate).
- **Storage stack:** [`storage-architecture.md`](storage-architecture.md)
  (LVM thinpool, per-resource thin meta LV, SeaweedFS topology).
- **Unified daemon:** [`daemon-unification.md`](daemon-unification.md)
  (single `bedrock-d` Python process — netd thread + mgmt/orchestrator asyncio).

Bedrock runs on every node. There is no external control plane; each node is
self-sufficient and any node can hold the management role. A node has three
roles, which overlap:

- **compute** — runs VMs (KVM + DRBD)
- **mgmt** — runs the dashboard, metrics, logs, and cluster state
- **witness** — passive K/V slot store (BedRock Echo on an ESP32 or a tiny
  container on a MikroTik), UDP/12321, ChaCha20-Poly1305 AEAD. See
  [`cluster-quorum-spec.md`](cluster-quorum-spec.md).

A 3-node cluster typically looks like this:

```
                          ┌───────── LAN (192.168.2.0/24) ──────────┐
                          │                                         │
    ┌────────── node1 (mgmt+compute, holds .254) ──────┐            │
    │                                                  │            │
    │ KVM + libvirtd                                   │            │
    │ DRBD 9.3                                         │ br0        │
    │ node_exporter :9100   vm_exporter :9177          ├────────────┤
    │ VictoriaMetrics :8428  VictoriaLogs :9428        │            │
    │ FastAPI + Svelte (mgmt-dashboard) :8443 HTTPS    │            │
    │ Cockpit :9090                                    │            │
    └──────────────────────────────────────────────────┘            │
                          ▲                                         │
                          │ rqlite consensus (4001/4002, mTLS)      │
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

**Cluster identity** — each node owns one stable `/32` on `lo`, derived from
`cluster_uuid` (per-cluster `/24` carved deterministically from RFC 6598 Shared
Address Space, `100.64.0.0/10`). All cluster-internal traffic targets that
`/32`; the kernel routes it through whichever physical NIC is best per the
bedrock-net path table. The address lives on `lo`, not a NIC, so a NIC change
never moves it.

**Per-NIC reachability** — every directly-attached interface gets a
`169.254.x.y` link-local (RFC 3927, MAC-derived). DRBD's multi-path config lists
each direct-link address pair as a separate `path` block; a loopback-fallback
path catches everything if every direct link fails. The mgmt LAN (br0,
192.168.2.x) keeps its DHCP address and carries operator-facing traffic
(dashboard, SSH, metrics, VM bridges); bedrock-net observes it as one of many
path candidates per peer pair.

See [`06-mesh-network.md`](06-mesh-network.md) for the full mesh-layer design,
RFC choices, cross-segment collision handling, and verification commands.

## Workload types

```
 ┌─────────┬──────────────────┬───────────────┬────────────────────────┐
 │ Type    │ Replicas         │ Min nodes     │ Semantics              │
 ├─────────┼──────────────────┼───────────────┼────────────────────────┤
 │ cattle  │ 1 local LV       │ 1             │ No DRBD, no migrate    │
 │ pet     │ 2-way DRBD       │ 2             │ Live migrate, failover │
 │ vipet   │ 3-way DRBD       │ 3             │ Pet that keeps 2 live  │
 │         │ (full mesh)      │               │ copies during outage   │
 └─────────┴──────────────────┴───────────────┴────────────────────────┘
```

A VM is promoted/demoted online in either direction; see
[`actions/vm-convert.md`](actions/vm-convert.md).

## Data plane — how a VM's disk is stored

```
cattle:                 pet (2-way):                  vipet (3-way):

  node1                  node1 (P)       node2 (S)      node1 (P)   node2 (S)   node3 (S)
  ┌───────────┐          ┌───────────┐   ┌───────────┐  ┌───────────┐ ┌───────────┐ ┌───────────┐
  │ thin LV   │          │ DRBD 1102 │═══│ DRBD 1102 │  │ DRBD 1102 │═│ DRBD 1102 │═│ DRBD 1102 │
  │ (raw)     │          └─────┬─────┘   └─────┬─────┘  └─────┬─────┘ └─────┬─────┘ └─────┬─────┘
  └───────────┘                │               │              │             │             │
                            data LV         data LV       data LV      data LV      data LV
                            + meta LV       + meta LV     + meta LV    + meta LV    + meta LV
```

**P** = DRBD Primary (VM runs here), **S** = Secondary. Each resource uses two
thin LVs: `bedrock-data-<r>` and an external-metadata `bedrock-meta-<r>` sized
for `--max-peers=7`. External metadata keeps `/dev/drbdN` byte-for-byte the size
of the data LV, so a local LV promotes to DRBD-replicated with zero copy.
See [`storage-architecture.md`](storage-architecture.md) for LV-pair layout and
growth semantics.

## Control plane — how state flows

```
Operator (browser)                       mgmt node (holds .254)
        │                                        │
        │   HTTP GET /                           │
        │ ─────────────────────────────────────> │  Svelte bundle
        │   WS /ws                               │  ┌───────────────────────┐
        │ ────────────────────────────────────── │  │ state_push_loop (3s)  │
        │   ws.on('cluster', ...)                │  │   ThreadPool SSH       │
        │ <═══════════════════════════════════ │  │   fan-out to all nodes │
        │   (json: nodes, vms, witness)        │  │   + all VMs            │
        │                                        │  └─────────┬─────────────┘
        │   ws.on('event', ...)  ◀ instant       │            │  load_cluster()
        │ <═══════════════════════════════════ │  ┌─────────▼─────────┐
        │   (push_log: WS first, VL second)      │  │ rqlite (Raft):    │
        │                                        │  │ nodes, vms,       │
        │   POST /api/vms/X/convert              │  │ drbd_resources …  │
        │ ─────────────────────────────────────> │  └───────────────────┘
        │                                        │
        │                                        │   ┌─ orchestrator ─┐
        │                                        │ ──│ saga / reactor │
        │                                        │   │ on each node:  │
        │                                        │   │ drbdadm,       │
        │                                        │   │ lvcreate,      │
        │                                        │   │ virsh …        │
        │                                        │   └────────────────┘
        │   (200 OK + status JSON)               │
        │ <───────────────────────────────────── │
        │                                        │   VictoriaLogs insert
        │                                        │   + WS 'event' broadcast
```

The operator never talks to compute nodes directly. All state-changing actions
go **through mgmt → rqlite write → orchestrator saga/reactor**. The canonical
cluster-state store is **rqlite** (Raft-replicated SQLite, see
[`01-rqlite-state-store.md`](01-rqlite-state-store.md)); code reads topology
directly via `cluster_state.load_cluster()` at rqlite read-level `none`, so it
works even without quorum (every node holds a full Raft-replicated copy). The
no-quorum recovery path re-reads at level `strong` so it never decides against a
stale snapshot. Compute nodes are stateless orchestration targets: they observe
rqlite changes and converge their local state. The only per-node local cluster
files are `/etc/bedrock/state.json` (this node's identity + cold-boot recovery
fields) and `/etc/bedrock/cluster.json` (a bootstrap-only rqlite peer list,
read by `rqlite_setup --render-env` at every boot because rqlite can't report
its own peers before it starts).

Election, witness I/O, self-demote on NoQuorum, the `.254` arbiter, and routing
all live in the **netd thread inside `bedrock-d`**. See
[`daemon-unification.md`](daemon-unification.md) and
[`cluster-quorum-spec.md`](cluster-quorum-spec.md).

## Consensus and failover

The netd thread runs a 1 s election tick over observable cluster state — no
rqlite dependency, since this is what *recovers* rqlite.

- **Vote weights:** node = 100, each valid+confirmed witness = 1.
  `total = 100·active_nodes + configured_witnesses`; `majority = total//2 + 1`;
  `my_votes = 100·(self + ACKing peers) + valid_witnesses`. A witness counts
  toward `my_votes` only when reachable AND reflecting our own slot write;
  otherwise it adds 0 to the numerator but still counts in the denominator,
  raising the bar and biasing toward "don't fail over" (safety over
  availability). 100/1 means witnesses break an exact node-tie but never
  overrule a node.
- **Timing:** a survivor promotes at `MASTER_LOSS_MISSES = 10` (~10 s); an
  isolated master self-demotes at `SELF_DEMOTE_MISSES = 9` (~9 s, one tick
  earlier, so `.254` is never on two nodes at once).
- **Promotion tiebreak:** among reachable acked contenders, the
  lowest-loopback-octet candidate promotes; the rest defer and ack it.

The witness (**BedRock Echo**) is a passive per-node slot store on UDP 12321,
ChaCha20-Poly1305 AEAD over msgpack. One slot per node (key = the node's
loopback last octet, 254 = arbiter VIP). Slots older than `SLOT_STALE_MS =
10000` are stale; the last-man-standing (LMS) bit never times out. Arbiter
takeover uses only the witness plus local commands (`drbdadm`, `ip`, `mount`,
`systemctl`) — no rqlite on that path, because rqlite is the service being
recovered. The takeover UUID match against the candidate's advertised
`cluster`-DRBD current-UUID is exact.

## Storage model

One LVM thinpool per node. The **cluster singleton** (DRBD resource and tier
named `cluster`, DRBD minor 1101, mounted at `/var/lib/bedrock/cluster`) holds
the arbiter rqlite data, the SeaweedFS filer's leveldb3, and the S3 IAM
database — one DRBD handoff moves them all together. Its replica set is capped
at `min(3, N)` (lowest-octet nodes). DRBD tuning: `resync-rate 100M`,
`c-min-rate 0`, `c-plan-ahead 0`.

Per-VM disks (resource `vm-<name>-disk0`, minors 1102+): cattle = one local thin
LV (no DRBD, no migrate); pet = 2-way DRBD; vipet = 3-way DRBD. SeaweedFS volume
bytes live on a large local thin LV (`bedrock-weed-volume`, no DRBD — SeaweedFS
replicates via its collections scratch=000 / standard=001 / critical=002).

## Pet/vipet failover

`bedrock_d/orchestrator/vm_failover.py` runs three tasks on a 5 s cadence on a
node that has lost quorum:

1. **Suspend** — local pet/vipet VMs are virsh-suspended ~20 s after partition
   (cattle are left alone — local LV, no failover target).
2. **Takeover** — for a peer whose heartbeat is ≥35 s stale, the next-in-line
   node by `vms.failover_order` runs: drbd disconnect → primary → record UUID →
   strong-read safety check → start → update `vms.host`.
3. **Kill** — a VM still down 5 minutes after **quorum loss** (the clock starts
   at quorum loss, not at suspend) is destroyed.

On quorum return, a still-suspended VM is resumed.

## Components in one line each

### mgmt dashboard (`mgmt/app.py`, :8443 HTTPS + 127.0.0.1:8001 HTTP)
FastAPI server run by `bedrock-d` with an embedded WebSocket hub, on two uvicorn
listeners: **8443 HTTPS** (`0.0.0.0`) for the operator dashboard + LAN mgmt API
(operator-authenticated, Ed25519, see `installer/lib/operator_auth.py`), and
**127.0.0.1:8001 HTTP** for the local CLI / intra-process API (loopback is
auth-exempt). Serves the Svelte build, exposes REST actions
(`/api/vms/{name}/{start,shutdown,migrate,convert}`, `/api/nodes/register`,
join handshake), pushes live state every 3 s on the `cluster` channel and
instant log events on the `event` channel, and proxies noVNC WebSockets at
`/vnc/{vm}` to the VM's host:VNC-port. See
[`components/mgmt-dashboard.md`](components/mgmt-dashboard.md).

### VictoriaMetrics + VictoriaLogs (:8428 / :9428)
Single-binary metrics and logs backend. Scrapes `{ip}:9100` (node_exporter) and
`{ip}:9177` (vm_exporter) on every node; the scrape config is regenerated when a
node registers and reloaded via HTTP `/-/reload`. VL takes `_time`-stamped JSON
from `push_log()` and syslog on port 5140. Data lives under `/opt/bedrock/data/`.

### node_exporter + vm_exporter (:9100 / :9177)
`node_exporter` is stock Prometheus. `vm_exporter` (`mgmt/vm_exporter.py`) is a
small Python `http.server` that parses `virsh domstats` + `drbdadm status` and
emits `libvirt_*` and `drbd_*` text metrics. Both deployed on every node via
`installer/lib/exporters.py` at init/join; both auto-start at boot.

### DRBD 9.3 (`kmod-drbd9x` from ELRepo)
Block-level replication. Resources use **external** meta-disks and
`--max-peers=7` so peers can be added later without rebuilding metadata. Ports
land in the 7700–7799 band: `port = 7700 + (minor − 1100)` (singleton minor
1101 → 7701; per-VM 1102+ → 7702+). See
[`storage-architecture.md`](storage-architecture.md).

### bedrock CLI (`installer/bedrock`)
Entry point on each node. Subcommands: `bootstrap`, `init`, `join`, `status`,
`node`, `vm`, `storage`. For VM lifecycle ops it is a thin HTTP client to the
local mgmt API at `127.0.0.1:8001`; install/join paths read
`/etc/bedrock/state.json` and call into `installer/lib/*.py`. Fetched at install
time from the install repo (the dev box or another host serving `installer/`).

### witness (optional, UDP 12321)
**BedRock Echo** — a passive K/V slot store (one slot per node, keyed by
loopback last octet; slot 254 = arbiter VIP). Each cluster node writes its slot
and reads peers' slots over UDP/12321 using ChaCha20-Poly1305 AEAD (msgpack
payload, shared `cluster.key`). The witness decides nothing; it holds state for
the election + arbiter-takeover logic in `bedrock-d`'s netd thread. Runs on an
ESP32 or a small MikroTik container — not in this repo; configured as a witness
host in rqlite's `witnesses` table.

## Ports

```
8443  HTTPS   dashboard + LAN mgmt API (operator-authed)
8001  HTTP    local CLI / intra-process (127.0.0.1, auth-exempt)
4001  rqlite HTTP API (mTLS)        4002  rqlite Raft
4011  arbiter rqlite HTTP (mTLS)    4012  arbiter rqlite Raft
9333  weed-master   8080 weed-volume   8888 weed-filer   8333 weed-s3
8428  VictoriaMetrics   9428  VictoriaLogs   5140  VL syslog
9100  node-exporter     9177  vm-exporter    9090  Cockpit
7732  mesh probe   7733 advert   7734 heartbeat (HMAC-SHA256) + ICMP
12321 witness (AEAD)
7700+ DRBD (7700–7799 band)   5900+ VNC   49152+ live-migrate
```

## Services (`installer/configs/*.service`)

Auto-started at `multi-user.target`: **`bedrock-d`**, `bedrock-rqlited`,
`bedrock-mdns`, `bedrock-redirect` (:80→:8443). `node-exporter` and `vm-exporter`
are their own auto-started units.

`bedrock-d` starts everything else once a clear quorum role is known
(`boot_orchestrator` → `_start_local_services`, idempotent and role-aware):
`bedrock-rqlited-arbiter` (on the `.254` holder), `bedrock-weed-master`
(Raft-3 lowest-octet set), `bedrock-weed-volume` + `bedrock-weed-s3` (every
node), and per-VM DRBD + libvirtd + this node's VMs. The SeaweedFS filer and its
S3 on `.254` are owned by `cluster_arbiter`.

## Directory layout on a mgmt+compute node

```
/etc/bedrock/
    state.json            per-node identity, mgmt_url, loopback_ip, hardware,
                          cold-boot recovery fields (atomic + fsynced)
    cluster.json          bootstrap-only rqlite peer list (read by
                          rqlite_setup --render-env at boot)
    cluster.key           32-byte shared key (HMAC-SHA256 for bedrock-net
                          probes/adverts; AEAD key for the Echo witness)
    storage.json          resolved VG name + storage layout decisions
    installer.env         BEDROCK_REPO=... (used by bedrock CLI subcommands)

/opt/bedrock/
    bin/                  victoria-metrics, victoria-logs, node_exporter,
                          vm_exporter.py
    data/vm/              VictoriaMetrics storage
    data/vl/              VictoriaLogs storage
    mgmt/                 mgmt app (app.py, ws.py, victoria.py, vm_exporter.py,
                          novnc/, ui/build/)
    scrape.yml            VM scrape config (regenerated on register)

/var/lib/bedrock/
    cluster/              cluster-singleton DRBD mount (arbiter rqlite + filer
                          leveldb3 + S3 IAM); mounted only on the .254 holder
    seaweedfs/volumes/    local weed-volume LV mount

/etc/drbd.d/
    global_common.conf
    cluster.res           cluster-singleton resource
    vm-<name>-disk0.res   per-VM resource, written during convert

/etc/systemd/system/
    bedrock-d.service               unified daemon (netd thread +
                                    mgmt/orchestrator asyncio)
    bedrock-rqlited.service         per-node rqlite (4001/4002)
    bedrock-rqlited-arbiter.service arbiter rqlite voter on .254 (4011/4012)
    bedrock-weed-master.service     SeaweedFS master (Raft-3 set)
    bedrock-weed-volume.service     SeaweedFS volume (every node)
    bedrock-weed-filer.service      SeaweedFS filer (on .254, DRBD-backed)
    bedrock-weed-s3.service         SeaweedFS S3 gateway (every node)
    bedrock-mdns.service            mDNS responder
    bedrock-redirect.service        HTTP :80 → HTTPS :8443
    bedrock-cert-refresh.service    TLS cert renewal
    bedrock-vg-loop.service         reattach loop-backed VG headroom PV at boot

/root/.ssh/
    id_ed25519, id_ed25519.pub   cluster identity
    authorized_keys              all cluster peers' pubkeys
    known_hosts                  pre-seeded at join time
    config                       StrictHostKeyChecking=accept-new for LAN/DRBD IPs
```

See [`reference/files.md`](reference/files.md) for the full list, including
compute-only nodes.

## The 10-second mental model

1. Every node runs `bedrock-d`, KVM + DRBD, and exporters.
2. Cluster truth lives in rqlite (Raft-replicated); the mgmt dashboard, active
   on whichever node holds `.254`, renders it.
3. The dashboard pushes state to browsers over WebSocket every 3 s and pushes
   log events the instant they happen.
4. Operator actions (convert, migrate, …) write rqlite; the orchestrator
   saga/reactor converges each node. Compute nodes observe rqlite and act
   locally — they carry no operator-facing orchestration logic.
5. Data lives in DRBD, replicating synchronously over the 100.X.Y mesh. VMs
   pivot between nodes via `virsh migrate` without touching disk.
