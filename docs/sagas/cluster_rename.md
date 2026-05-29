# Saga: `cluster_rename`

**Module:** `bedrock_d/cluster/rename.py` — class `ClusterRename`

## Summary

Changes the cluster's display tag. The cluster's real identity is
`cluster_uuid` (immutable for the cluster's life); this saga only updates the
friendly `cluster_name` in rqlite's `cluster_info` row. Rename is therefore
cosmetic — nothing keys off the name for routing or security. The
`cluster_uuid` is the load-bearing field everywhere (mesh-loopback prefix
derivation, peer-auth gate, witness AEAD, DRBD resource names).

- **What:** single rqlite write to `cluster_info.cluster_name`.
- **Where:** runs on the node that received the operation (the saga executor
  on that `bedrock-d`). The write goes to rqlite, so it is cluster-wide.
- **Trigger:** `bedrock cluster rename <new-name>`, or
  `POST /api/operations` with `kind="cluster_rename"`,
  `params={"new_name": "<…>"}`. The CLI dials the local mgmt API
  (`http://127.0.0.1:8001/api/operations`) with `wait: True` and reports the
  returned `state`/`op_id`.
- **End state:** `cluster_info.cluster_name = <new-name>`,
  `bedrock_meta.revision` bumped; the new name surfaces in `state.json` on
  every node (≤2 s), the dashboard (next request), and the mDNS TXT record
  (≤60 s).

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_request`](#validate_request) | Strip whitespace; refuse empty / over-length / disallowed-char names |
| 2 | [`write_rqlite_cluster_info`](#write_rqlite_cluster_info) | One `UPDATE` on `cluster_info.cluster_name`; bumps `bedrock_meta.revision` |

**Inputs (`ctx`)**

| key | required | type | meaning |
|-----|----------|------|---------|
| `new_name` | yes | str | New display tag, 1..64 chars from `[A-Za-z0-9_.-]`; whitespace stripped |

**Outputs (`ctx`):** none. The saga is a pure rqlite write; downstream
projection handles every consumer.

**Revert:** run the saga again with the previous name — rename is symmetric,
no inverse step.

## Detail

### `validate_request`

Strips whitespace from `ctx["new_name"]`, then raises `ValueError` if the
result is empty or fails `re.fullmatch(r"^[A-Za-z0-9_.-]{1,64}$", …)`. The
canonicalised (stripped) value is written back into `ctx`.

- **Why this charset:** avoids characters that would need escaping in JSON
  state files, the mDNS TXT record, shell-rendered log lines, or systemd unit
  paths. Operators wanting a fancier label format it client-side.
- **Revert:** none — pure validation, no side effects.
- **Idempotency:** pure function; re-running is a no-op. Runs first so the
  saga fails before touching rqlite.

### `write_rqlite_cluster_info`

Calls `bedrock_d.state.set_cluster_name(new_name)`, which runs
`UPDATE cluster_info SET cluster_name = ?, updated_at = ? WHERE id = 1`
wrapped in `_bump_and_close` so `bedrock_meta.revision` ticks forward, then
logs the new name and revision.

- **Revert:** re-run the saga with the old name (a further `UPDATE`).
- **Idempotency:** re-running with the same name is a no-op at the row level;
  the revision still bumps and subscribers re-project the unchanged value.
  Harmless — at most one extra subscriber tick.

## Propagation

The revision bump wakes every node's `rqlite_subscriber`
(`mgmt/orchestrator.py`), which folds a fresh snapshot and projects to disk.

```
set_cluster_name → cluster_info row + bedrock_meta.revision++
                          │
        rqlite_subscriber._apply_revision  (each node, ~2 s)
                          │
                view_builder._state_view → state.json (atomic write)
```

| Consumer | Path | Latency |
|---|---|---|
| `state.json` on every node | `rqlite_subscriber` → `_apply_revision` → `view_builder._state_view` (atomic write) | ≤2 s |
| `bedrock status` | reads `cluster_name` from `state.json` | after state.json projection (≤2 s) |
| Dashboard / mgmt API | `cluster_state.load_cluster()` reads rqlite per request | next request |
| mDNS TXT record | `mdns_responder.cluster_identity()` re-reads `state.json` on its 60 s refresh tick | ≤60 s |

`state.json` is the only per-node file re-projected on each revision; it holds
the fields a node needs at cold boot without rqlite (identity, role, master
URL). All other consumers query rqlite directly via
`cluster_state.load_cluster()` (read level `none`, so it works without quorum).
