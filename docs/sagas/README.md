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
  / `operation_steps` tables; submitted via
  `POST /api/operations`.

## All sagas

| Kind | File | Triggered by | Reverts via |
|------|------|--------------|-------------|
| [`cluster_init`](cluster_init.md) | `bedrock_d/install/cluster_init.py` | `bedrock init` CLI | n/a — initial state |
| [`node_join`](node_join.md) | `bedrock_d/install/node_join.py` | `bedrock join` CLI | [`node_leave`](node_leave.md) |
| [`node_leave`](node_leave.md) | `bedrock_d/install/node_leave.py` | `bedrock node leave <node>` (on any surviving node, not the target itself) | n/a — terminal |
| [`cluster_tier_promote_master`](cluster_tier_promote_master.md) | `bedrock_d/install/cluster_tier.py` | orchestrator `cluster_tier_watcher` task | manual (`tier_storage.drbd_demote_to_local()` helper; not yet wrapped as a saga) |
| [`cluster_tier_join_peer`](cluster_tier_join_peer.md) | `bedrock_d/install/cluster_tier.py` | last step of `node_join` | drops out automatically when peer leaves |
| [`cluster_rename`](cluster_rename.md) | `bedrock_d/cluster/rename.py` | `bedrock cluster rename <new-name>` | run again with the previous name |
| [`vm_create`](vm_create.md) | `bedrock_d/vm/create.py` | `POST /api/vms` | [`vm_destroy`](vm_destroy.md) |
| [`vm_destroy`](vm_destroy.md) | `bedrock_d/vm/destroy.py` | `DELETE /api/vms/{name}` | n/a — terminal |
| [`vm_grow`](vm_grow.md) | `bedrock_d/vm/grow.py` | `POST /api/vms/{name}/grow` | manual (shrink not supported online) |
| [`vm_migrate`](vm_migrate.md) | `bedrock_d/vm/migrate.py` | `POST /api/vms/{name}/migrate` | run again pointing back at the source node |
| [`replica_repair`](replica_repair.md) | `bedrock_d/orchestrator/replica_repair.py` | orchestrator `self_heal` calm loop | manual (`tier_storage.drbd_remove_peer`) |

## Per-saga doc structure

Each per-saga doc follows the same shape:

1. **Purpose** — one paragraph, why this saga exists.
2. **Trigger** — who submits it.
3. **Inputs (ctx)** — fields the caller must set.
4. **Outputs (ctx)** — fields filled by the saga's own steps.
5. **Step overview** — table of step names, one-liners, and links
   into the step-detail section.
6. **Revert** — how to undo the effects (paired inverse saga, or
   manual procedure if there isn't one).
7. **Idempotency / resume** — what happens on re-run.
8. **Step details** — section per step, with the idempotency check
   it performs and what it changes.

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
