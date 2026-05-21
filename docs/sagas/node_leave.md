# Saga: `node_leave`

**Module:** `bedrock_d/install/node_leave.py`  
**Class:** `NodeLeave`  
**Entry:** `run_node_leave(target_name)`

## Purpose

Remove a node from the cluster cleanly. Runs on the **master** —
it's the inverse of [`node_join`](node_join.md) as seen from the
cluster's POV.

The target node's local state is NOT touched by this saga; the
operator runs `bedrock node reset` on the target box after this
saga reports success to wipe `/etc/bedrock` and the storage.

## Trigger

`bedrock node leave --target <node-name>` on the master, or
`POST /api/operations` with `kind="node_leave"`. Both submit through
the saga executor backed by rqlite (it's a run-time saga, not a
bootstrap one).

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `target_name` | str | The node to remove |

## Outputs (`ctx`)

The saga writes through to rqlite; ctx itself doesn't carry derived
state forward.

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_target`](#validate_target) | Refuse to leave self; refuse if target unknown; refuse if target is the only mgmt-master |
| 2 | [`rqlite_node_unregister`](#rqlite_node_unregister) | Delete the target's `nodes` row + propagate via revision bump |
| 3 | [`rqlite_voter_remove`](#rqlite_voter_remove) | `DELETE /nodes/<id>` on rqlite to drop the target as a Raft voter |
| 4 | [`propagate_daemon_config`](#propagate_daemon_config) | Update every remaining peer's rqlited.env + restart |
| 5 | [`stop_remote_services`](#stop_remote_services) | SSH to the target and stop bedrock-d + dependents (best-effort) |
| 6 | [`verify_membership_drop`](#verify_membership_drop) | Re-read rqlite and confirm the target is gone from voters + nodes |

## Revert

No inverse — `node_leave` is terminal. To re-add a node after a
leave, run `bedrock join` on it again (which submits a fresh
[`node_join`](node_join.md) saga). The node's loopback_ip will be
re-allocated, and its bedrock_pubkey will be freshly approved by
the operator (treated as a new joiner).

## Idempotency / resume

Each step is idempotent — re-running on a node already removed is
a sequence of no-ops:
- `rqlite_node_unregister` uses `DELETE … WHERE node_name = ?` (no-op if missing)
- `rqlite_voter_remove` retries on 404 (idempotent on the rqlite side)
- `stop_remote_services` uses `systemctl stop` with `--no-block` and tolerates SSH timeouts
- `verify_membership_drop` is a read-only check

If the target node is **unreachable** (the common case for "node
died"), `stop_remote_services` logs a warning and continues; the
voter is already removed from rqlite at that point, so cluster
quorum is restored. The dead node can later run
`bedrock node reset` when it comes back, and then `bedrock join`
to re-enter the cluster.

## Step details

### `validate_target`

Refuses if any of these hold:
- `target_name == self_name` (use `bedrock storage demote-critical`
  to hand off the master role first, then a peer runs node_leave
  against this node)
- Target not in cluster.json's `nodes`
- Target is the ONLY voter (would leave the cluster with no Raft
  quorum)

### `rqlite_node_unregister`

Calls `bedrock_state.node_unregister(target_name)` which:
1. Deletes the row from `nodes`
2. Deletes related rows in `obs_backends`, `tier_critical_membership`,
   `seaweed_master_membership`
3. Bumps `bedrock_meta.revision` so every peer's
   `rqlite_subscriber` wakes and re-projects cluster.json without
   the target

### `rqlite_voter_remove`

`DELETE /nodes/<node_id>` against rqlite's admin API to remove the
target as a Raft voter. Without this, rqlite Raft still expects the
target to vote on every commit and slows down (or stalls if N//2+1
of the remaining voters can't form quorum with the target absent).

This is the step lesson
[`lesson_node_leave_rqlite_remove`](../../../.claude/projects/-home-tommy-projects/memory/lesson_node_leave_rqlite_remove.md)
exists for — earlier versions skipped this and consecutive leaves
bricked the cluster at N/2 voters.

### `propagate_daemon_config`

Renders the new `rqlited.env` (without the target in `-join`) and
SSH-pushes it to every remaining peer, then restarts each peer's
`bedrock-rqlited.service`. Restart is staggered so we don't drop
quorum mid-roll.

### `stop_remote_services`

SSH to the target and `systemctl stop bedrock-d`, then stop the
dependents (rqlited, obs services, seaweed) in dependency order.
If SSH fails (target dead), logs a warning and continues — the
target has already been excised from rqlite + the routing tables.

### `verify_membership_drop`

Re-queries rqlite (`SELECT … FROM nodes WHERE node_name = ?` and
`GET /nodes`) and confirms the target is gone from both. If
either still shows the target, the saga fails — operator must
investigate (usually a stalled subscriber).
