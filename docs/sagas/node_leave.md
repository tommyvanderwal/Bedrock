# Saga: `node_leave`

**Module:** `bedrock_d/install/node_leave.py` — `@saga("node_leave")` class `NodeLeave`
**Entry:** `run_node_leave(target_name, reason="leave", self_name=None)`

## Summary

Cleanly removes a node from the cluster. The inverse of
[`node_join`](node_join.md) from the cluster's point of view.

- **What:** drops `<target>` from rqlite membership (`nodes`, DRBD node-ids,
  tier peer-lists), removes its Raft voter slot, and best-effort stops its
  bedrock services.
- **When:** `bedrock node leave <node-name> [--reason <r>]`. Also submittable
  via `POST /api/operations` with `kind="node_leave"`.
- **Where:** runs on a **surviving** node (normally the master) — never on the
  target itself. Operators never run this on the box being removed.
- **End state:** target absent from rqlite `nodes` and from the Raft voter set;
  quorum recomputed without it. The target's own `/etc/bedrock` and storage are
  untouched — wipe them with `bedrock node reset` on that box afterward.

The CLI path drives the saga through the file-based `FileSagaBackend`
(`/var/lib/bedrock/init-progress.json`, shared with `cluster_init` /
`node_join`), so a leave survives a crash and resumes even mid Raft
reconfiguration. The `/api/operations` path runs the same saga through the
rqlite-backed executor.

The saga does **not** re-shuffle the `cluster` singleton's 3-peer DRBD set when
the leaver was carrying it; it logs that redundancy may be below design, and the
orchestrator's reconcile promotes a replacement peer.

| # | Step | What it does |
|---|------|--------------|
| 1 | `validate_target` | Refuse to leave self; no-op if target already absent from snapshot |
| 2 | `rqlite_node_unregister` | One txn: delete `nodes` row + DRBD node-ids + tier peer entries; bump revision |
| 3 | `rqlite_voter_remove` | `DELETE /remove` on rqlite to drop the target's Raft voter slot |
| 4 | `propagate_daemon_config` | Bump the rqlite revision so each surviving peer regenerates its own config |
| 5 | `stop_remote_services` | SSH the target, stop bedrock-d + rqlited + arbiter (best-effort) |
| 6 | `verify_membership_drop` | Strong-read rqlite, confirm target gone from `nodes` |

**ctx in:** `target_name` (node to remove), `reason` (audit text, default
`"leave"`), `self_name` (node running the saga; defaults to local hostname).
**ctx out:** `target_host`, `target_loopback`, `target_voter_id` cached by step 1
for later steps; `already_gone` set when the target is missing. Cluster state is
written through to rqlite, not carried in ctx.

## Detail

### 1. `validate_target`

**Action:** if `target_name == self_name`, raise — the master can't leave itself;
the operator runs the command from a different surviving node. Otherwise read the
snapshot (`cluster_state.load_cluster()`); if the target is absent, set
`already_gone = True` and return. If present, cache `target_host` +
`target_loopback`, and derive `target_voter_id` from the loopback's last octet
(the rqlite voter id is the loopback last octet, stable for the node's life).
**Revert:** none (read-only).
**Idempotency:** `already_gone` short-circuits every later step, so a re-run
no-ops cleanly.

### 2. `rqlite_node_unregister`

**Action:** `state.node_unregister(target_name, reason)` runs one transaction
that (a) deletes the target's `nodes` row, (b) deletes its `tier_drbd_node_ids`
rows, (c) filters the target out of every `tiers.peers` JSON array, then bumps
`bedrock_meta.revision`. The master is the single writer; Raft replicates.
**Revert:** none.
**Idempotency:** all `DELETE … WHERE node_name = ?` / JSON-filter clauses are
no-ops when the target is already absent.

### 3. `rqlite_voter_remove`

**Action:** `DELETE /remove` on rqlite (mTLS via `node.crt`/`node.key.pem`/`ca.crt`,
body `{"id": <voter_id>}`) drops the target as a Raft voter. Skipped if no voter
id was derivable. Required because an offline node's voter slot otherwise counts
against quorum on the next election; consecutive leaves without it brick the
cluster at `N//2` voters.
**Revert:** none.
**Idempotency:** rqlite's `/remove` is a 200-OK no-op for an already-removed
voter.

### 4. `propagate_daemon_config`

**Action:** bump `bedrock_meta.revision`. Each surviving node's
`rqlite_subscriber` regenerates its own daemon config on the revision tick, so
its peer set drops the leaver — the master never renders or SSH-pushes config to
peers.
**Revert:** none.
**Idempotency:** a bump is always safe; subscribers reconcile to current state.

### 5. `stop_remote_services`

**Action:** best-effort SSH (`StrictHostKeyChecking=no`, `BatchMode=yes`,
`ConnectTimeout=5`) running
`systemctl stop bedrock-d bedrock-rqlited bedrock-rqlited-arbiter` and
`rm -f /run/bedrock-no-quorum` on the target. Skipped if no `target_host` or
`already_gone`.
**Revert:** none.
**Idempotency:** stopping an already-stopped service is a no-op. SSH failure
(the common case when the node is dead) is logged and non-fatal — the cluster has
already excised the node, and its witness slot ages out within ~10 s
(`SLOT_STALE_MS`).

### 6. `verify_membership_drop`

**Action:** strong-read (`cluster_state.load_cluster(level="strong")`) up to
10 × 0.5 s = 5 s, waiting for the target to disappear from `nodes`. Bounds the
eventual-consistency window so the operator sees the result, not a stale view.
**Revert:** none (read-only).
**Idempotency:** read-only. If the target still shows after 5 s it logs a warning
(subscriber backed up) but does not fail the saga; the cluster converges.

## Re-adding a node

`node_leave` is terminal — no inverse. To re-add a node, run `bedrock join` on it
(a fresh [`node_join`](node_join.md)): its loopback `/32` is reallocated and its
`bedrock_pubkey` is operator-approved as a new joiner.

A dead/unreachable target is the common case: step 5 logs and continues, but the
voter slot is already removed in step 3, so quorum is restored. The node can later
`bedrock node reset` and `bedrock join` to rejoin.
