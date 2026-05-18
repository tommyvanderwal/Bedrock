# `view_builder.py`

**Module purpose.** Project the rqlite tables into the in-memory
`snapshot` dict that the orchestrator and consumers read, and into
the two on-disk JSON files every node consults:

- `/etc/bedrock/cluster.json` — operator-facing view of the
  cluster, identical on every node (single source of truth: rqlite
  SELECTs).
- `/etc/bedrock/state.json` — this node's POV (role, mgmt_ip,
  loopback_ip, mgmt_url, witness_host).

`build_snapshot(client)` runs SELECTs across every relevant table
and assembles them into one nested dict. `_cluster_view(snapshot)`
and `_state_view(snapshot, node_name)` project that dict to the
two file shapes. `_atomic_write_json` does tmp+rename to avoid
torn writes when concurrent subscribers update concurrently.

The orchestrator's `rqlite_subscriber._apply_revision` is the
only caller in production. The CLI also calls
`rebuild(this_node=...)` ad-hoc.

## Functions

- `empty_snapshot() -> dict` — returns the zero-value shape:
  `{cluster_uuid, cluster_name, mgmt_master, nodes, tiers,
  witnesses, params, vms, backup_targets, paths, operators,
  join_requests, obs_backends, log_index}`. Used as the initial
  `_SNAPSHOT` global before the first rqlite tick.
- `build_snapshot(*, client) -> dict` — runs a series of
  read-only SELECTs against rqlite (cluster_info, nodes, tiers,
  witnesses, params, vms, backup_targets, paths, operators,
  join_requests, obs_backends) and assembles them into the
  snapshot dict. Pulls `bedrock_meta.revision` and stores as
  `log_index` for back-compat.
- `_cluster_view(v) -> dict` — strips the snapshot to the
  operator-visible shape and drops per-node-internal fields
  (e.g. peer_auth challenges). What ends up in cluster.json.
- `_state_view(v, node_name) -> dict` — this-node POV. Copies
  cluster_name/uuid, computes `role` from the matching `nodes`
  row, sets `mgmt_url = https://<master_host>:8443` (used by the
  dashboard + agent_install).
- `_atomic_write_json(path, data)` — `json.dumps` → write to
  `<path>.tmp` → `os.replace` to `<path>`. Avoids the
  concatenated-JSON race that bit us when two subscriber
  passes wrote at once.
- `rebuild(*, this_node) -> dict` — convenience: open an
  rqlite_client.RqliteClient, run `build_snapshot`, return it.
  Used by the CLI's `_propagate_daemon_config` after a write
  to give the operator a fresh snapshot for status output.
- `fold_since(rev, client=None) -> list` — legacy alias for
  rqlite revision diffs; kept for back-compat with older
  callers (the orchestrator no longer uses it).
