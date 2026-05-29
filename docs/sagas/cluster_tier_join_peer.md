# Saga: `cluster_tier_join_peer`

**Module:** `bedrock_d/install/cluster_tier.py` · **Class:** `ClusterTierJoinPeer`

## Summary

Join this node to the cluster-singleton DRBD resource (`cluster`) as a Secondary,
so DRBD's initial sync carries the master's data — arbiter rqlite, SeaweedFS filer
leveldb3, S3 IAM — onto this node.

- **What:** join as DRBD Secondary; the initial sync pulls the master's
  cluster-singleton data over.
- **Triggers:**
  - `node_join.step_cluster_tier_join_peer` runs it inline as the join's storage
    step (the common path).
  - `POST /api/operations` `{"kind":"cluster_tier_join_peer"}` — standalone, for a
    node that joined while the cluster was still N=1, or operator recovery when a
    peer's DRBD is out of sync.
- **Where:** runs on a non-master node after the master's
  [`cluster_tier_promote_master`](cluster_tier_promote_master.md) has flipped
  `tiers.cluster.mode` to `drbd` in rqlite.
- **End state:** `bedrock-data-cluster` + `bedrock-meta-cluster` LVs exist on this
  node, `/etc/drbd.d/cluster.res` matches the replica set, DRBD is up as Secondary,
  and the initial sync is running (or done). `/etc/bedrock/cluster-drbd-ready` is
  written, marking this node eligible to host the singleton on a future arbiter
  failover. The singleton DRBD is capped at `min(3, N)`-way (lowest-octet nodes); a
  4th+ node logs a skip and returns — it hosts per-VM DRBD + the local weed-volume
  LV, but not the singleton.

### Inputs / outputs (`ctx`)

| key | direction | meaning |
|-----|-----------|---------|
| `wait_timeout_s` | in (param, default 120) | seconds to wait for the master's promote before failing |
| `_peers` | filled by step 1 | `tiers.cluster.peers` names as recorded by the master |
| `_master` | filled by step 1 | current `mgmt_master`; gives the master's loopback for the DRBD link |

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1 | `wait_master_drbd` | Poll rqlite until `tiers.cluster.mode == "drbd"`; stash `_peers` + `_master`. |
| 2 | `join_as_secondary` | Cap to the replica set, then `transition_to_n2_peer`: LV pair + `.res` + `drbdadm up` Secondary. |

## Detail

### 1 · `wait_master_drbd`

Polls `cluster_state.load_cluster()` every 2 s for `tiers.cluster.mode == "drbd"`.
On success, stashes `_peers` (the cluster-tier peer names) and `_master` (current
`mgmt_master`) in ctx so step 2 needn't re-read rqlite. Times out after
`wait_timeout_s` (default 120) and raises — a timeout means the master's
[`cluster_tier_promote_master`](cluster_tier_promote_master.md) never reached
`mode=drbd`; check that saga in the master's `bedrock-d` journal.
**Revert:** none (read-only). **Idempotent:** re-running just polls again.

### 2 · `join_as_secondary`

Rebuilds the peer list from the snapshot (`_peers` names → loopback IPs from
`nodes`, plus self), then caps it with `tier_storage.cap_singleton_peers()` to the
lowest-octet `SINGLETON_MAX_REPLICAS` (= 3) nodes. If self is not in the capped set,
logs and returns (this node is a singleton non-participant). Otherwise calls
`tier_storage.transition_to_n2_peer(self_loopback_ip, master, peers)`, which calls
`join_drbd_peer("cluster", peers)`:

1. `ensure_thin_lv("bedrock-data-cluster", 5G)` + `ensure_meta_lv("bedrock-meta-cluster", …)`
   — create the LV pair if absent.
2. `write_drbd_resource("cluster", peers)` — writes `/etc/drbd.d/cluster.res`
   (mesh path blocks per peer pair); re-caps to 3-way defensively.
3. `drbdadm create-md cluster --force --max-peers=7`.
4. `drbdadm up cluster` — attaches as Secondary; an already-up resource
   ("exists already" / "in use") is treated as success. It does **not** promote —
   the master is Primary; the initial sync starts automatically and pulls the
   master's data over.

Then writes `/etc/bedrock/cluster-drbd-ready` so this node's `cluster_arbiter` may
host the singleton on a later failover. Sync progress is visible via
`drbdadm status cluster` on either side.

**Revert:** manual, without leaving the cluster — `drbdadm down cluster`;
`drbdadm wipe-md cluster`; `lvremove bedrock/bedrock-data-cluster bedrock/bedrock-meta-cluster`.
The master then shows this peer `Connecting` and the cluster keeps running with
singleton redundancy one lower. Leaving the cluster ([`node_leave`](node_leave.md),
run on the master) drops the role automatically.
**Idempotent:** every sub-call guards on existing state — `ensure_*_lv` skips
present LVs, `create-md --force` overwrites stale metadata, `drbdadm up` on an up
resource is a no-op. A crash mid-sync resumes incrementally: DRBD's bitmap tracks
what is already synced, so a re-run is not from-scratch.
