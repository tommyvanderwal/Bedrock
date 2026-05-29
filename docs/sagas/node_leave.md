# Saga: `node_leave`

**Module:** `bedrock_d/install/node_leave.py`  
**Class:** `NodeLeave`  
**Entry:** `run_node_leave(target_name, reason="leave", self_name=None)`

## Purpose

Remove a node from the cluster cleanly. Runs on a surviving node
(normally the master) — never on the target itself. It's the inverse
of [`node_join`](node_join.md) as seen from the cluster's POV.

The target node's local state is NOT touched by this saga; the
operator runs `bedrock node reset` on the target box after this
saga reports success to wipe `/etc/bedrock` and the storage.

## Trigger

`bedrock node leave <node-name>` (optionally `--reason <r>`) on a
surviving node. The CLI's `run_node_leave()` entry point drives the
saga through the **file-based** `FileSagaBackend`
(`/var/lib/bedrock/init-progress.json`) — same backend as
`cluster_init` / `node_join` — so a leave survives a crash and resumes
even if rqlite is mid-reconfiguration. (It can also be submitted via
`POST /api/operations` with `kind="node_leave"`, which runs through the
rqlite-backed executor.)

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `target_name` | str | The node to remove |
| `reason` | str | Free-text reason recorded with the unregister (default `"leave"`) |
| `self_name` | str | The node running the saga; used by the self-leave guard (defaults to the local hostname) |

## Outputs (`ctx`)

The saga writes through to rqlite; ctx itself doesn't carry derived
state forward.

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_target`](#validate_target) | Refuse to leave self; no-op if the target is already gone from the snapshot |
| 2 | [`rqlite_node_unregister`](#rqlite_node_unregister) | Delete the target's `nodes` row (+ DRBD node-ids, + tier peer-list) and bump the revision |
| 3 | [`rqlite_voter_remove`](#rqlite_voter_remove) | `DELETE /remove` on rqlite to drop the target as a Raft voter |
| 4 | [`propagate_daemon_config`](#propagate_daemon_config) | Bump the rqlite revision so each remaining peer regenerates its own config |
| 5 | [`stop_remote_services`](#stop_remote_services) | SSH to the target and stop bedrock-d + rqlited + arbiter (best-effort) |
| 6 | [`verify_membership_drop`](#verify_membership_drop) | Re-read rqlite (strong) and confirm the target is gone from voters + nodes |

## Revert

No inverse — `node_leave` is terminal. To re-add a node after a
leave, run `bedrock join` on it again (which submits a fresh
[`node_join`](node_join.md) saga). The node's loopback_ip will be
re-allocated, and its bedrock_pubkey will be freshly approved by
the operator (treated as a new joiner).

## Idempotency / resume

Each step is idempotent — re-running on a node already removed is
a sequence of no-ops:
- `validate_target` sets `already_gone` if the target isn't in the
  snapshot; the unregister/voter-remove/stop steps then short-circuit
- `rqlite_node_unregister` uses `DELETE … WHERE node_name = ?` (no-op if missing)
- `rqlite_voter_remove` is a no-op against rqlite's `/remove` for an
  already-removed voter (200 OK)
- `stop_remote_services` SSHes `systemctl stop` and tolerates SSH timeouts
- `verify_membership_drop` is a read-only check

If the target node is **unreachable** (the common case for "node
died"), `stop_remote_services` logs a warning and continues; the
voter is already removed from rqlite at that point, so cluster
quorum is restored. The dead node can later run
`bedrock node reset` when it comes back, and then `bedrock join`
to re-enter the cluster.

## Step details

### `validate_target`

Reads the cluster snapshot from rqlite (`cluster_state.load_cluster()`)
and:
- Refuses if `target_name == self_name` — the master can't leave
  itself; run `bedrock node leave <target>` from a different node so a
  surviving node drives the removal.
- If the target isn't in the snapshot's `nodes`, sets
  `ctx["already_gone"] = True` and returns — a re-run / partial-retry
  no-ops cleanly rather than failing.
- Otherwise caches the target's host + loopback in ctx, and derives the
  Raft voter id from the loopback's last octet for the next step.

### `rqlite_node_unregister`

Calls `state.node_unregister(target_name)` (skipped if `already_gone`),
which in one transaction:
1. Deletes the target's row from `nodes`
2. Deletes its `tier_drbd_node_ids` rows (DRBD node-id assignments)
3. Filters the target out of every `tiers.peers` JSON array
4. Bumps `bedrock_meta.revision` so every peer's `rqlite_subscriber`
   wakes and re-reads membership without the target

### `rqlite_voter_remove`

`DELETE /remove` against rqlite (mTLS, with the voter id derived in
`validate_target`) to drop the target as a Raft voter. Without this,
rqlite Raft still counts the target's offline node against quorum on
the next election — consecutive leaves without it would brick the
cluster at `N//2` voters (lesson L: node-leave must call rqlite
`/remove`). The server-side `/remove` is idempotent (200 OK for an
already-removed voter).

### `propagate_daemon_config`

Bumps `bedrock_meta.revision`. Each remaining node's `rqlite_subscriber`
regenerates its own daemon config from the new revision on the next
tick — the master does not render or SSH-push config to peers.

### `stop_remote_services`

Best-effort SSH to the target (`StrictHostKeyChecking=no`,
`ConnectTimeout=5`) running `systemctl stop bedrock-d bedrock-rqlited
bedrock-rqlited-arbiter` and clearing `/run/bedrock-no-quorum`. If SSH
fails (target dead — the common case), logs a warning and continues:
the target is already excised from rqlite, and its witness slot ages
out within ~15 s.

### `verify_membership_drop`

Re-queries rqlite with a strong read
(`cluster_state.load_cluster(level="strong")`), polling up to 5 s for
the target to disappear from `nodes` — this bounds the
eventual-consistency window so the operator sees the result rather than
a stale view. If the target still shows after 5 s, it logs a warning
(subscriber may be backed up) but does NOT fail the saga; the cluster
converges eventually.
