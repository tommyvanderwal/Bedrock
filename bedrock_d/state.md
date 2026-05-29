# bedrock_d/state.py

The single module that owns Bedrock's canonical state I/O. Code imports from
`bedrock_d.state` for two things: typed mutators of cluster-wide state in rqlite
(membership, tiers, DRBD node-ids, operator, observability backends, cluster
name, mgmt master) and per-node bootstrap state in `/etc/bedrock/state.json`. It
is a thin façade — the rqlite transport, the typed mutators, and the local
state.json reader/writer are implemented under `installer/lib/` and re-exported
here, so callers have one import surface and no inline SQL leaks across the
codebase. At import it prepends `installer/` to `sys.path` so the `lib.*` imports
resolve.

## Functions / Classes

All typed mutators below share one shape:
- **In:** typed args (described per function) plus an optional
  `client: RqliteClient | None`. When `client` is omitted, the helper opens its
  own `RqliteClient`, runs the write, and closes it; when passed, the caller owns
  the connection and it is left open.
- **Out:** the new `bedrock_meta.revision` (int). Each mutator writes its typed
  row(s) and bumps the revision counter atomically, so subscribers see a
  consistent snapshot. On error an owned client is closed and the exception
  re-raises.

### rqlite transport (re-exported from `lib.rqlite_client`)
- `RqliteClient`, `AsyncRqliteClient` — sync/async HTTP clients for the rqlite
  store (mTLS).
- `RqliteError`, `RqliteRowError` — exception types; `RqliteRowError` is a
  subclass of `RqliteError`.
- `apply_schema(client, schema_sql_path)` — apply the schema SQL file. **Out:**
  None; runs DDL against rqlite.
- `bump_revision(client) -> int` — increment and return `bedrock_meta.revision`.

### `cluster_init(cluster_uuid, cluster_name, client=None) -> int`
Set the singleton `cluster_info` row at `bedrock init` on the fresh master.
Idempotent — re-runs with unchanged values are no-ops.

### `set_cluster_name(cluster_name, client=None) -> int`
Update `cluster_info.cluster_name` (the display tag projected into state.json and
the mDNS TXT record). `cluster_uuid` is immutable.

### `set_mgmt_master(node_name, client=None) -> int`
Atomically set `cluster_info.mgmt_master` and reconcile node role columns: nodes
holding `mgmt+compute` (other than `node_name`) drop to `compute`, and
`node_name` becomes `mgmt+compute`.

### `node_register(node_name, host, role="compute", pubkey="", bedrock_pubkey="", state="active", client=None) -> int`
Upsert a node row. Preserves the existing `loopback_ip` and the existing `state`
on re-register (a re-register of an active node is not demoted to `joining`).
`state` is the lifecycle gate the election denominator reads.

### `node_set_active(node_name, client=None) -> int`
Flip a node's lifecycle `state` to `active` so it counts toward the election
denominator. Idempotent.

### `node_unregister(node_name, reason="", client=None) -> int`
Drop a node from membership: delete its `nodes` row, delete its
`tier_drbd_node_ids` rows, and remove it from every tier's JSON `peers` array.
Logs the unregister with `reason`.

### `node_loopback(node_name, loopback_ip, client=None) -> int`
Set the node's cluster-identity loopback `/32`. Called once at register-time; the
value is stable for the life of membership.

### `node_maintenance(node_name, on, client=None) -> int`
Set the `nodes.maintenance` flag (`1`/`0`) for a node.

### `tier_state(tier, mode, master=None, peers=None, backend_path=None, client=None) -> int`
Upsert a `tiers` row. `peers` is stored as a JSON array; `version` auto-increments
on every update for use as an optimistic-concurrency token.

### `drbd_node_id_assigned(tier, node_name, node_id, client=None) -> int`
Upsert a permanent DRBD node-id for `(tier, node_name)`. On conflict the same id
is rewritten (refreshing `updated_at`) — re-register never shifts an assignment.

### `drbd_node_id_freed(tier, node_name, node_id, reason="", client=None) -> int`
Delete the `tier_drbd_node_ids` row for `(tier, node_name)`. Logs with `reason`.

### `operator_set(username, salt, password_hash, client=None) -> int`
Upsert an `operators` row (username, salt, password hash).

### `obs_backends_set(metrics, logs, client=None) -> int`
Replace the entire `obs_backends` assignment. `metrics` and `logs` are ordered
lists of node names (up to 2 per stack); the helper clears the table and
re-inserts rows with `stack` and `position` for the dual-write pattern.

### `load_local_state() -> dict` (re-exported from `lib.state.load`)
Read `/etc/bedrock/state.json`.
- **Out:** the parsed dict, or `{}` if the file is missing, empty, truncated, or
  corrupt JSON (so callers can self-heal from cluster.json rather than crash). It
  never unlinks the file.

### `save_local_state(state)` (re-exported from `lib.state.save`)
Atomically persist the per-node bootstrap dict to `/etc/bedrock/state.json`.
- **In:** `state` dict (identity + cold-boot recovery fields).
- **Out:** None; writes via tempfile + `fsync` (data) + `os.replace` + `fsync`
  of the directory fd, so the write is atomic vs concurrent readers and durable
  vs power loss. Raises `RuntimeError` if the dict has neither `bootstrap_done`
  nor `node_name` (refusing to persist a corrupt/empty state).

### `schema_path() -> Path`
Return the on-disk path to `bedrock_schema.sql` (located next to the
`bedrock_state` implementation).
- **Out:** a `Path`; pure computation, no I/O.

## How it works

The module is an import façade plus one computed helper. At import time it
inserts `installer/` onto `sys.path`, then pulls three groups of names from
`lib.*`: the rqlite transport (`lib.rqlite_client`), the typed cluster-state
mutators (`lib.bedrock_state`), and the local state.json reader/writer
(`lib.state`, aliased to `load_local_state` / `save_local_state`). `__all__`
pins the public surface.

Two distinct stores sit behind this one module:

```
  bedrock_d.state
    │
    ├─ cluster-wide  ── rqlite (Raft/SQLite) ── nodes, tiers, cluster_info,
    │   (mutators)        tier_drbd_node_ids, operators, obs_backends, …
    │                     every write bumps bedrock_meta.revision atomically
    │
    └─ per-node      ── /etc/bedrock/state.json
        (load/save)      identity + cold-boot recovery fields
```

Every typed mutator runs the same connection lifecycle via `_client` /
`_bump_and_close`: if no `client` is passed it opens one and owns it; it executes
the write(s) (single statement or a statement batch for multi-row consistency),
then bumps and returns the revision; on any exception an owned client is closed
before re-raising. Because the revision bump is part of the same call, a
subscriber polling `bedrock_meta.revision` re-projects only after the write has
landed.

Local state.json read/write is deliberately defensive. `load_local_state` treats
any unreadable or malformed file as `{}` so a power-loss in the write window
becomes a self-heal opportunity, not a crash. `save_local_state` writes through a
tempfile, fsyncs the data, renames into place, then fsyncs the directory — atomic
against concurrent readers and durable against power loss — and refuses to persist
a dict missing both `bootstrap_done` and `node_name`, which would otherwise turn a
transient empty read into permanent corruption.

## Why

One module owning all rqlite reads/writes keeps SQL out of the rest of the
codebase — callers use typed helpers, and every mutation carries the
revision-bump that downstream snapshot projection depends on. The local
state.json half lives here too because it is the per-node bootstrap material that
recovers rqlite (identity + recovery fields) and is the natural companion to the
cluster-wide store.
