# bedrock_d/orchestrator/replica_repair.py

The `ReplicaRepair` saga rebuilds one missing DRBD replica after a host is
permanently lost, restoring a resource's redundancy onto a chosen target node.
The self-heal calm loop (`self_heal.py`) submits it one resource at a time
through `self_heal._submit_repair`. Like every runtime saga it is rqlite-backed
and crash-safe: each step is idempotent, and the boot resume sweep re-runs it if
`bedrock-d` dies mid-repair. It handles two shapes, chosen by `ctx['kind']`: the
`cluster` singleton, and per-VM `pet`/`vipet` resources.

## Functions / Classes

### `parse_node_ids(res_text: str) -> dict[str, int]`
Map `node_name → node-id` by scanning the `on <name> { … node-id N; }` blocks of
a rendered `.res` file's text.
- **In:** `res_text` — contents of a DRBD `.res` file (may be empty/garbage).
- **Out:** dict of name → id; empty dict if nothing matches. No side effects.

### `next_free_node_id(used: dict[str, int], max_peers: int = 7) -> int`
Pick the smallest node-id in `0..max_peers-1` not already assigned, keeping
existing assignments untouched (DRBD node-ids are permanent).
- **In:** `used` — name → id map of taken ids; `max_peers` — id ceiling.
- **Out:** the chosen int. Raises `RuntimeError` if every id is taken. No side
  effects.

### `class ReplicaRepair` — `@saga("replica_repair")`
Restore redundancy for ONE resource onto ONE target node. Two ordered steps.

**ctx inputs** (from `self_heal._submit_repair`): `resource` (DRBD resource
name), `kind` (`'singleton'` | `'pet'` | `'vipet'`), `target` (node to host the
new replica), `vm_name` (`''` for the singleton).

#### `step_validate(self, ctx)` — `@step("validate")`
Check required fields and load the resource's current shape from rqlite.
- **In:** `ctx` with `resource`, `kind`, `target` set.
- **Out:** mutates `ctx` in place — sets `minor`, `data_size_bytes`,
  `max_peers` (default 7), `existing_peers` (parsed from the row's `peers` JSON,
  `[]` on parse error). If `target` is already in `existing_peers`, sets
  `already_member = True`. Reads `drbd_resources` (level `none`). Raises
  `ValueError` for a missing ctx field, `RuntimeError` if the resource has no
  `drbd_resources` row.

#### `step_repair(self, ctx)` — `@step("repair")`
Perform the rebuild, dispatching on `kind`.
- **In:** `ctx` enriched by `step_validate`.
- **Out:** no return value. Returns immediately if `already_member`. Otherwise
  calls `_repair_singleton` (kind `singleton`) or `_repair_vm_replica` (any
  other kind). Side effects are in those helpers.

### Private helpers
- `_repair_singleton(ctx)` — resolves the target's `loopback_ip` from `nodes`,
  delegates to `tier_storage.promote_cluster_to_3way({name, loopback_ip})`, then
  `INSERT OR REPLACE`s the target into `cluster_drbd_membership` and bumps the
  rqlite revision. Subprocess work (LVM/DRBD) lives inside
  `promote_cluster_to_3way`.
- `_repair_vm_replica(ctx)` — the full per-VM expand (see How it works).

## How it works

`step_validate` is the idempotency gate: if `target` already appears in the
resource's `peers`, it flags `already_member` and `step_repair` is a no-op, so a
resumed run after a crash does nothing harmful.

For a per-VM resource, `_repair_vm_replica` runs this sequence; node-ids are
read from a live holder so existing peers keep their stable ids:

```
existing_peers + [target]  ─┐
                            ▼
  resolve host + loopback_ip for every peer (nodes table, level none)
        └─ missing host/loopback → RuntimeError (abort before any change)
                            │
  learn node-ids: cat <holder>.res → parse_node_ids (first readable wins)
        fallback: positional 0..n-1 for existing peers (fresh-cluster edge)
        target := next_free_node_id(...)        # smallest unused id
                            │
  1. lvcreate_pair(target_host, resource, data_gb, max_peers)   # data+meta LVs
  2. render extended .res → write to ALL peers (cat > .res << EOF)
  3. target:   drbdadm create-md --force --max-peers=N  (check=False)
     holders:  drbdadm adjust <resource>                (check=False)
     target:   drbdadm up <resource>   → resync from the primary
  4. rqlite: UPDATE drbd_resources.peers = new_peers
            if VM row: append target to vms.failover_order (if absent)
            bump_revision
```

`data_gb = max(1, data_size_bytes // GiB)`. Steps 1–3 are ordered so the new
LVs and matching `.res` exist on every holder before any `drbdadm` runs;
DRBD commands use `check=False` so partial-state reruns don't abort. The rqlite
writes in step 4 happen last, which is also what makes `step_validate`'s
`existing_peers` membership test the resume guard. Appending `target` to
`failover_order` makes the new replica a recognised takeover target for VM
failover.

## Why

Node-ids are read from a live holder's `.res` rather than re-derived so each
existing peer keeps its permanent DRBD node-id; only the new target gets the
smallest free slot. rqlite updates land after the on-disk/DRBD work so a crash
leaves `peers` unchanged and the saga safely retries from scratch.
