# Saga: `replica_repair`

Code: `bedrock_d/orchestrator/replica_repair.py` (`ReplicaRepair`).

## Summary

**What:** rebuild **one** lost replica onto **one** target node, restoring the
redundancy a permanently-lost node was providing:

- the arbiter / `cluster` singleton back toward its `min(3, N)`-way set;
- a **pet** dropped to a single replica back to 2-way;
- a **vipet** dropped to 2-way back to 3-way.

It only ever **adds** a replica. It never demotes, destroys, or moves a primary.

**Trigger:** the self-heal calm loop `self_heal_task()`
(`bedrock_d/orchestrator/self_heal.py`, launched by `mgmt/orchestrator.py`).
The loop runs **only on the mgmt-master** (single writer, holds `.254`, has
quorum). Each 30 s tick it treats a node mesh-unreachable for ≥ `CALM_DOWN_S`
(default 65 min) as permanently lost, computes the single highest-priority
repair that keeps its target at/under the **80 % disk gate** (charged by the
resource's actual thin-LV usage), and submits this saga with
`target_node = self`. Priority order: singleton, then pets, then vipets, each
group high → normal → low. At most one repair per pass, so two never race.

**Where:** runs inline on the master (`_submit_repair` → `execute_one`).
rqlite-backed and idempotent, so a crash mid-repair is re-run by the boot
resume sweep (`executor.resume_in_flight`).

**End state:** the target node holds a fresh replica of the resource resyncing
from the primary, and the membership is persisted — `tiers.peers` +
`cluster_drbd_membership` for the singleton; `drbd_resources.peers` +
`vms.failover_order` for a per-VM resource.

**Inputs (`ctx`, from `self_heal._submit_repair`):**

| key | type | meaning |
|-----|------|---------|
| `resource` | str | DRBD resource name (`cluster` or `vm-<name>-diskN`) |
| `kind` | str | `singleton` \| `pet` \| `vipet` |
| `target` | str | node_name to host the new replica |
| `vm_name` | str | VM name for per-VM resources (`''` for the singleton) |

**Steps:**

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate`](#validate) | Read the `drbd_resources` row (`minor`, `data_size_bytes`, `max_peers`, `peers`); short-circuit if `target` is already a peer |
| 2 | [`repair`](#repair) | singleton → `tier_storage.promote_cluster_to_3way` + record in `cluster_drbd_membership`. per-VM → lvcreate the data+meta pair on the target, extend the `.res` keeping every peer's stable DRBD node-id, distribute + `drbdadm adjust`, bring the new replica up to resync, then persist `drbd_resources.peers` + append `target` to `vms.failover_order` |

## Detail

### `validate`

Requires `resource`, `kind`, `target` in ctx (raises `ValueError` otherwise).
Reads the resource's `drbd_resources` row at level `none`, setting
`ctx["minor"]`, `ctx["data_size_bytes"]`, `ctx["max_peers"]` (default 7), and
`ctx["existing_peers"]` (parsed from the `peers` JSON column). Raises if the row
is missing. Sets `ctx["already_member"] = True` when `target` is already a peer.

- **Revert:** none — read-only.
- **Idempotent:** pure read plus the `already_member` short-circuit, which makes
  a resumed run after a completed repair a no-op.

### `repair`

Returns immediately if `already_member` is set. Otherwise branches on `kind`.

**`singleton`:** looks up the target's `loopback_ip` from `nodes`, calls
`tier_storage.promote_cluster_to_3way({"name": target, "loopback_ip": lo})` (the
tested singleton-expand path: write the extended `.res`, `drbdadm adjust`,
distribute the identical `.res` to every existing peer, and set `tiers.peers`),
then `INSERT OR REPLACE` the target into `cluster_drbd_membership` and bump the
rqlite revision.

**`pet` / `vipet`:**
```
new_peers = existing_peers + [target]
resolve host + loopback_ip for every peer from `nodes`   (raise if any missing)
learn existing DRBD node-ids by cat-ing a live holder's .res        (parse_node_ids)
   fall back to positional 0..n-1 if no holder had a readable .res
assign target the smallest free node-id 0..max_peers-1              (next_free_node_id)
1. lvcreate data+meta pair on target  (data_gb = data_size_bytes // 1GiB, min 1)
2. render extended .res, write it to ALL peers (existing + new)
3. target:  drbdadm create-md --force --max-peers=N; drbdadm up   (resync from primary)
   holders: drbdadm adjust                                        (pick up new peer)
4. drbd_resources.peers = new_peers; append target to vms.failover_order; bump revision
```
DRBD node-ids are kept stable per existing peer because they are permanent (L3);
the new target gets the smallest unused id. Steps 1–3 use SSH via `lvm._run_on`
with `check=False`, so already-applied state does not abort the run.

- **Revert:** none. Removing a replica is the operator's decommission path
  (`tier_storage.drbd_remove_peer`); self-heal only ever rebuilds toward the
  target replica count. A half-finished repair is re-run, not reverted.
- **Idempotent:** `lvcreate_pair` skips an existing LV; `drbdadm
  create-md`/`up`/`adjust` tolerate already-applied state; the `peers` /
  `failover_order` writes only append `target`, so re-running never duplicates
  it.
