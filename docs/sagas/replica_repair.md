# Saga: `replica_repair`

**Module:** `bedrock_d/orchestrator/replica_repair.py`  
**Class:** `ReplicaRepair`

## Purpose

Rebuild **one** lost replica after a node is declared permanently lost.
This is the actuator the self-heal calm loop
(`bedrock_d/orchestrator/self_heal.py`) submits — one resource at a
time — to restore redundancy that a dead node was providing:

- the arbiter / `cluster` singleton back toward its 3-way set;
- a **pet** dropped to a single replica back to 2-way;
- a **vipet** dropped to 2-way back to 3-way.

It only ever **adds** a replica onto a target node the self-heal loop
already vetted against the 80 % disk gate. It never demotes, destroys,
or moves a primary.

## Trigger

`self_heal_task()` in `mgmt/orchestrator.py` (leader-only calm loop)
computes the next repair under the disk gate and submits this saga via
the rqlite-backed executor with `target_node = self` (the master, which
holds `.254` and is the single writer). The loop submits at most one
repair per pass, so two repairs never race.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `resource` | str | DRBD resource name (`cluster` or `vm-<name>-diskN`) |
| `kind` | str | `singleton` \| `pet` \| `vipet` |
| `target` | str | node_name that will host the new replica |
| `vm_name` | str | VM name for per-VM resources (`''` for the singleton) |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `existing_peers` | `validate` | the resource's current peer node set |
| `minor` / `data_size_bytes` / `max_peers` | `validate` | resource shape read from `drbd_resources` |
| `already_member` | `validate` | short-circuit flag if the target already hosts the resource |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate`](#validate) | Read the `drbd_resources` row (minor, size, peers); short-circuit if `target` is already a peer |
| 2 | [`repair`](#repair) | Singleton → `tier_storage.promote_cluster_to_3way` + record in `cluster_drbd_membership`. Per-VM → lvcreate the data+meta pair on the target, extend the `.res` keeping every existing peer's stable DRBD node-id, distribute + `drbdadm adjust`, bring the new replica up to resync, then persist `drbd_resources.peers` + append the target to `vms.failover_order` |

## Revert

No automated revert. Removing a replica is the operator's
decommission path (`tier_storage.drbd_remove_peer`); self-heal only
ever rebuilds toward the target replica count. Re-running this saga is
safe (idempotent), so a half-finished repair is resumed, not reverted.

## Idempotency

- `validate` short-circuits when `target` is already in the resource's
  peer set, so a resumed run after a successful repair is a no-op.
- `lvcreate_pair` skips an LV that already exists; `drbdadm
  create-md`/`up`/`adjust` tolerate already-applied state.
- The rqlite `peers` / `failover_order` writes only append the target,
  so re-running doesn't duplicate it.

## Step details

### `validate`

Reads `minor`, `data_size_bytes`, `max_peers`, and the current `peers`
list from the resource's `drbd_resources` row. Raises if the row is
missing. Sets `already_member` when the target is already a peer so the
repair step no-ops.

### `repair`

For `kind == "singleton"`: looks up the target's loopback, calls the
existing tested singleton-expand path
`tier_storage.promote_cluster_to_3way`, and records the new member in
`cluster_drbd_membership`.

For `pet` / `vipet`: parses the existing peers' DRBD node-ids from a
live holder's `.res` (kept stable per L3), assigns the smallest free
node-id to the new target, `lvcreate`s the data+meta pair on the
target, renders + distributes the extended `.res` to every peer,
`drbdadm adjust`s existing holders and `drbdadm up`s the new replica so
it resyncs from the primary, then persists the extended `peers` and
appends the target to the VM's `failover_order`.
