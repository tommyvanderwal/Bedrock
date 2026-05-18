# `bedrock_state.py`

**Module purpose.** Single point of write into rqlite. Every
cluster mutation goes through one of the helpers here — they
build the parametrised SQL, run it in a single rqlite
`/db/execute?transaction` round-trip (so multiple-statement
mutations are atomic), and bump `bedrock_meta.revision` so the
orchestrator's subscriber on every node sees the change.

Mirrors the schema in `bedrock_schema.sql`. Each helper is named
after the table or the conceptual mutation, not the SQL verb.

`_bump_and_close(client, owns)` is the housekeeping at the end of
every write: SELECT max(revision)+1, UPDATE bedrock_meta, close
the client if the caller didn't pass one in.

## Functions

### Cluster init / meta

- `cluster_init(cluster_uuid, cluster_name, client=None) -> int`
  — first-write seed for the `cluster_info` row.
- `set_mgmt_master(node_name, client=None) -> int` — atomic
  update of `cluster_info.mgmt_master` AND the per-node `role`
  column (old master gets demoted to `compute`, new master gets
  `mgmt+compute`). Three statements in one transaction so
  downstream readers never see a mid-transition state.

### Membership

- `node_register(node_name, host, role="compute", pubkey="",
  bedrock_pubkey="", client=None) -> int` — upsert a node row.
  Preserves `loopback_ip` across re-registers (loopback is
  allocated separately by `node_loopback` at join-approval time).
- `node_loopback(node_name, loopback_ip, client=None) -> int` —
  set the per-node loopback /32 (assigned by the master from the
  CGNAT /24 derived from cluster_uuid).
- `node_unregister(node_name, reason="", client=None) -> int` —
  delete the node row + write a log entry to `node_unregisters`.
  Called by `bedrock node leave`.
- `node_set_maintenance(node_name, on, client=None) -> int` —
  toggle the per-node maintenance flag (used by the witness/
  election layer to decide whether a peer's silence is expected).
- `pubkey_set(node_name, pubkey, bedrock_pubkey, client=None) -> int`
  — refresh SSH + Ed25519 pubkeys for a node (used by `bedrock
  node refresh-keys`).

### Witnesses

- `witness_register(witness_id, addr, witness_pubkey,
  encrypted_witness_key_hex, backend="echo", client=None) -> int`
  — operator declares a witness device.
- `witness_unregister(witness_id, client=None) -> int`.

### Tiers + DRBD node-ids

- `tier_state(tier_name, *, mode=None, master=None, peers=None,
  client=None) -> int` — UPSERT into `tiers`. Mode is "local"
  pre-promote, "drbd" post-promote.
- `set_tier_drbd_node_id(tier_name, node_name, node_id,
  client=None) -> int` — persist the per-tier per-node DRBD
  node-id. The composite PK enforces uniqueness so two nodes can't
  accidentally share an id for the same resource.
- `free_tier_drbd_node_id(tier_name, node_name, client=None) -> int`
  — release the id when a node leaves a resource.

### Operators

- `operator_set(username, salt, password_hash, client=None) -> int`
  — set/replace an operator credential. Salt + hash are computed
  by `operator_auth.hash_password`.
- `operator_delete(username, client=None) -> int`.

### VMs / backup_targets / params

- `vm_upsert(name, host, state, **kwargs) -> int` — VM row write.
- `vm_delete(name, client=None) -> int`.
- `backup_target_set(target_id, kind, ...) -> int` /
  `backup_target_delete(target_id, client=None) -> int`.
- `param_set(key, value, client=None) -> int` — generic key/value
  table for cluster-wide flags / feature switches.

### Observability backends

- `obs_backends_set(metrics, logs, client=None) -> int` — write
  the comma-separated list of node_names that host VictoriaMetrics
  / VictoriaLogs storage. `observability.reconcile` reads this.

### Join requests

- `join_request_insert(request_id, node_pubkey, sender_host,
  challenge, client=None) -> int`.
- `join_request_approve(request_id, approver_username, client=None)
  -> int` — state transition pending → approved.
- `join_request_reject(request_id, approver_username, reason="",
  client=None) -> int`.

### Internals

- `_client(client) -> (client, owns)` — accept caller-supplied
  client OR open a new one. The `owns` flag tells the caller to
  close it.
- `_now() -> int` — `int(time.time())`; centralised so the test
  suite can monkey-patch.
- `_bump_and_close(client, owns) -> int` — UPDATE bedrock_meta
  SET revision = revision + 1, close if owns, return the new
  revision. Every public mutation calls this last.
