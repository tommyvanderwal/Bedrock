# Saga: `cluster_tier_promote_master`

**Module:** `bedrock_d/install/cluster_tier.py`  
**Class:** `ClusterTierPromoteMaster`

## Purpose

Convert the cluster-singleton tier (DRBD resource `cluster`) from a
**local directory on the root FS** (N=1 mode) into a **DRBD primary**
(N≥2 mode), preserving the on-disk contents (filer leveldb3,
rqlite-arbiter data dir, S3 IAM database, cluster CA) byte-for-byte
via DRBD external metadata.

Runs **on the mgmt-master only** — the node that currently holds
`.254` and the live filer state. Pair this with
[`cluster_tier_join_peer`](cluster_tier_join_peer.md) on the peer
that will become DRBD Secondary.

## Trigger

The `cluster_tier_watcher()` task in `mgmt/orchestrator.py` polls
every 10 s (reading the rqlite snapshot via
`cluster_state.load_cluster()`) and submits this saga when ALL of:

- `self == mgmt_master`
- `len(nodes) >= 2`
- `tiers.cluster.mode == "local"`

are true. Submit happens via the saga executor with
`target_node = self`, so the saga runs locally.

The watcher tracks the submitted peer key in an in-memory set so it
won't re-submit while a prior op is in flight or completed.

## Inputs (`ctx`)

| key | type | meaning |
|-----|------|---------|
| `peer_node` | str | Name of the first peer to mirror to (lowest-octet other node) |
| `peer_loopback` | str | The peer's `100.X.Y.Z/32` for the DRBD link address |

## Outputs (`ctx`)

| key | filled by | meaning |
|-----|-----------|---------|
| `_already_drbd` | `check_preconditions` | Short-circuit flag if the tier is already mode=drbd |
| `_promote_result` | `promote_local_to_drbd` | Echo of `transition_to_n2_master`'s return dict |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`check_preconditions`](#check_preconditions) | Confirm self is still master + the `cluster` tier is still local + peer params are sane |
| 2 | [`promote_local_to_drbd`](#promote_local_to_drbd) | The big move: stop singletons, snapshot leveldb3, umount local LV, create meta LV, write .res, drbdadm create-md/up/primary, mount DRBD device, restore snapshot, update fstab/symlinks, restart singletons |
| 3 | [`record_tier_state_rqlite`](#record_tier_state_rqlite) | Mirror the new `tiers.cluster` row into rqlite so every node sees `mode=drbd` |

## Revert

There is no automated cluster_tier demote-to-local saga yet —
`tier_storage.drbd_demote_to_local()` exists as a helper, intended
for the operator's "shrink to N=1 / decommission cluster" path, but
it isn't wrapped as a saga. v1.x candidate.

Until then: if a promote leaves the cluster in a half-state, the
operator can:
1. `drbdadm down cluster && drbdadm wipe-md cluster` on both nodes
2. `umount /var/lib/bedrock/cluster` on the master
3. Restore the cluster-singleton dir back onto the root FS at
   `/var/lib/bedrock/cluster`
4. Hand-edit the `cluster` row in rqlite's `tiers` table back to
   `mode = local`

## Idempotency / resume

- `check_preconditions` is read-only.
- `promote_local_to_drbd` wraps `transition_to_n2_master` which is
  *mostly* idempotent — see
  [`transition_to_n2_master`](#promote_local_to_drbd) below for the
  one case where it wasn't (and was fixed: `drbdadm create-md` now
  skips when the resource is already configured).
- `record_tier_state_rqlite` is `INSERT OR UPDATE`.

If the saga crashes mid-`promote_local_to_drbd` (e.g. between
`drbdadm primary` and `mount`), re-running the saga walks
`transition_to_n2_master` again; each sub-step's idempotency check
no-ops the work already done and the surviving steps complete. The
data already on the LV (filer leveldb3 + arbiter rqlite) survives
because external metadata leaves the data LV byte-untouched until
mount.

## Step details

### `check_preconditions`

Reads the cluster snapshot from rqlite (`cluster_state.load_cluster()`)
and refuses to proceed when:
- `mgmt_master != self_name` — the cluster failed over while the
  watcher was waiting; the new master's watcher will fire its own
  promote.
- Peer is missing from `nodes` — bootstrap window race; retry next
  watcher tick.
- `peer_node` or `peer_loopback` ctx is empty — caller bug.

Sets `ctx["_already_drbd"] = True` and returns silently if the tier
is already in `mode=drbd` (saga becomes a no-op).

### `promote_local_to_drbd`

Wraps `tier_storage.transition_to_n2_master()` →
`promote_local_to_drbd_master("cluster", peers)`. Short-circuits to a
no-op if the resource is already `Primary` and mounted. Otherwise the
full sequence is:

1. Create the data + meta LV pair (`bedrock-data-cluster`,
   `bedrock-meta-cluster`; meta sized by the standard DRBD9 formula,
   `--max-peers=7`)
2. Write `/etc/drbd.d/cluster.res` (mesh-aware, full-mesh peer blocks)
3. Stop the singletons (`bedrock-weed-s3`, `bedrock-weed-filer`,
   `bedrock-rqlited-arbiter`) so the leveldb3 is quiescent
4. `cp -a /var/lib/bedrock/cluster/. /var/lib/bedrock-promote-snapshot/`
   to preserve the existing singleton data
5. `umount /var/lib/bedrock/cluster` if it's a mountpoint (at N=1 it's
   just a dir on the root FS, so usually a no-op)
6. `drbdadm create-md … --force --max-peers=7` + `drbdadm up cluster`
   — both skipped if the resource is already configured (DRBD9 refuses
   create-md/up on a configured device; this lets the step re-run safely)
7. `drbdadm primary --force cluster` (initial sync source; idempotent)
8. `mount -t xfs /dev/drbd1101 /var/lib/bedrock/cluster` (mkfs.xfs on
   first promote; existing XFS preserved on re-run)
9. Restore the snapshot back into the DRBD volume, then `rm` the snapshot
10. Swap fstab: drop any prior `/var/lib/bedrock/cluster` line, add the
    DRBD line
11. Restart the singletons; `cluster_arbiter.converge()` on the next
    subscriber tick reapplies the full set

### `record_tier_state_rqlite`

Re-affirms the `cluster` tier's rqlite row (`tier_storage.set_tier_state`
with `mode="drbd"`, the master, peers, and `backend_path =
/var/lib/bedrock/cluster`). `transition_to_n2_master` already wrote it;
this re-affirmation bumps `bedrock_meta.revision` so every peer's
`rqlite_subscriber` wakes and sees the new mode.

The peer's [`cluster_tier_join_peer`](cluster_tier_join_peer.md)
saga is waiting on exactly this — it polls rqlite for
`tiers.cluster.mode == "drbd"`.

## Kopia / backup snapshot compatibility

The layout chosen by this saga keeps LVM thin snapshots usable.
`bedrock-data-cluster` is a thin LV in the same pool as VM disks, so
`lvcreate --snapshot --thinpool` captures point-in-time views without
copying blocks. The standard recipe:

```bash
fsfreeze --freeze /var/lib/bedrock/cluster
lvcreate --snapshot -n cluster-snap-$(date +%s) bedrock/bedrock-data-cluster
fsfreeze --unfreeze /var/lib/bedrock/cluster
mount -o ro,nouuid /dev/bedrock/cluster-snap-… /mnt/snap
kopia snapshot create /mnt/snap
umount /mnt/snap
lvremove bedrock/cluster-snap-…
```

DRBD is unaware of the snapshot — it's mirroring at the block
layer below LVM.
