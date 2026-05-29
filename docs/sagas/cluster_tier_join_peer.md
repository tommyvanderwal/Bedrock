# Saga: `cluster_tier_join_peer`

**Module:** `bedrock_d/install/cluster_tier.py`  
**Class:** `ClusterTierJoinPeer`

## Purpose

Peer-side counterpart to
[`cluster_tier_promote_master`](cluster_tier_promote_master.md). Runs
on a non-master node once the master has promoted the cluster-singleton
tier (DRBD resource `cluster`) to DRBD. Allocates the peer's local LV
pair, configures DRBD as a Secondary, and lets the initial sync carry
the master's filer leveldb3 + arbiter rqlite data over.

> Note: the [`node_join`](node_join.md) saga has an **inline**
> step also called `cluster_tier_join_peer` that does the same work
> as a final saga step. This standalone saga exists for two cases:
> (a) the orchestrator's reconciler triggers it on a node that
> joined while the master was still N=1, and (b) operator-driven
> recovery when a peer's DRBD got out of sync.

## Trigger

- Submitted by `node_join.step_cluster_tier_join_peer` as an
  inline step at the end of `node_join` (the common path).
- Can also be submitted manually via `POST /api/operations` with
  `kind="cluster_tier_join_peer"`.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `wait_timeout_s` | int (default 120) | How long to wait for the master to finish promoting before failing |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `_peers` | `wait_master_drbd` | The `tiers.cluster.peers` list as recorded by the master |
| `_master` | `wait_master_drbd` | Current `mgmt_master` from the rqlite snapshot |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`wait_master_drbd`](#wait_master_drbd) | Poll rqlite for `tiers.cluster.mode == "drbd"` (master finished its promote) |
| 2 | [`join_as_secondary`](#join_as_secondary) | Cap to the singleton replica set, allocate LV pair, write .res, `drbdadm up` as Secondary, await initial sync |

## Revert

The peer's role goes away automatically when the node leaves the
cluster ([`node_leave`](node_leave.md) on the master). To
manually drop the peer's DRBD attachment without leaving the
cluster, the operator can:

1. `drbdadm down cluster`
2. `drbdadm wipe-md cluster`
3. `lvremove bedrock/bedrock-data-cluster bedrock/bedrock-meta-cluster`

The master then sees the peer as `Connecting` indefinitely in its
DRBD status — the cluster keeps running but cluster-singleton
redundancy drops by one.

## Idempotency / resume

- `wait_master_drbd` is read-only and polls until success or
  timeout.
- `join_as_secondary` wraps `tier_storage.transition_to_n2_peer()`
  which is idempotent — checks for existing LVs/config before
  creating, and `drbdadm up` on an already-up resource is a noop.

If the saga crashes mid-sync, re-running it picks up where it left
off — DRBD's bitmap tracks what's been synced so the resume is
incremental, not from-scratch.

## Step details

### `wait_master_drbd`

Polls the rqlite snapshot (via `cluster_state.load_cluster()`) every
2 s for `tiers.cluster.mode == "drbd"`. Times out after
`wait_timeout_s` seconds (default 120).

Carries forward into ctx:
- `_peers`: the peer list the master wrote — used to rebuild the
  full mesh in the next step.
- `_master`: the current `mgmt_master` — used to figure out which
  loopback to use as the DRBD link target.

Failure mode: timeout means the master's
[`cluster_tier_promote_master`](cluster_tier_promote_master.md)
never completed. Operator checks the master's `bedrock-d` journal
for the promote saga's error.

### `join_as_secondary`

Reads the rqlite snapshot's `tiers.cluster.peers`, builds a full peer
list (every recorded peer + self), then caps it to the singleton
replica set via `tier_storage.cap_singleton_peers()` — the
cluster-singleton DRBD is at most `min(3, N)`-way (lowest-octet
nodes). A 4th+ node not in the capped set logs a skip and returns
(it hosts per-VM DRBD + the weed-volume LV, but not the singleton).
Otherwise it calls `tier_storage.transition_to_n2_peer(self_loopback_ip,
master, peers)`, which:

1. Unmounts the node's local cluster-singleton dir (if mounted) and
   drops the fstab line — the local copy stops being the source of
   truth on this node.
2. Calls `join_drbd_peer("cluster", peers)` which:
   a. Ensures `bedrock-data-cluster` and `bedrock-meta-cluster` LVs
      exist on this peer (creates if not — same size as master)
   b. Writes the same `/etc/drbd.d/cluster.res` as the master
      (mesh blocks for every peer pair)
   c. `drbdadm create-md` if not already configured
   d. `drbdadm up cluster` — becomes Secondary
3. DRBD's initial sync runs in the background, carrying the
   master's data onto this peer. Status visible via
   `drbdadm status cluster` on either side.
