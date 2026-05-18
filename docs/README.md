# Bedrock documentation

Bedrock is a local-infrastructure HA platform. It turns one AlmaLinux 9
box into a cluster of 1 → N nodes, running KVM VMs with DRBD replication,
and lets you flip workloads between **cattle** (local-only), **pet**
(2-way DRBD), and **ViPet** (3-way DRBD) — online, no downtime.

This documentation exists so any engineer picking up the project can answer:

- **What happens** when I click `Migrate`, or check `PET (HA)`, or kill a node?
- **In what order** do the pieces move, and **why**?
- **Where do I look** when something breaks — which log line, on which node,
  from which component?

---

## Start here

- [`state-flow.md`](state-flow.md) — **what each node does in each
  cluster state, what triggers transitions, and what failure modes
  look like.** Covers N=1 init → join → critical-tier promote →
  healthy N≥2 → isolation+failover → master rejoin → 2-node HA →
  scale-down → boot recovery, plus a what-can-go-wrong matrix.
  ~20 minutes to read; the single best place to start after the
  May-2026 Rust-removal rewrite.
- [`architecture.md`](architecture.md) — the whole stack on one page, with a
  component map, port list, and data-flow diagram.
- [`network-walkthrough.md`](network-walkthrough.md) — a friendly,
  ASCII-art-heavy tour of how the cluster's networking actually works
  (every step, every decision). Aimed at non-network engineers; use as
  the gentle introduction before diving into `06-mesh-network.md`.

Per-module Python source companion notes live next to the code
under [`installer/lib/*.md`](../installer/lib/) and
[`mgmt/*.md`](../mgmt/). Each has a module-purpose paragraph at
the top + a one-sentence description per function.

## Actions (what engineers trigger)

These are the operations the dashboard / CLI expose. Each doc walks through
the full sequence — SSH calls, DRBD commands, log lines emitted, failure modes.

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

| Component | Port | Doc |
|---|---|---|
| mgmt dashboard (FastAPI + Svelte) | 8080 | [`components/mgmt-dashboard.md`](components/mgmt-dashboard.md) |
| **rqlite (cluster-state store)** | 4001 HTTP / 4002 Raft | [`01-rqlite-state-store.md`](01-rqlite-state-store.md) |
| **rqlite-arbiter (3rd voter, follows mgmt master)** | same ports on `100.X.Y.254/32` | [`01-rqlite-state-store.md`](01-rqlite-state-store.md) |
| **SeaweedFS master + volume + filer + s3** | 9333 / 8080 / 8888 / 8333 | [`components/storage-tiers.md`](components/storage-tiers.md) |
| VictoriaMetrics | 8428 | [`components/metrics.md`](components/metrics.md) |
| VictoriaLogs | 9428 (syslog 5140) | [`components/metrics.md`](components/metrics.md) |
| node_exporter + vm_exporter | 9100 / 9177 | [`components/exporters.md`](components/exporters.md) |
| DRBD | kernel + port 7000+minor | [`components/drbd.md`](components/drbd.md) |
| bedrock-net (mesh discovery + routing) | UDP 7732 (discovery) + ICMP echo (latency) + UDP 7733 (advertisement) | [`06-mesh-network.md`](06-mesh-network.md) |
| bedrock-rust (witness + fence + peer heartbeat — POST-REWRITE) | TCP 8200 peer | [`03-witness-and-orchestrator.md`](03-witness-and-orchestrator.md) |
| Cockpit | 9090 | —  (upstream docs) |

> **Post-0.8-alpha rewrite (2026-05-18)**: cluster state lives in
> **rqlite**; the bedrock-rust hash-chained log is gone. S3 storage
> uses **SeaweedFS** (Garage + RustFS retired). See
> [`post-alpha-rewrite-notes.md`](post-alpha-rewrite-notes.md) for
> the full design rationale (D-01..D-22).

---

## Deep dives (design-level internals)

Older design documents that predate the operational docs above and cover
the internals in more detail:

- [`01-storage-stack.md`](01-storage-stack.md) — physical-to-virtual mapping
  of how a VM's disk reaches the guest kernel.
- [`02-drbd-replication.md`](02-drbd-replication.md) — network topology and
  DRBD wire protocol.
- [`03-witness-and-orchestrator.md`](03-witness-and-orchestrator.md) — the
  failover orchestrator design and 2-of-3 quorum logic.
- [`04-boot-recovery-gaps.md`](04-boot-recovery-gaps.md) — known gaps in
  auto-recovery on cold boot.
- [`05-drbd-internals.md`](05-drbd-internals.md) — activity log, bitmap,
  and how DRBD stays fast + crash-safe.
- [`06-mesh-network.md`](06-mesh-network.md) — bedrock-net daemon,
  per-NIC link-local addressing, kernel routing, DRBD multi-path
  integration, cross-segment collision handling.
- [`mesh-network-v1-uncertainties.md`](mesh-network-v1-uncertainties.md) —
  honest list of what's tested, what isn't, and what's known-to-be-fragile
  in the mesh layer.

## Conventions used in these docs

- **Code paths** point to the canonical source-of-truth, e.g. `mgmt/app.py:_vm_migrate`.
- **Log lines** are quoted verbatim from the code. Placeholders in curly braces
  (`{vm_name}`, `{src}`, `{dst}`) are f-string interpolations at runtime.
- **ASCII sequence diagrams** use `─>` for a call/action and `═>` for replication
  traffic (DRBD / migration memory copy).
- **`T=0`** marks the start of an operation. Durations are measured from the
  entry point (HTTP request or CLI command).
