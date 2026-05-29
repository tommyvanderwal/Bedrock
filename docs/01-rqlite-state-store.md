# Bedrock cluster-state store — rqlite

rqlite is **the** Bedrock cluster-state store. There is no
`bedrock-rust` hash-chained log (deleted in the May-2026 rewrite) and
no `/etc/bedrock/cluster.json` (deleted 2026-05-26). All cluster-wide
topology lives in rqlite; code reads it via
[`cluster_state.load_cluster()`](../installer/lib/cluster_state.py)
(rqlite read-level `none`, so it works even without quorum). The only
per-node local file is `/etc/bedrock/state.json` (this node's
identity/role, written crash-durably with fsync+rename).

## What rqlite is, in 30 seconds

rqlite is a Raft-replicated SQLite. Each node runs one `rqlited`
daemon. They form a Raft cluster, replicating SQLite WALs to each
other. Reads can come from any node; writes go to the elected
leader and replicate. The HTTP API is **HTTPS with mutual TLS**
(per-node `node.crt`/`node.key.pem`/`ca.crt`).

For Bedrock-specific properties:

| | rqlite |
|---|---|
| Wire protocol | HTTP/JSON |
| Engine | SQLite — `sqlite3 raft.db` works for forensics |
| Idle RSS per node | ~25 MB on-disk mode |
| Cluster size | 3 voters required for HA (Raft) |
| License | MIT |

## Cluster shape at each scale

Cluster shapes:

```
N=1 (1-node NAS box):
   per-node rqlite (solo Raft, bootstraps itself)
   • bound to 100.X.Y.1:4001 / :4002 on the node's loopback /32
   • HTTPS mTLS on 4001, Raft on 4002
   • on-disk mode, data at /var/lib/bedrock/rqlite/

N=2 (HA cluster, 2 physical nodes + arbiter):
   per-node rqlite (sim-1 + sim-2) — 2 voters
   arbiter rqlite (.254/32 floating IP, follows mgmt master)
                  — 1 voter
   total = 3 voters, quorum = 2

N=3..N=8 (larger clusters):
   per-node rqlite × N
   arbiter rqlite × 1 (still follows mgmt master)
   total = N + 1 voters
```

The arbiter is NOT a separate machine. It's a second `rqlited`
process co-resident with the elected mgmt master, bound to the
`100.X.Y.254/32` secondary IP on `lo` (HTTPS mTLS on **4011**, Raft
on 4012, so it coexists with the per-node `rqlited` on 4001/4002).
Its data dir lives on the `cluster` singleton DRBD volume so it moves
with the mgmt master on role transitions. See
[`storage-architecture.md`](storage-architecture.md).

## Schema

The single source of truth is
[`installer/lib/bedrock_schema.sql`](../installer/lib/bedrock_schema.sql).
Tables, one-line summary each:

| Table | What's in it |
|---|---|
| `bedrock_meta` | Singleton row with monotonic `revision` counter (replaces the old log_index) |
| `cluster_info` | Singleton row: cluster_uuid, cluster_name, mgmt_master |
| `nodes` | Membership + per-node host, drbd_ip, loopback_ip, role, pubkeys |
| `tiers` | Storage tier modes + masters; companion `tier_drbd_node_ids` for permanent node-id assignments |
| `witnesses` | One row per witness, with `backend` column for future SMB/NFS/S3 backends |
| `vms` + `vm_intents` | VM state + INTENT/OUTCOME work-queue pattern |
| `vm_backups` | Per-VM backup history (multi-disk JSON column) |
| `backup_targets` | Configured Kopia backup targets |
| `paths` | Mesh path table (LINK_UP/DOWN/QUALITY replacement) |
| `operators`, `join_requests`, `params`, `obs_backends` | Login users, join handshake, cluster params, observability backend assignments |

## Writes and the revision counter

Every write goes through one of two surfaces:

- **CLI / FastAPI handlers** call helpers in
  [`installer/lib/bedrock_state.py`](../installer/lib/bedrock_state.py)
  (`node_register`, `tier_state`, `vm_create_intent`, etc.). Each
  helper performs the SQL upsert AND bumps `bedrock_meta.revision`
  inside the same Raft commit.
- **The netd thread** (mesh/election, inside `bedrock-d`) writes path
  entries via the same `bedrock_state` helpers (`link_up` /
  `link_down` / `link_quality`). Master-only — followers don't write
  (D-20 single-writer discipline).

## Reads

Three patterns:

- **`cluster_state.load_cluster()`** — the cluster-wide read entry
  point. Delegates to `view_builder.build_snapshot()` at read-level
  `none` (local replica, works without quorum). This replaces the old
  `cluster.json` file: nothing is cached to disk anymore.
- **`view_builder.build_snapshot()`** — runs SELECTs across all
  tables and assembles the cluster snapshot dict. Used by
  `mgmt/app.py` handlers and the CLI. (The old `rebuild()` projection
  that wrote `/etc/bedrock/cluster.json` is gone; only this node's
  `state.json` is still projected to disk.)
- **`rqlite_client.watch(since_revision)`** — generator that
  polls `bedrock_meta.revision` and yields each new value. Consumed
  by `mgmt/orchestrator.py`'s `rqlite_subscriber` task, which on each
  revision-advance rebuilds the snapshot and runs the reactor on the
  prev→cur diff.

## Forensics

The biggest practical win versus etcd: `sqlite3 raft.db` works
out of the box. On a stuck cluster, SSH into any node, do
`sqlite3 /var/lib/bedrock/rqlite/raft.db ".tables"` and inspect
state directly. SQL is universally legible.

The HTTP API is HTTPS with mutual TLS, so the one-liners below pass
the per-node cert/key/CA:

```sh
# Convenience: rqlite curl with this node's mTLS material.
RQ="curl -fsSL --cert /etc/bedrock/node.crt \
    --key /etc/bedrock/node.key.pem --cacert /etc/bedrock/ca.crt"

# Current cluster identity (the heart of the snapshot):
$RQ 'https://127.0.0.1:4001/db/query?level=strong' \
    -d '["SELECT cluster_uuid, cluster_name, mgmt_master FROM cluster_info WHERE id=1"]'

# How far along is replication on this node?
$RQ https://127.0.0.1:4001/status

# Membership of the rqlite cluster (incl. arbiter):
$RQ https://127.0.0.1:4001/nodes

# Walk the per-VM backup history:
$RQ 'https://127.0.0.1:4001/db/query?level=strong' \
    -d '["SELECT vm_name, label, ts_index, bytes_added FROM vm_backups ORDER BY ts_index DESC LIMIT 50"]'
```

## Upgrade path to PostgreSQL (out of scope for v1.0)

Per D-10: SeaweedFS filer metadata could move from SQLite to
PostgreSQL via `fs.meta.save` / `fs.meta.load`. Bedrock's rqlite
itself doesn't need this — SQLite-on-Raft scales beyond Bedrock's
plausible cluster sizes. The PG escape-hatch is documented for
the SeaweedFS filer specifically.
