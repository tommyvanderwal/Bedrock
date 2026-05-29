# Bedrock sagas — index

Every long-running cluster operation in Bedrock is implemented as a
**saga**: an ordered list of idempotent steps with durable progress
records. Each saga has one entry in this index and a per-saga doc
that lists its steps, inputs, outputs, and revert behaviour.

For the saga-engine internals (backend protocol, resume rules,
crash-safety guarantees) see [`bedrock_d/orchestrator/sagas/`](../../bedrock_d/orchestrator/sagas/)
and [`docs/storage-architecture.md`](../storage-architecture.md#everything-goes-through-rqlite-except-arbiter-recovery).

## When sagas run

- **Install-time** (`cluster_init`, `node_join`, `node_leave`) — driven
  from the `bedrock` CLI; persists progress to
  `/var/lib/bedrock/init-progress.json` because rqlite isn't necessarily up.
- **Run-time** (everything else) — persisted to rqlite's `operations`
  / `operation_steps` tables. Submitted either through the generic
  `POST /api/operations` endpoint (`mgmt/routes_operations.py`) or
  directly in-process: the VM-lifecycle endpoints (`POST /api/vms`,
  `DELETE /api/vms/{name}`, `POST /api/vms/{name}/migrate`) call
  `SagaExecutor.submit()` then `execute_one()` synchronously on the
  mgmt-master, and the orchestrator's calm loops submit their own
  (`cluster_tier_promote_master`, `replica_repair`).

## All sagas

| Kind | File | Triggered by | Reverts via |
|------|------|--------------|-------------|
| [`cluster_init`](cluster_init.md) | `bedrock_d/install/cluster_init.py` | `bedrock init` CLI | n/a — initial state |
| [`node_join`](node_join.md) | `bedrock_d/install/node_join.py` | `bedrock join` CLI | [`node_leave`](node_leave.md) |
| [`node_leave`](node_leave.md) | `bedrock_d/install/node_leave.py` | `bedrock node leave <node>` (on any surviving node, not the target itself) | n/a — terminal |
| [`cluster_tier_promote_master`](cluster_tier_promote_master.md) | `bedrock_d/install/cluster_tier.py` | `mgmt/orchestrator.py` `cluster_tier_watcher` task (mgmt-master only) | manual (`tier_storage.drbd_demote_to_local()` helper; no inverse saga) |
| [`cluster_tier_join_peer`](cluster_tier_join_peer.md) | `bedrock_d/install/cluster_tier.py` | submitted as a step inside `node_join` (the joiner) | drops out automatically when the peer leaves |
| [`cluster_rename`](cluster_rename.md) | `bedrock_d/cluster/rename.py` | `bedrock cluster rename <new-name>` CLI → `POST /api/operations` | run again with the previous name |
| [`vm_create`](vm_create.md) | `bedrock_d/vm/create.py` | `POST /api/vms` | [`vm_destroy`](vm_destroy.md) |
| [`vm_destroy`](vm_destroy.md) | `bedrock_d/vm/destroy.py` | `DELETE /api/vms/{name}` | n/a — terminal |
| [`vm_grow`](vm_grow.md) | `bedrock_d/vm/grow.py` | `POST /api/operations` (kind `vm_grow`) | manual (online shrink not supported) |
| [`vm_migrate`](vm_migrate.md) | `bedrock_d/vm/migrate.py` | `POST /api/vms/{name}/migrate` | run again pointing back at the source node |
| [`replica_repair`](replica_repair.md) | `bedrock_d/orchestrator/replica_repair.py` | `self_heal` calm loop (mgmt-master only) | manual (`tier_storage.drbd_remove_peer`) |

## Per-saga doc structure

Each per-saga doc has two top-level sections:

1. **Summary** — what the saga does, what triggers it, where it runs,
   and its end state; a `### Steps` table (one row per step, in order)
   and an `### Inputs / outputs (ctx)` table. Cluster-wide revert and
   resume behaviour are stated here in one line each.
2. **Detail** — one `### N. step_name` block per step: what it does, its
   **Revert:** (compensation), and its **Idempotent:** guarantee (how a
   re-run converges).

## Conventions

- **Steps are ordered.** The executor runs them in declaration order
  and records each as `done` in `operation_steps`. A saga that
  crashed mid-step resumes at the first not-`done` step on retry.
- **Idempotency is required.** Every step either no-ops if its
  effect is already in place, or is a clean re-run. There are NO
  "fragile" steps that must run exactly once.
- **Ctx is per-run, not per-saga-instance.** On resume the executor
  rebuilds ctx from the operation's `params`, then re-runs each
  not-`done` step. Any data that needs to survive a crash and be
  available to a *later* step in a *resumed* run must come from
  `params` (the durable input dict) or be re-derived from durable
  state (`/etc/bedrock/state.json` or rqlite — read via
  `cluster_state.load_cluster()`). See `_enrich_params_from_state`
  in `node_join.py` for the pattern.

## Cross-links

- Storage architecture: [`docs/storage-architecture.md`](../storage-architecture.md)
- Cluster quorum: [`docs/cluster-quorum-spec.md`](../cluster-quorum-spec.md)
- Saga engine: [`bedrock_d/orchestrator/sagas/`](../../bedrock_d/orchestrator/sagas/)
