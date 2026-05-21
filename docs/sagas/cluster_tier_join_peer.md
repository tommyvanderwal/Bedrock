# Saga: `cluster_tier_join_peer`

**Module:** `bedrock_d/install/cluster_tier.py`  
**Class:** `ClusterTierJoinPeer`

## Purpose

Peer-side counterpart to
[`cluster_tier_promote_master`](cluster_tier_promote_master.md). Runs
on a non-master node once the master has promoted the critical tier
to DRBD. Allocates the peer's local LV pair, configures DRBD as a
Secondary, and lets the initial sync carry the master's filer
leveldb3 + arbiter rqlite data over.

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
| `_peers` | `wait_master_drbd` | The `tiers.critical.peers` list as recorded by the master |
| `_master` | `wait_master_drbd` | Current `mgmt_master` from cluster.json |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`wait_master_drbd`](#wait_master_drbd) | Poll cluster.json for `tiers.critical.mode == "drbd"` (master finished its promote) |
| 2 | [`join_as_secondary`](#join_as_secondary) | Allocate LV pair, write .res, `drbdadm up` as Secondary, await initial sync |

## Revert

The peer's role goes away automatically when the node leaves the
cluster ([`node_leave`](node_leave.md) on the master). To
manually drop the peer's DRBD attachment without leaving the
cluster, the operator can:

1. `drbdadm down tier-critical`
2. `drbdadm wipe-md tier-critical`
3. `lvremove bedrock/bedrock-data-tier-critical bedrock/bedrock-meta-tier-critical`

The master then sees the peer as `Connecting` indefinitely in its
DRBD status — the cluster keeps running but tier-critical
redundancy drops to 1.

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

Polls cluster.json every 2 s for `tiers.critical.mode == "drbd"`.
Times out after `wait_timeout_s` seconds (default 120).

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

Reads cluster.json's `tiers.critical.peers`, builds a full peer
list (master + every recorded peer + self), and calls
`tier_storage.transition_to_n2_peer()`. That helper:

1. Unmounts `/var/lib/bedrock/local/critical` (if mounted) and
   drops the fstab line — the local LV stops being the primary
   source of truth on this node.
2. Calls `join_drbd_peer("critical", peers)` which:
   a. Ensures `bedrock-data-tier-critical` and
      `bedrock-meta-tier-critical` LVs exist on this peer (creates
      if not — same size as master)
   b. Writes the same `/etc/drbd.d/tier-critical.res` as the master
      (mesh blocks for every peer pair)
   c. `drbdadm create-md` if not already configured
   d. `drbdadm up tier-critical` — becomes Secondary
3. DRBD's initial sync runs in the background, carrying the
   master's data onto this peer. Status visible via
   `drbdadm status tier-critical` on either side.
