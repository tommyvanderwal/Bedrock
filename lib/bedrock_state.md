# installer/lib/bedrock_state.py

Cluster-state write helpers. This is the single module through which the elected
mgmt-master mutates cluster-wide state in rqlite: node membership, cluster
identity, storage tiers, DRBD node-ids, witnesses, params, dashboard operators,
the join handshake, observability backend assignments, the mesh path table, VM
declared state, and backup targets/history/schedules. Each public function takes
domain arguments (no SQL at the call site), runs the right INSERT/UPDATE/DELETE
against rqlite, bumps `bedrock_meta.revision` so every node's subscriber wakes,
and returns the new revision. Callers are `mgmt/app.py` and the orchestrator/saga
code; the CLI never touches this directly.

This module does **not** check role. Single-writer discipline (only the
mgmt-master writes) is enforced by callers; this module trusts that the caller
verified its role first.

## Functions / Classes

Every public function shares the same client/return contract, so it is stated
once here and not repeated per function:

- **Common In:** `client` (optional `RqliteClient`) — pass one to batch several
  mutations under a shared connection; omit it and the function opens and closes
  its own short-lived client.
- **Common Out:** returns `int`, the new `bedrock_meta.revision`. Side effect:
  one or more rows written in rqlite (through Raft) plus a revision bump. No
  files, no services, no subprocesses. The bump is the wake signal subscribers
  poll.

### Cluster identity

#### `cluster_init(cluster_uuid, cluster_name, client=None) -> int`
Set the singleton `cluster_info` row (id=1). Called once at `bedrock init`.
Idempotent via `ON CONFLICT(id) DO UPDATE`.

#### `set_cluster_name(cluster_name, client=None) -> int`
Update only `cluster_info.cluster_name` (the display tag projected into
`state.json` and the mDNS TXT record); `cluster_uuid` is immutable.

#### `set_mgmt_master(node_name, client=None) -> int`
Atomically record the new master and reconcile per-node roles in one
transaction: set `cluster_info.mgmt_master`, demote every other `mgmt+compute`
node to `compute`, and promote `node_name` to `mgmt+compute`.

### Membership

#### `node_register(node_name, host, role='compute', pubkey='', bedrock_pubkey='', state='active', client=None) -> int`
Upsert a node row.
- **In:** `state` is the lifecycle gate the election denominator reads — register
  a mid-join node as `'joining'` so it does not count toward active nodes until
  it self-activates.
- **Out:** On insert, `loopback_ip` is `''` and `maintenance` is `0`. On conflict
  the existing `loopback_ip` **and** `state` are preserved (the UPDATE branch
  touches only host/role/pubkey/bedrock_pubkey/updated_at) — a re-register never
  demotes an already-active node back to `joining`.

#### `node_set_active(node_name, client=None) -> int`
Flip a node's `state` to `'active'` (the join saga's final step). Idempotent.

#### `node_unregister(node_name, reason='', client=None) -> int`
Drop a node from membership in one transaction: delete its `nodes` row, delete
its `tier_drbd_node_ids` rows, and remove it from each `tiers.peers` JSON array
(via `json_each`/`json_group_array`, only touching tiers that list it). Logs.

#### `node_loopback(node_name, loopback_ip, client=None) -> int`
Set a node's cluster-identity loopback `/32` (written once at register-time).

#### `node_maintenance(node_name, on, client=None) -> int`
Set the `maintenance` flag (1/0) on a node.

### Storage tiers

#### `tier_state(tier, mode, master=None, peers=None, backend_path=None, client=None) -> int`
Upsert a `tiers` row. `peers` is stored as a JSON array. `version` starts at 1
and auto-increments on every conflicting update (an optimistic-concurrency token).

#### `drbd_node_id_assigned(tier, node_name, node_id, client=None) -> int`
Upsert a `tier_drbd_node_ids` row keyed on `(tier_name, node_name)`. Node-ids are
permanent per resource, so a re-register re-writes the same id (no shift).

#### `drbd_node_id_freed(tier, node_name, node_id, reason='', client=None) -> int`
Delete a `tier_drbd_node_ids` row. Logs.

### Witnesses

#### `witness_register(witness_id, addr, witness_pubkey_hex, encrypted_witness_key_hex, backend='echo', client=None) -> int`
Upsert a `witnesses` row. `backend` defaults to `'echo'` (the UDP/12321 path).

#### `witness_unregister(witness_id, reason='', client=None) -> int`
Delete a `witnesses` row. Logs.

### Params

#### `param_change(key, value, client=None) -> int`
Upsert a cluster-wide parameter into `params`. `value` may be any JSON-encodable
type; it is stored as JSON.

### Operators (dashboard auth)

#### `operator_set(username, salt, password_hash, client=None) -> int`
Upsert an `operators` row (salt + password hash).

#### `operator_remove(username, client=None) -> int`
Delete an `operators` row.

### Join handshake

#### `join_request(request_id, node_name, host, bedrock_pubkey, x25519_eph_pubkey, fingerprint, client=None) -> int`
Insert a pending `join_requests` row. `ON CONFLICT(request_id) DO NOTHING`, so a
retried request is not re-opened.

#### `join_resolved(request_id, decision, master_eph_pubkey='', ciphertext='', nonce='', reason='', node_cert_pem='', ca_cert_pem='', client=None) -> int`
Resolve a join request: set `state` to `decision` (`'approved'`/`'rejected'`) and
store the master's ephemeral pubkey, encrypted payload (ciphertext+nonce),
reason, and the joiner's signed `node_cert_pem` + `ca_cert_pem` (so the joiner
can configure rqlited mTLS from the status reply).

### Observability backend assignments

#### `obs_backends_set(metrics, logs, client=None) -> int`
Replace the entire `obs_backends` assignment in one transaction: `DELETE FROM
obs_backends`, then re-insert each `metrics`-stack and `logs`-stack node with its
list `position` (the dual-write pattern, up to 2 nodes per stack).

### Mesh path table

#### `link_up(node_a, nic_a, node_b, nic_b, link_addr_a='', link_addr_b='', speed_mbps=0, rtt_us=0, observed_at=0.0, client=None) -> int`
Record/refresh a continuously-reachable mesh path. Upserts a `paths` row keyed on
the canonical `path_key`; on insert it sets `up_since = observed_at`, on conflict
it refreshes addrs/speed/rtt/observed_at but **not** `up_since`.

#### `link_down(node_a, nic_a, node_b, nic_b, reason='', observed_at=0.0, client=None) -> int`
Delete the `paths` row for the canonical path key. Logs.

#### `link_quality(node_a, nic_a, node_b, nic_b, link_addr_a='', link_addr_b='', speed_mbps=0, rtt_us=0, observed_at=0.0, client=None) -> int`
Update speed/RTT/addrs on an **existing** path only. A bare UPDATE, so a
quality report for a torn-down path matches zero rows and does not resurrect it.

### VMs — declared state and lifecycle

#### `vm_create_intent(name, vm_type, host, ram_mb, disk_gb, requested_by='', client=None) -> int`
Record a pre-create intent. Bumps the revision **first**, then writes a `vms` row
with `state='creating'` and `intent_index` = that revision; returns the revision.

#### `vm_created(name, vm_type, host, ram_mb, disk_gb, client=None) -> int`
Upsert a `vms` row at `state='created'`, clearing `fail_reason`.

#### `vm_create_failed(name, reason, client=None) -> int`
Set a VM's `state='create_failed'` and `fail_reason`.

#### `vm_set_failover_order(name, order, client=None) -> int`
Store the VM's `failover_order` as a JSON array of node_names (index 0 = primary,
1 = secondary, 2 = tertiary for vipet; `[]` for cattle). Read by the failover
orchestrator to decide who is next in line.

#### `vm_set_priority(name, priority, client=None) -> int`
Set the VM's HA `priority`. Any value other than `'low'`/`'normal'`/`'high'` is
coerced to `'normal'`. Read by the self-heal repair loop to order replica
restoration.

#### `drbd_resource_uuid_set(resource_name, uuid, client=None) -> int`
Record a resource's post-promote DRBD `current_uuid` and `uuid_ts_set` in
`drbd_resources`. Called right after `drbdadm primary`, before starting any
service on the disk; a single-statement transaction so quorum confirms before
return.

#### `vm_destroyed(name, reason='', client=None) -> int`
Delete a VM's `vms` row. Logs.

#### `vm_migrated(name, src_host, dst_host, client=None) -> int`
Update a VM's `host` to `dst_host`. (`src_host` is for the caller's context; not
written.)

#### `vm_state_change(name, host, state, client=None) -> int`
Set a VM's `state` (verbatim libvirt label) and, only if `host` is truthy, its
`host` (via `COALESCE(?, host)`).

### Backups — targets, history, schedules

#### `backup_target_set(target_id, kind, *, s3_*, filesystem_path='', override_source_prefix='', cache_directory='', reason='', client=None) -> int`
Upsert a `backup_targets` row. Booleans `s3_disable_tls` /
`s3_disable_tls_verification` are stored as 1/0.

#### `backup_target_removed(target_id, reason='', client=None) -> int`
Delete a `backup_targets` row. Logs.

#### `backup_done(vm, target_id, *, disks=None, source_node='', duration_s=0.0, label='', fs_freeze_used=False, kopia_snapshot_id=None, bytes_added=0, client=None) -> int`
Append one `vm_backups` history row. Bumps the revision first and writes it as
`ts_index`; returns it.
- **In:** pass either `disks=[{target_dev, lv_path, kopia_snapshot_id,
  bytes_added}, …]` or, for a single disk, `kopia_snapshot_id=…` (synthesised
  into a one-element `disk0` list). Raises `ValueError` if both are omitted.
- **Out:** `disks` stored as a normalized JSON array; `primary_kopia_id` =
  first disk's id; `bytes_added` = sum across disks.

#### `backup_failed(vm, target_id, reason, *, source_node='', label='', client=None) -> int`
Set the VM's `last_backup_error` (JSON: `ts_index`, `target_id`, `reason`).
Logs. Note: bumps the revision twice (once to build `ts_index`, once on return).

#### `backup_deleted(vm, target_id, kopia_snapshot_id, reason='', client=None) -> int`
Delete the matching `vm_backups` row (`vm_name`+`primary_kopia_id`+`target_id`).
Logs.

#### `restore_done(vm, target_id, kopia_snapshot_id, *, dest_node='', duration_s=0.0, client=None) -> int`
Set the VM's `last_restore` (JSON: `ts_index`, snapshot id, target, dest_node).

#### `restore_failed(vm, target_id, kopia_snapshot_id, reason, *, dest_node='', client=None) -> int`
Set the VM's `last_restore_err` (JSON: `ts_index`, snapshot id, target, reason).

#### `backup_schedule_set(vm, target_id, cron_expr, *, label_prefix='auto', retention_count=0, reason='', client=None) -> int`
Set the VM's `backup_schedule` (JSON: target, cron_expr, label_prefix,
retention_count, set_at_index).

#### `backup_schedule_removed(vm, reason='', client=None) -> int`
Clear the VM's `backup_schedule` (set to NULL).

### Private helpers

- `_now() -> int` — current Unix epoch seconds, stamped into every row's
  `updated_at`/`created_at`.
- `_client(client) -> (RqliteClient, owns)` — return the passed client with
  `owns=False`, or a fresh one with `owns=True`.
- `_bump_and_close(client, owns) -> int` — bump the revision; close the client
  only if owned.
- `_path_key(node_a, nic_a, node_b, nic_b) -> str` — canonical-order
  `a|nic_a|b|nic_b` key so both endpoints name the same physical path identically.

## How it works

Every public mutator follows one shape:

```
c, owns = _client(client)          # borrow caller's client, or open one
try:
    c.execute(<INSERT/UPDATE/DELETE>)   # → rqlite, through Raft
    return _bump_and_close(c, owns)     # revision +1; close iff owns
except Exception:
    if owns: c.close()             # close-on-error only if we opened it
    raise
```

The `owns` flag is the lifecycle guard: a connection the caller passed for
batching is never closed here; one opened internally is always closed, on both
the success and error paths. A failed write therefore never bumps the revision —
the bump happens only after the SQL succeeds, so a subscriber that wakes can
trust the row is committed.

Bump ordering normally is **mutate then bump** (`_bump_and_close`), so a watcher
that reads the revision and then fetches always sees the just-committed change.
Several functions need the new revision baked **into** the row, so they invert
the order — bump first, then write that revision as a sequence/ordering token:

```
  vm_create_intent  →  intent_index = rev
  backup_done       →  ts_index     = rev
  backup_schedule_set / restore_done / restore_failed  →  index field = rev
```

These do not use `_bump_and_close`; they bump explicitly and use a `finally`
block that only closes (no second bump on the normal path). `backup_failed`
bumps once to build `ts_index` and again on return, so it advances the revision
by two.

Multi-row consistency rides on `RqliteClient.execute([...])` batches, which
commit as a single Raft transaction (all-or-nothing). The functions that exploit
this — `set_mgmt_master`, `node_unregister`, `obs_backends_set` — must land
every row atomically:

```
set_mgmt_master(N):
  ┌ cluster_info.mgmt_master = N
  ├ nodes: role 'mgmt+compute' → 'compute'  WHERE node_name <> N
  └ nodes: role → 'mgmt+compute'            WHERE node_name = N
  one transaction → no reader ever sees two masters or zero masters
```

Idempotency is by design: identity/membership/tier/witness/operator/target
writes are `INSERT … ON CONFLICT … DO UPDATE` upserts, so a saga step re-run is a
no-op write. Two cases guard against clobbering:

- `node_register`'s conflict branch deliberately omits `state` and `loopback_ip`,
  preserving an active node's lifecycle and its allocated `/32` across re-register.
- `link_quality` is a bare `UPDATE` (no insert), so a quality report cannot
  resurrect a path that `link_down` already deleted.

Mesh path writes canonicalise the endpoint pair before keying: if
`(node_a, nic_a) > (node_b, nic_b)` the two sides (and their `link_addr`s) are
swapped, then `_path_key` joins them. Both endpoints observing the same link thus
write the same `path_key` and converge on one `paths` row rather than two.

## Why

The module owns no role logic on purpose: a single elected writer (the
mgmt-master) is the invariant, and centralising that check in the callers that
already know their role keeps this module a pure, reusable set of typed SQL
helpers. Bumping `bedrock_meta.revision` on every mutation is the cluster's
change-notification channel: subscribers poll the counter rather than diffing
tables, so one integer compare tells a node whether to re-project state.
