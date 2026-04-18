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

- [`architecture.md`](architecture.md) — the whole stack on one page, with a
  component map, port list, and data-flow diagram. Read this first.

## Actions (what engineers trigger)

These are the operations the dashboard / CLI expose. Each doc walks through
the full sequence — SSH calls, DRBD commands, log lines emitted, failure modes.

| Action | Trigger | Doc |
|---|---|---|
| Install a node | `curl | bash` + `bedrock bootstrap` | [`actions/install-bootstrap.md`](actions/install-bootstrap.md) |
| Start a new cluster | `bedrock init` | [`actions/init-cluster.md`](actions/init-cluster.md) |
| Add a node to a cluster | `bedrock join` | [`actions/join-cluster.md`](actions/join-cluster.md) |
| Create a VM | `bedrock vm create` / dashboard | [`actions/vm-create.md`](actions/vm-create.md) |
| Change HA level | PET / ViPet checkboxes | [`actions/vm-convert.md`](actions/vm-convert.md) |
| Live-migrate a VM | `Live Migrate` button | [`actions/vm-migrate.md`](actions/vm-migrate.md) |
| Start / stop / delete a VM | dashboard buttons | [`actions/vm-lifecycle.md`](actions/vm-lifecycle.md) |

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
| VictoriaMetrics | 8428 | [`components/metrics.md`](components/metrics.md) |
| VictoriaLogs | 9428 (syslog 5140) | [`components/metrics.md`](components/metrics.md) |
| node_exporter + vm_exporter | 9100 / 9177 | [`components/exporters.md`](components/exporters.md) |
| DRBD | kernel + port 7000+minor | [`components/drbd.md`](components/drbd.md) |
| Cockpit | 9090 | —  (upstream docs) |

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

## Conventions used in these docs

- **Code paths** point to the canonical source-of-truth, e.g. `mgmt/app.py:_vm_migrate`.
- **Log lines** are quoted verbatim from the code. Placeholders in curly braces
  (`{vm_name}`, `{src}`, `{dst}`) are f-string interpolations at runtime.
- **ASCII sequence diagrams** use `─>` for a call/action and `═>` for replication
  traffic (DRBD / migration memory copy).
- **`T=0`** marks the start of an operation. Durations are measured from the
  entry point (HTTP request or CLI command).
