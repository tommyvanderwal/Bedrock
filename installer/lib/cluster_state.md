# installer/lib/cluster_state.py

Single entry point for reading cluster-wide state as a plain dict. It reads the
per-node rqlite replica (not a file on disk) and returns the same dict shape
that consumers expect for cluster-wide configuration. Anything in the codebase
that needs "what are this cluster's witnesses / nodes / VMs / tiers" calls
`load_cluster()`. It is a thin wrapper over `view_builder` — kept under this
name so the import reads as intent ("cluster state") rather than implementation
("view builder").

## Functions / Classes

### `load_cluster(client=None, level="none") -> dict`
Return the cluster-wide state as a dict (cluster.json shape).
- **In:**
  - `client` — optional `rqlite_client.RqliteClient` to share an existing
    connection; if omitted, `view_builder` opens and closes a fresh one for the
    call.
  - `level` — rqlite read consistency. Default `"none"` reads the local replica
    (works without quorum, can lag the Raft leader). Pass `"strong"` to force a
    Raft-leader round-trip.
- **Out:** a dict with keys `cluster_name`, `cluster_uuid`, `mgmt_master`,
  `nodes`, `tiers`, `witnesses`, `params`, `vms`, `backup_targets`, `paths`,
  `operators`, `join_requests`, `obs_backends`, and `log_index` (the rqlite
  revision, named `log_index` for consumer compatibility). Assembled by
  `view_builder.build_snapshot(...)` then shaped by
  `view_builder._cluster_view(...)`. No files written; the only side effect is
  the rqlite read query (a fresh client open/close when `client` is not
  supplied).

## How it works

The function is one composed call:

```
load_cluster(client, level)
        │
        ▼
view_builder.build_snapshot(client=client, level=level)   # runs the SQL,
        │                                                  # returns raw snapshot
        ▼
view_builder._cluster_view(snapshot)                       # reshapes into the
        │                                                  # cluster.json dict
        ▼
      dict  ──►  caller
```

`level="none"` is the load-bearing default: every node holds a full
Raft-replicated copy of the cluster-state tables in its local rqlite store, and
a `none` read answers from that local store without consulting the leader. So a
node partitioned away from the rest of the cluster can still answer questions
about witnesses, VM types, nodes, and tiers.

`level="strong"` forces a leader round-trip. The no-quorum recovery path uses it
after a partition heals so it does not decide between resuming the local paused
VM and destroying it (because a peer took over) against a stale local snapshot.

## Why

Reading rqlite directly at `level="none"` keeps reads available without quorum,
which is the per-node isolation-correctness property the design depends on. This
module owns only cluster-wide state; per-node truth (this node's `cluster_uuid`,
`node_name`, `loopback_ip`, `bootstrap_done`, …) lives in
`/etc/bedrock/state.json` and is never derived here.
