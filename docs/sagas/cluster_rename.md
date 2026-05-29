# Saga: `cluster_rename`

**Module:** `bedrock_d/cluster/rename.py`  
**Class:** `ClusterRename`

## Purpose

Change the cluster's display name. The cluster's real identity is
`cluster_uuid` (immutable for the cluster's life); this saga
updates the friendly `cluster_name` tag that surfaces in the
dashboard, `bedrock status`, the mDNS TXT record, and every node's
`state.json` projection.

## Trigger

- `bedrock cluster rename <new-name>` CLI, or
- `POST /api/operations` with `kind="cluster_rename"` and
  `params={"new_name": "<…>"}`.

The CLI dials the local mgmt API (`http://127.0.0.1:8001/api/operations`),
which submits the saga via the executor and waits for completion.

## Inputs (`ctx`)

| key | required | type | meaning |
|-----|----------|------|---------|
| `new_name` | required | str | New display tag, 1..64 chars from `[A-Za-z0-9_.-]`. Whitespace is stripped. |

## Outputs (`ctx`)

None — the saga is a pure write through rqlite; downstream
projection handles every consumer.

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`validate_request`](#validate_request) | Strip whitespace; refuse empty / over-length / non-allowed-char names |
| 2 | [`write_rqlite_cluster_info`](#write_rqlite_cluster_info) | Single UPDATE on `cluster_info.cluster_name`; bumps `bedrock_meta.revision` |

## Revert

Run the saga again with the previous name. There is no special
inverse — rename is symmetric.

## Idempotency / resume

- `validate_request` is pure-function; re-running is a no-op.
- `write_rqlite_cluster_info` is `UPDATE … SET cluster_name = ?`.
  Re-running with the same name is a no-op write at the row level;
  the revision still bumps and downstream subscribers re-project
  the unchanged value. Harmless — at most one extra subscriber
  tick.

## Propagation

The saga only writes rqlite. Downstream consumers update
automatically:

| Consumer | Path | Latency |
|---|---|---|
| `state.json` on every node | `rqlite_subscriber` → `view_builder._state_view` | ≤2 s |
| mDNS TXT record | `mdns_responder.cluster_identity()` polls state.json every 60 s | ≤60 s |
| Dashboard / `bedrock status` | reads rqlite directly via `cluster_state.load_cluster()` per request | next request |

(`cluster.json` is no longer a steady-state projection — that layer
was removed 2026-05-26; consumers read rqlite directly. Only
`state.json` is re-projected on each revision.)

Nothing keys off `cluster_name` for routing or security; the
`cluster_uuid` is the load-bearing field everywhere
(mesh-loopback prefix derivation, peer-auth gate, witness AEAD
nonce derivation, DRBD resource names, …). Rename is therefore
truly cosmetic.

## Step details

### `validate_request`

Strips whitespace from `new_name`, then refuses if:
- empty, or
- `re.fullmatch(r"^[A-Za-z0-9_.-]{1,64}$", new_name)` is False.

The allowed-char policy avoids characters that would need
escaping in JSON state files, the mDNS TXT record, shell-rendered
log lines, or systemd unit paths.

### `write_rqlite_cluster_info`

Calls `bedrock_state.set_cluster_name(new_name)` which is a single
`UPDATE cluster_info SET cluster_name = ?, updated_at = ? WHERE id = 1`
wrapped in the standard `_bump_and_close` so `bedrock_meta.revision`
ticks forward. The bump is what wakes every node's
`rqlite_subscriber` task — the subscriber then re-projects
`state.json` via `view_builder._state_view`. Other consumers read
the new name straight from rqlite on their next request.
