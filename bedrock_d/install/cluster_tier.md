# bedrock_d/install/cluster_tier.py

Crash-resumable sagas that bring the **cluster singleton** under DRBD as the
cluster grows past one node. The cluster singleton is the DRBD-replicated volume
backing `/var/lib/bedrock/cluster` — home of the rqlite-arbiter data dir, the
SeaweedFS filer's leveldb3, and the S3 IAM database. One LV pair per node
(`bedrock-data-cluster` + `bedrock-meta-cluster`); DRBD mirrors them
synchronously with the master writable. The rqlite `tiers` row and the DRBD
resource are both keyed `cluster`. Both sagas are thin declarative wrappers — all
LV creation, `drbdadm` calls, snapshot/restore, fstab, and symlink work lives in
`installer/lib/tier_storage.py`. The saga executor runs them one per cluster-size
jump: `cluster_tier_promote_master` is launched on the mgmt-master by the
orchestrator's `cluster_tier_watcher`; `cluster_tier_join_peer` is submitted by a
joiner's `node_join` saga as a blocking follow-up step.

## Functions / Classes

### `class ClusterTierPromoteMaster` — saga `"cluster_tier_promote_master"`
Runs on the mgmt-master when the cluster first reaches N≥2; converts the local
cluster-singleton data into a DRBD primary that peers mirror as they join.
Idempotent at every step (re-running after a crash resumes; completed steps
no-op).
- **In (ctx params, set by orchestrator at submit):** `peer_node` (str) — node
  name of the first peer to mirror to; `peer_loopback` (str) — that peer's
  loopback IP for the DRBD link.
- **Out:** no return; side effects via `tier_storage.transition_to_n2_master`
  (stops singletons, snapshots leveldb3, creates the data+meta LV pair, writes
  `.res`, `create-md`, `drbdadm up`+`primary`, mounts the DRBD device, restores
  the snapshot, updates fstab, restarts singletons, writes the DRBD marker) and
  the rqlite `tiers.cluster` row re-affirmed to `mode=drbd` via `set_tier_state`.

Steps:
- **`check_preconditions`** — reads cluster state; raises if `mgmt_master` is no
  longer this node; if `tiers.cluster.mode` is already `drbd` it sets
  `ctx["_already_drbd"]` (making the later two steps no-op) and returns; raises if
  `peer_node` / `peer_loopback` are missing or `peer_node` is not yet in
  `cluster.json` nodes (bootstrap-window race).
- **`promote_local_to_drbd`** — reads self loopback from `state.json`, calls
  `tier_storage.transition_to_n2_master(self_loopback_ip, peer={name,loopback_ip})`,
  stashes the result in `ctx["_promote_result"]`.
- **`record_tier_state_rqlite`** — re-affirms the `tiers.cluster` row via
  `set_tier_state(CLUSTER_TIER, mode, master, peers, backend_path)` (default
  `write_rqlite=True`) so every node's view-builder fold sees the new mode.

### `class ClusterTierJoinPeer` — saga `"cluster_tier_join_peer"`
Runs on a joiner after the master's promote saga has finished; allocates this
node's LV pair and joins the cluster-singleton DRBD as Secondary, then relies on
the initial sync to carry the master's data over. Idempotent —
`tier_storage.join_drbd_peer` (called inside `transition_to_n2_peer`) checks for
existing LVs and skips creation.
- **In (ctx params):** `wait_timeout_s` (int, default 120) — how long to wait for
  the master to reach `mode=drbd` before failing.
- **Out:** no return; side effects via `tier_storage.transition_to_n2_peer`
  (creates LV pair, writes `.res`, `drbdadm up` as Secondary, writes the DRBD
  marker). No-op (logs and returns) for nodes that fall outside the capped
  replica set.

Steps:
- **`wait_master_drbd`** — polls cluster state every 2 s until
  `tiers.cluster.mode == "drbd"`; on success carries `tiers.cluster.peers` →
  `ctx["_peers"]` and `mgmt_master` → `ctx["_master"]`; raises after
  `wait_timeout_s`.
- **`join_as_secondary`** — rebuilds the peer list (each `_peers` name resolved to
  its `loopback_ip` from cluster nodes, self appended if absent), caps it with
  `cap_singleton_peers`, and if self survives the cap calls
  `transition_to_n2_peer(self_loopback_ip, master={name,loopback_ip}, peers)`. If
  self falls outside the `SINGLETON_MAX_REPLICAS`-way set it logs and returns
  (this node hosts per-VM DRBD + weed-volume but not the singleton).

### Module constants
- `CLUSTER_TIER = "cluster"` — the rqlite `tiers` key and the DRBD resource name.
- `CLUSTER_JSON` = `/etc/bedrock/cluster.json`, `STATE_JSON` = `/etc/bedrock/state.json`.

### Private helpers
- `_load_cluster() -> dict` — returns `cluster_state.load_cluster()`, `{}` on any error.
- `_self_node_name() -> str` — `node_name` from `state.json`, else `socket.gethostname()`.
- `_self_loopback() -> str` — `loopback_ip` from `state.json`, else `""`.
- `_cluster_tier_mode(cluster) -> str` — `tiers.cluster.mode`, default `"local"`.

## How it works

The two sagas form an ordered handshake across the N=1→N=2 cluster-size jump. The
master must finish promoting before any peer can join, because a DRBD Secondary
needs a Primary to sync from. The rqlite `tiers.cluster.mode` field is the shared
latch both sides watch.

```
mgmt-master                                   joiner
-----------                                   ------
cluster_tier_watcher fires
  cluster_tier_promote_master
    check_preconditions
      master? tier local? peer in nodes? ──── (fail → abort, new master retries)
    promote_local_to_drbd
      transition_to_n2_master:
        stop singletons
        snapshot leveldb3
        lvcreate data+meta
        write .res / create-md
        drbdadm up + primary
        mount DRBD dev, restore snap
        fstab, restart singletons
    record_tier_state_rqlite
      tiers.cluster.mode = "drbd" ─────┐
                                        │  node_join saga submits →
                                        └► cluster_tier_join_peer
                                             wait_master_drbd
                                               poll every 2s until mode=drbd
                                               (timeout wait_timeout_s → fail loud)
                                             join_as_secondary
                                               resolve peers, cap to <=3-way
                                               in set?  yes → transition_to_n2_peer
                                                        no  → log + return (skip)
```

Guards that carry the weight:

- `check_preconditions` short-circuits a failover that happened mid-saga: if this
  node is no longer `mgmt_master`, it aborts rather than fighting the new master
  (whose own watcher fires a fresh promote). If the tier is already `drbd`, it
  sets `_already_drbd` so `promote_local_to_drbd` and `record_tier_state_rqlite`
  both no-op — that is the crash-resume path.
- The peer-in-nodes check rejects the bootstrap-window race where the orchestrator
  submitted with a peer name not yet visible in cluster state.
- `wait_master_drbd` makes the join strictly second: it blocks (2 s poll) on the
  master's latch and fails loudly on timeout so the operator notices a master that
  never promoted, rather than a Secondary with no Primary.
- The cluster-singleton replica set is capped at `SINGLETON_MAX_REPLICAS` (3-way,
  lowest-octet nodes) via `cap_singleton_peers`. A 4th-or-later joiner runs the
  saga but `join_as_secondary` detects it fell outside the cap and returns without
  touching DRBD.

Idempotency comes entirely from the `tier_storage` helpers:
`transition_to_n2_master` steps no-op when already applied, `set_tier_state` is
`INSERT OR REPLACE`, and `join_drbd_peer` skips LV creation when the LVs already
exist. Step boundaries are placed so a power loss between steps leaves a
recoverable state.

## Why

`mode=drbd` in rqlite is the single ordering latch, so the master/peer handshake
needs no direct coordination — the peer just polls. The 3-way cap keeps the
arbiter/filer/S3 quorum store on a small, stable lowest-octet set even as the
cluster grows, while the data LV stays a thin LV in the shared pool so
`lvcreate --snapshot --thinpool` backups (Kopia) keep working under DRBD.
