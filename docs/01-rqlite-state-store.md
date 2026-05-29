# Bedrock cluster-state store — rqlite

rqlite is **the** Bedrock cluster-state store. All cluster-wide topology
(membership, tiers, VMs, DRBD resources, witnesses, sagas, …) lives in it.
Code reads it through
[`cluster_state.load_cluster()`](../installer/lib/cluster_state.py) at rqlite
read-level `none`, so reads work even without quorum: every node carries a
full Raft-replicated copy and answers locally.

Two per-node local files sit alongside rqlite. Neither is a runtime projection
of cluster state:

- `/etc/bedrock/state.json` — this node's identity + role + mgmt URL, written
  crash-durably (fsync + rename, plus a directory fsync so the rename survives
  power loss). The orchestrator re-projects this node's role + `mgmt_url` on
  each rqlite revision; if it is lost to a 0-byte truncation,
  `rqlite_setup.py` self-heals identity from `cluster.json` + hostname before
  rendering the rqlited env.
- `/etc/bedrock/cluster.json` — bootstrap-only: the rqlite peer list (node
  names + loopback `/32`s). [`rqlite_setup.py --render-env`](../installer/lib/rqlite_setup.py)
  reads it at every boot to build the `-join` / `-bootstrap-expect` flags,
  because rqlite can't report its own peers before it starts.

## What rqlite is, in 30 seconds

rqlite is Raft-replicated SQLite. Each node runs one `rqlited` daemon; together
they form a Raft cluster replicating SQLite to each other. Reads can be served
by any node; writes forward to the elected leader and replicate. The HTTP API
is **HTTPS with mutual TLS** (per-node `node.crt` / `node.key.pem` / `ca.crt`).

| | rqlite |
|---|---|
| Wire protocol | HTTP/JSON |
| Engine | SQLite, on-disk — inspectable with `sqlite3` |
| Idle RSS per node | ~25 MB |
| Cluster size | 3 voters for HA (Raft majority) |
| License | MIT |

## Cluster shape at each scale

```
N=1 (single NAS box):
   one rqlited (solo Raft, self-bootstraps with -bootstrap-expect 1)
   • binds 0.0.0.0:4001/:4002, advertises the node's loopback /32
   • HTTPS mTLS on 4001, Raft on 4002
   • data at /var/lib/bedrock/rqlite/

N=2 (HA: 2 physical nodes + arbiter):
   per-node rqlited × 2 ............ 2 voters
   arbiter rqlited (on the .254 VIP)  1 voter
   total = 3 voters, quorum = 2

N=3..N=8:
   per-node rqlited × N ............ N voters
   arbiter rqlited × 1 ............. 1 voter
   total = N+1 voters
```

The arbiter is not a separate machine. It is a second `rqlited` process
co-resident with the elected mgmt master, bound to the `100.X.Y.254/32`
secondary IP on `lo`, on HTTPS mTLS **4011** / Raft **4012** so it coexists
with the per-node `rqlited` on 4001/4002. Its node-id is fixed at 254 (matching
the VIP octet) and its data dir lives on the `cluster` singleton DRBD volume
(`/var/lib/bedrock/cluster/rqlite`), so it moves with the mgmt master on role
transitions. `cluster_arbiter.promote_to_arbiter_host()` claims the `.254/32`,
renders `rqlited-arbiter.env`, and starts `bedrock-rqlited-arbiter`; that unit
always `-join`s the per-node peers (local peer first), never bootstraps. Its
mTLS material lives on the cluster volume at
`/var/lib/bedrock/cluster/ca/arbiter.{crt,key.pem}` + `ca.crt`. See
[`storage-architecture.md`](storage-architecture.md).

Per-node `rqlited` uses the loopback `/32`'s last octet as its Raft node-id —
permanent and unique per node, so adding a node never shifts another's id.

## Schema

Single source of truth:
[`installer/lib/bedrock_schema.sql`](../installer/lib/bedrock_schema.sql)
(applied idempotently by `rqlite_client.apply_schema`, `CREATE TABLE IF NOT
EXISTS` throughout, with additive `ALTER TABLE` migrations for newer columns).

| Table | What's in it |
|---|---|
| `bedrock_meta` | Singleton row with the monotonic `revision` counter watchers poll |
| `cluster_info` | Singleton row: `cluster_uuid`, `cluster_name`, `mgmt_master` |
| `nodes` | Membership: host, loopback_ip, role, pubkeys, maintenance, lifecycle state |
| `tiers` + `tier_drbd_node_ids` | Storage tier mode/master/peers + permanent per-tier DRBD node-ids |
| `drbd_resources` | One row per DRBD resource (cluster + per-VM): LV pair, minor, peers, last-known `current_uuid` |
| `cluster_drbd_membership`, `seaweed_master_membership` | The capped-3 cluster-singleton DRBD set and the Raft-3 SeaweedFS master set |
| `witnesses` | One row per witness; `backend` column (`echo`/`smb`/`s3`) |
| `vms` + `vm_intents` | VM declared/runtime state + INTENT/OUTCOME work queue |
| `vm_backups`, `backup_targets` | Per-VM backup history (multi-disk JSON) + configured Kopia targets |
| `paths` | Mesh path table (per link: NICs, addrs, speed, RTT, observed_at) |
| `operations` + `operation_steps` | Generic crash-safe saga journal (intent → idempotent steps → done) |
| `operators`, `join_requests`, `params`, `obs_backends` | Login users, join handshake, cluster params, observability backend assignments |

Conventions: every table carries `updated_at` (epoch seconds); JSON columns
hold variable-cardinality lists/maps (tier peers, failover order, backup
disks).

## Writes and the revision counter

Every write goes through one of two surfaces, and bumps
`bedrock_meta.revision` last (so a watcher that reads the new revision then
fetches sees the committed mutation):

- **CLI / FastAPI handlers** call helpers in
  [`installer/lib/bedrock_state.py`](../installer/lib/bedrock_state.py)
  (`node_register`, `tier_state`, `vm_create_intent`, `set_mgmt_master`, …).
  Each helper does the SQL upsert then `rqlite_client.bump_revision()`.
- **The netd thread** (mesh/election, inside `bedrock-d`) writes mesh path
  entries via the same helpers (`link_up` / `link_down` / `link_quality`).
  Master-only — followers don't write (single-writer discipline).

## Reads

Three patterns:

- **`cluster_state.load_cluster(level="none")`** — the cluster-wide read entry
  point; delegates to `view_builder.build_snapshot()` against the local replica
  (works without quorum). Pass `level="strong"` to force a leader round-trip
  when a decision must not run against a stale snapshot (e.g. the no-quorum
  recovery path deciding resume-vs-destroy on a paused VM).
- **`view_builder.build_snapshot()`** — runs the SELECTs across all tables and
  assembles the cluster snapshot dict. Used by `mgmt/app.py` handlers and the
  CLI. Nothing is cached to disk; the snapshot IS the database.
- **`rqlite_client.watch(since_revision)`** — generator polling
  `bedrock_meta.revision` (~500 ms) and yielding each new value. Consumed by
  `mgmt/orchestrator.py`'s `rqlite_subscriber`, which on each advance rebuilds
  the snapshot, re-projects `state.json`, and runs the reactor on the
  prev→cur diff.

## Forensics

The practical win: `sqlite3` reads the on-disk store directly. On a stuck
cluster, SSH any node and inspect `/var/lib/bedrock/rqlite/` (the SQLite file
there) with `.tables` / plain `SELECT`s — no special tooling.

The HTTP API is HTTPS with mutual TLS, so the one-liners below pass the
per-node cert/key/CA:

```sh
# Convenience: rqlite curl with this node's mTLS material.
RQ="curl -fsSL --cert /etc/bedrock/node.crt \
    --key /etc/bedrock/node.key.pem --cacert /etc/bedrock/ca.crt"

# Current cluster identity (the heart of the snapshot):
$RQ 'https://127.0.0.1:4001/db/query?level=strong' \
    -d '["SELECT cluster_uuid, cluster_name, mgmt_master FROM cluster_info WHERE id=1"]'

# Replication progress on this node:
$RQ https://127.0.0.1:4001/status

# rqlite cluster membership (incl. the arbiter):
$RQ https://127.0.0.1:4001/nodes

# Walk the per-VM backup history:
$RQ 'https://127.0.0.1:4001/db/query?level=strong' \
    -d '["SELECT vm_name, label, ts_index, bytes_added FROM vm_backups ORDER BY ts_index DESC LIMIT 50"]'
```

## PostgreSQL escape hatch (SeaweedFS filer only)

Bedrock's rqlite does not need PostgreSQL — SQLite-on-Raft scales past any
plausible Bedrock cluster size. Only the **SeaweedFS filer** metadata has a
documented PG escape hatch (`fs.meta.save` / `fs.meta.load`), out of scope for
v1.0.
