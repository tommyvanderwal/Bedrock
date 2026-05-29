# Bedrock documentation

Bedrock is a local-infrastructure HA platform. It turns AlmaLinux 10 boxes into
a cluster of 1 → N nodes running KVM VMs with DRBD replication, and flips
workloads between **cattle** (one local thin LV, no DRBD), **pet** (2-way DRBD),
and **ViPet** (3-way DRBD) online, no downtime.

These docs answer:

- **What happens** when I click `Migrate`, check `PET (HA)`, or kill a node?
- **In what order** do the pieces move, and **why**?
- **Where do I look** when something breaks — which log line, on which node,
  from which component?

---

## Start here

- [`architecture.md`](architecture.md) — the whole stack on one page:
  component map, port list, data-flow diagram. Read this first.
- [`cluster-quorum-spec.md`](cluster-quorum-spec.md) — the witness + arbiter
  takeover protocol. Passive K/V slot store (BedRock Echo, AEAD on UDP 12321),
  weighted vote (node = 100, witness = 1), exact-UUID-match arbiter takeover.
- [`storage-architecture.md`](storage-architecture.md) — the storage stack:
  one thinpool per node, an external-meta LV per DRBD resource, the cluster
  singleton DRBD capped at 3 peers, and SeaweedFS topology (filer on `.254`,
  master Raft-3 on lowest-octet nodes, volume + S3 on every node).
- [`sagas/README.md`](sagas/README.md) — index of every long-running cluster
  operation as a crash-resumable saga, with per-saga inputs/outputs, step
  contents, revert behaviour, and idempotency guarantees. Read here first when
  adding a cluster operation; the saga pattern is load-bearing for crash-safety.
- [`state-flow.md`](state-flow.md) — what each node does in each cluster state,
  what triggers transitions, and what failure modes look like: N=1 init → join
  → critical-tier promote → healthy N≥2 → isolation+failover → master rejoin →
  2-node HA → scale-down → boot recovery, plus a what-can-go-wrong matrix.
- [`network-walkthrough.md`](network-walkthrough.md) — an ASCII-heavy tour of
  the cluster's networking aimed at non-network engineers; the gentle intro
  before [`06-mesh-network.md`](06-mesh-network.md).

Per-module source companion notes live next to the code under
[`installer/lib/*.md`](../installer/lib/) and [`mgmt/*.md`](../mgmt/): a
module-purpose paragraph plus a one-line description per function.

## Actions (what engineers trigger)

The operations the dashboard / CLI expose. An operator change flows
operator → mgmt API → rqlite → reactor/saga → converge on each node; nobody
SSHes a compute node for state changes. Each doc walks the full sequence —
the API call, the rqlite writes, the DRBD/libvirt commands, the log lines
emitted, and the failure modes.

| Action | Trigger | Doc |
|---|---|---|
| Install a node | `curl | bash` + `bedrock bootstrap` | [`actions/install-bootstrap.md`](actions/install-bootstrap.md) |
| Start a new cluster | `bedrock init` | [`actions/init-cluster.md`](actions/init-cluster.md) |
| Add a node to a cluster | `bedrock join` | [`actions/join-cluster.md`](actions/join-cluster.md) |
| Manage ISOs (upload / list / delete) | dashboard `/isos` | [`actions/iso-library.md`](actions/iso-library.md) |
| Create a VM | dashboard `+ New VM` / `bedrock vm create` | [`actions/vm-create.md`](actions/vm-create.md) |
| Change HA level | PET / ViPet checkboxes in Settings | [`actions/vm-convert.md`](actions/vm-convert.md) |
| Change vCPU / RAM / Disk / Priority / CDROM | Settings page | [`actions/vm-settings.md`](actions/vm-settings.md) |
| Import a VM from VMware/Hyper-V/… | `/imports` upload + convert | [`actions/vm-import-export.md`](actions/vm-import-export.md) |
| Export a VM to qcow2/vmdk/vhdx/raw | Export card on Settings | [`actions/vm-import-export.md`](actions/vm-import-export.md) |
| Live-migrate a VM | `Live Migrate` button | [`actions/vm-migrate.md`](actions/vm-migrate.md) |
| Start / stop / delete a VM | dashboard buttons | [`actions/vm-lifecycle.md`](actions/vm-lifecycle.md) |
| Configure a backup target | dashboard `/backups` | [`actions/backup-target-set.md`](actions/backup-target-set.md) |
| Back up a VM | dashboard `Backup` button on VM | [`actions/vm-backup.md`](actions/vm-backup.md) |
| Restore a VM from a backup | dashboard `Restore` button on snapshot | [`actions/vm-restore.md`](actions/vm-restore.md) |
| List & delete backups | dashboard `/backups` (cluster-wide) or VM card | [`actions/backup-list-delete.md`](actions/backup-list-delete.md) |
| Schedule periodic backups | dashboard VM page → Schedule cron field | [`actions/backup-schedule.md`](actions/backup-schedule.md) |

## Failure scenarios (what happens when things break)

| Scenario | Doc |
|---|---|
| Secondary node power loss | [`scenarios/power-loss-secondary.md`](scenarios/power-loss-secondary.md) |
| Primary node power loss (running the VM) | [`scenarios/power-loss-primary.md`](scenarios/power-loss-primary.md) |
| All nodes power loss | [`scenarios/power-loss-all.md`](scenarios/power-loss-all.md) |
| Split-brain (DRBD) | [`scenarios/split-brain.md`](scenarios/split-brain.md) |
| Network partition | [`scenarios/network-partition.md`](scenarios/network-partition.md) |
| Node rejoin after outage | [`scenarios/node-rejoin.md`](scenarios/node-rejoin.md) |

## Reference

| Topic | Doc |
|---|---|
| Every log line — format, origin, how to query | [`reference/logs.md`](reference/logs.md) |
| All ports + networks | [`reference/ports.md`](reference/ports.md) |
| Every file Bedrock reads or writes | [`reference/files.md`](reference/files.md) |
| HTTP + WebSocket API | [`reference/api.md`](reference/api.md) |

## Components (what each service does)

| Component | Port | Bind | Doc |
|---|---|---|---|
| mgmt dashboard (FastAPI + Svelte) | 8443 HTTPS (operator-authed) | `0.0.0.0` | [`components/mgmt-dashboard.md`](components/mgmt-dashboard.md) |
| mgmt local CLI / intra-process API | 8001 HTTP (auth-exempt) | `127.0.0.1` | [`components/mgmt-dashboard.md`](components/mgmt-dashboard.md) |
| **rqlite (per-node)** | 4001 HTTPS mTLS + 4002 Raft | node | [`01-rqlite-state-store.md`](01-rqlite-state-store.md) |
| **rqlite-arbiter (extra voter)** | 4011 HTTPS mTLS + 4012 Raft | `.254` (singleton) | [`01-rqlite-state-store.md`](01-rqlite-state-store.md) |
| **SeaweedFS master** | 9333 | node (Raft-3 nodes only) | [`storage-architecture.md`](storage-architecture.md) |
| **SeaweedFS volume** | 8080 | `0.0.0.0` (every node) | [`storage-architecture.md`](storage-architecture.md) |
| **SeaweedFS filer** | 8888 | `.254` (singleton; DRBD-backed) | [`storage-architecture.md`](storage-architecture.md) |
| **SeaweedFS s3** | 8333 | `0.0.0.0` (every node) | [`storage-architecture.md`](storage-architecture.md) |
| **bedrock-echo (witness)** | UDP 12321 (ChaCha20-Poly1305 AEAD) | LAN appliance | [`cluster-quorum-spec.md`](cluster-quorum-spec.md) |
| VictoriaMetrics | 8428 | node | [`components/metrics.md`](components/metrics.md) |
| VictoriaLogs | 9428 (syslog 5140) | node | [`components/metrics.md`](components/metrics.md) |
| node_exporter + vm_exporter | 9100 / 9177 | node | [`components/exporters.md`](components/exporters.md) |
| DRBD | kernel + per-link port (7000+minor) | per-NIC IP | [`storage-architecture.md`](storage-architecture.md) + [`05-drbd-internals.md`](05-drbd-internals.md) |
| bedrock-net (mesh) | UDP 7732 mcast probe + 7733 advert + 7734 heartbeat + ICMP | per-NIC | [`06-mesh-network.md`](06-mesh-network.md) |
| bedrock-mdns | mDNS (5353) | node | — |
| bedrock-redirect | :80 → :8443 | `0.0.0.0` | — |
| **bedrock-d (the daemon)** | — | — | [`daemon-unification.md`](daemon-unification.md) |
| Cockpit | 9090 | node | — (upstream docs) |

> **One daemon, one state store.** Every node runs a single `bedrock-d` Python
> process: a netd thread (mesh, election, witness, `.254` arbiter, routing) plus
> an asyncio mgmt/orchestrator (dashboard, saga executor, reactor). It is the
> only thing that auto-starts the rest. Cluster-wide state lives in **rqlite**
> (Raft-replicated SQLite, mTLS on 4001/4002). The witness is a passive AEAD K/V
> slot store on UDP 12321 ([`cluster-quorum-spec.md`](cluster-quorum-spec.md)).
> S3 object storage is **SeaweedFS** ([`storage-architecture.md`](storage-architecture.md)).

---

## Deep dives (design-level internals)

- [`daemon-unification.md`](daemon-unification.md) — how the single `bedrock-d`
  process structures its netd thread and asyncio mgmt/orchestrator, and the
  locking that keeps the shared state safe across them.
- [`cluster-quorum-spec.md`](cluster-quorum-spec.md) — witness + arbiter
  takeover protocol.
- [`storage-architecture.md`](storage-architecture.md) — on-disk layout, DRBD
  topology, SeaweedFS deployment.
- [`05-drbd-internals.md`](05-drbd-internals.md) — DRBD activity log, bitmap,
  how DRBD stays fast and crash-safe (backend-agnostic primer).
- [`06-mesh-network.md`](06-mesh-network.md) — the bedrock-net mesh, per-NIC
  link-local addressing, kernel routing, DRBD multipath integration.
- [`mesh-network-v1-uncertainties.md`](mesh-network-v1-uncertainties.md) —
  what's tested, what isn't, and what's known-fragile in the mesh layer.

## Conventions used in these docs

- **Code paths** point to the canonical source-of-truth, e.g. `mgmt/app.py:_vm_migrate`.
- **Log lines** are quoted verbatim from the code. Placeholders in curly braces
  (`{vm_name}`, `{src}`, `{dst}`) are f-string interpolations at runtime.
- **ASCII sequence diagrams** use `─>` for a call/action and `═>` for replication
  traffic (DRBD / migration memory copy).
- **`T=0`** marks the start of an operation. Durations are measured from the
  entry point (HTTP request or CLI command).
