# installer/lib/view_builder.py

Materialised-view builder. Reads the cluster-state tables out of rqlite and
assembles them into a single in-memory snapshot dict, plus two projection
helpers that shape that snapshot for the cluster-wide `cluster.json` view and
this node's `state.json` view. The rqlite tables are the canonical store; this
module is the read side that turns rows into the dict structure every consumer
(the mgmt FastAPI app, the orchestrator reactor, the dashboard, CLI verbs)
expects. Callers invoke `build_snapshot()` whenever `bedrock_meta.revision`
advances; on a real cluster the orchestrator's rqlite-watch loop does this.

## Functions / Classes

### `empty_snapshot() -> dict`
Returns the empty snapshot shape so callers don't have to spell out the full
key list.
- **In:** none.
- **Out:** a dict with every snapshot key present and empty — `cluster_name` /
  `cluster_uuid` / `mgmt_master` = `None`; the collections (`nodes`, `tiers`,
  `witnesses`, `params`, `vms`, `backup_targets`, `paths`, `operators`,
  `join_requests`) = `{}`; `obs_backends` = `{"metrics": [], "logs": []}`;
  `log_index` = `0`. No side effects. Matches what `build_snapshot()` returns
  for an empty cluster.

### `build_snapshot(client=None, *, level="weak") -> dict`
Reads every cluster-state table and assembles the full snapshot dict.
- **In:** `client` — an optional `rqlite_client.RqliteClient`; if `None`, one is
  constructed and closed inside the call. `level` — rqlite read-consistency
  knob: `"weak"` (default) reads this node's local Raft follower replica (no
  leader round-trip); `"strong"` routes through the leader for a linearizable
  read, used only when the caller must observe a just-committed write.
- **Out:** a snapshot dict (the `empty_snapshot()` shape, populated). Side
  effects: issues read-only `SELECT` queries against rqlite via the client;
  closes the client only if it constructed it. No files written.

### `fold_into(out, entries) -> dict`
Refreshes a snapshot dict in place from current rqlite state.
- **In:** `out` — a dict to update in place. `entries` — a list (ignored).
- **Out:** builds a fresh snapshot via `build_snapshot()`, clears `out`, copies
  the new snapshot into it, and returns `out`. Side effects: a
  `build_snapshot()` read against rqlite (constructs its own client) and a
  debug log line.

Private helpers: `_cluster_view(v)` shapes a snapshot into the `cluster.json`
dict; `_state_view(v, node_name)` shapes it into this node's `state.json` dict;
`_atomic_write_json(path, obj)` writes JSON via a unique temp file in the target
directory then `os.replace`, unlinking the temp on failure. Module constants
`CLUSTER_JSON` and `STATE_JSON` name `/etc/bedrock/cluster.json` and
`/etc/bedrock/state.json`.

## How it works

`build_snapshot()` owns the client only when the caller passes none; a `finally`
block closes it in that case, so a shared client handed in by the caller is
never closed underneath them.

It starts from `empty_snapshot()` and fills it table by table, every query at
the same `level`, so a missing or empty table leaves the skeleton default
untouched:

```
cluster_info (id=1)   -> cluster_uuid, cluster_name, mgmt_master
bedrock_meta (id=1)   -> revision  ->  out["log_index"]   (int)
nodes                 -> out["nodes"][node_name] = {host, loopback_ip, role,
                         pubkey, bedrock_pubkey, maintenance(bool), state}
tiers                 -> out["tiers"][tier_name] = {mode, version, [master],
                         peers(list), [backend_path]}
tier_drbd_node_ids    -> tier["drbd_node_ids"][node_name] = node_id (int)
witnesses             -> {addr, witness_pubkey, encrypted_witness_key,[backend]}
params                -> out["params"][key] = json-decoded value
operators             -> {salt, hash}
join_requests         -> {node_name, host, bedrock_pubkey, x25519_eph_pubkey,
                         fingerprint, state, ...state-dependent fields}
vms                   -> {vm_type, host, ram_mb, disk_gb, state,
                         failover_order(list), [intent_index],[fail_reason],
                         backup/restore fields}
vm_backups            -> vm["backups"] (newest-first, capped 200)
backup_targets        -> per-target dict
paths                 -> per-path dict (mesh topology)
obs_backends          -> {stack: [node_name, ...]} ordered by position
```

Load-bearing details:

- **JSON columns are decoded defensively.** `tiers.peers`, `params.value`,
  `vms.failover_order`, the VM backup/restore columns, and `vm_backups.disks`
  are stored JSON-encoded. Each decode is wrapped so a `TypeError` or
  `JSONDecodeError` falls back to a sane default (`[]`, the raw value, or the
  field skipped) rather than aborting the whole snapshot.

- **`tiers` then `tier_drbd_node_ids`.** Tier rows land first; the
  DRBD-node-id pass uses `setdefault` so a tier referenced only by a node-id
  row still gets a minimal `{"mode": "local"}` stub. `master` and
  `backend_path` are added only when non-null.

- **`join_requests` carry extra fields by state.** An `approved` request adds
  `master_eph_pubkey`, `ciphertext`, `nonce`, and the mTLS pair
  `node_cert_pem` / `ca_cert_pem` (surfaced to the joiner through
  `cluster.json`). A `rejected` request adds `reason`. Other states carry only
  the base fields.

- **`vm_backups` ordering and cap.** Queried `ORDER BY ts_index DESC LIMIT
  1000`, then appended to each owning VM's `backups` list; once a VM already has
  200 entries, further rows for it are skipped, so each VM keeps its 200 newest
  backups. The `last_restore_err` column maps to the `last_restore_error` key.

- **`obs_backends`** is rebuilt from scratch each call: the query is ordered by
  `(stack, position)`, so per-stack node lists come out in position order.

`_cluster_view(v)` re-emits the cluster-wide fields, keeping `log_index` as the
field name for the rqlite revision. `_state_view(v, node_name)` derives this
node's view: it reads the node's own row for `role` / `mgmt_ip` /
`loopback_ip`, resolves `mgmt_master` to that node's host, and builds `mgmt_url`
as `https://{master_host}:8443` (empty string when there is no master);
`witness_host` is set to the master's host.

`_atomic_write_json` `mkstemp`s a uniquely-named temp file in the destination
directory, writes the indented JSON, then `os.replace`s it onto the target so a
concurrent writer can never observe a half-written or concatenated file; on any
error it unlinks the temp and re-raises.

## Why

The on-disk JSON files are caches over rqlite: any consumer (the mgmt app,
`bedrock storage status`, an operator running `cat`) can read them without a
SQL round-trip. `level="weak"` is the default because the local follower replica
is sub-second fresh and avoids a leader hop on every read; `"strong"` is
reserved for the rare caller that must see its own just-committed write.
