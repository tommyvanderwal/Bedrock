# Saga: `cluster_tier_promote_master`

**Module:** `bedrock_d/install/cluster_tier.py`  
**Class:** `ClusterTierPromoteMaster`

## Purpose

Convert the cluster's critical-tier from a **local thin LV** (N=1
mode) into a **DRBD primary** (N≥2 mode), preserving the on-disk
contents (filer leveldb3, rqlite-arbiter data dir, S3 IAM database)
byte-for-byte via DRBD external metadata.

Runs **on the mgmt-master only** — the node that currently holds
`.254` and the live filer state. Pair this with
[`cluster_tier_join_peer`](cluster_tier_join_peer.md) on the peer
that will become DRBD Secondary.

## Trigger

The `cluster_tier_watcher()` task in `mgmt/orchestrator.py` polls
every 10 s and submits this saga when ALL of:

- `self == mgmt_master` (in cluster.json)
- `len(nodes) >= 2`
- `tiers.critical.mode == "local"`

are true. Submit happens via the saga executor with
`target_node = self`, so the saga runs locally.

The watcher tracks submitted ops in an in-memory set so it
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
| 1 | [`check_preconditions`](#check_preconditions) | Confirm self is still master + critical is still local + peer params are sane |
| 2 | [`promote_local_to_drbd`](#promote_local_to_drbd) | The big move: stop singletons, snapshot leveldb3, umount local LV, create meta LV, write .res, drbdadm create-md/up/primary, mount DRBD device, restore snapshot, update fstab/symlinks, restart singletons |
| 3 | [`record_tier_state_rqlite`](#record_tier_state_rqlite) | Mirror the new `tiers.critical` row into rqlite so every node's projection sees `mode=drbd` |

## Revert

There is no automated cluster_tier demote-to-local saga yet —
`tier_storage.drbd_demote_to_local()` exists as a helper, intended
for the operator's "shrink to N=1 / decommission cluster" path, but
it isn't wrapped as a saga. v1.x candidate.

Until then: if a promote leaves the cluster in a half-state, the
operator can:
1. `drbdadm down tier-critical && drbdadm wipe-md tier-critical` on
   both nodes
2. `umount /var/lib/bedrock/cluster` on the master
3. Restore the old `tier-critical` mount on `/var/lib/bedrock/local/critical`
4. Hand-edit `cluster_info.tiers.critical.mode` back to `local` in
   rqlite

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

Refuses to proceed when:
- `cluster.json:mgmt_master != self_name` — the cluster failed over
  while the watcher was waiting; the new master's watcher will fire
  its own promote.
- Peer is missing from `cluster.json:nodes` — bootstrap window race;
  retry next watcher tick.
- `peer_node` or `peer_loopback` ctx is empty — caller bug.

Sets `ctx["_already_drbd"] = True` and returns silently if the tier
is already in `mode=drbd` (saga becomes a no-op).

### `promote_local_to_drbd`

Wraps `tier_storage.transition_to_n2_master()`. That helper does
the full sequence:

1. Create the meta LV (`bedrock-meta-tier-critical`, thin, 32 MiB)
2. Write `/etc/drbd.d/tier-critical.res` (mesh-aware, full-mesh peer
   blocks)
3. Stop the singletons (filer, s3, arbiter rqlite) so the leveldb3
   is quiescent
4. `cp -a /var/lib/bedrock/cluster/. /var/lib/bedrock-promote-snapshot/`
   to preserve the filer data
5. `umount /var/lib/bedrock/local/critical`
6. `drbdadm create-md --force --max-peers=7` — **skipped if resource
   already configured** (idempotency fix added 2026-05-21)
7. `drbdadm up tier-critical`
8. `drbdadm primary --force tier-critical` (initial sync source)
9. `mount -t xfs /dev/drbd1101 /var/lib/bedrock/cluster` (fresh XFS
   on first promote; existing XFS preserved on re-run)
10. Restore the snapshot back into the DRBD volume
11. Swap fstab: drop the old local-LV line, add the new DRBD line
12. `atomic_symlink /bedrock/critical → /var/lib/bedrock/cluster`
13. Restart the singletons; cluster_arbiter.converge() on the next
    subscriber tick reapplies the full set

### `record_tier_state_rqlite`

Calls `bedrock_state.tier_state(tier="critical", mode="drbd",
master=self, peers=[self, peer], backend_path="/var/lib/bedrock/cluster")`.
Bumps `bedrock_meta.revision` so every peer's rqlite_subscriber
wakes and re-projects cluster.json with the new mode.

The peer's [`cluster_tier_join_peer`](cluster_tier_join_peer.md)
saga is waiting on exactly this — it polls cluster.json for
`tiers.critical.mode == "drbd"`.

## Kopia / backup snapshot compatibility

The layout chosen by this saga keeps LVM thin snapshots usable.
`bedrock-data-tier-critical` is a thin LV in the same pool as VM
disks, so `lvcreate --snapshot --thinpool` captures point-in-time
views without copying blocks. The standard recipe:

```bash
fsfreeze --freeze /var/lib/bedrock/cluster
lvcreate --snapshot -n cluster-snap-$(date +%s) bedrock/bedrock-data-tier-critical
fsfreeze --unfreeze /var/lib/bedrock/cluster
mount -o ro,nouuid /dev/bedrock/cluster-snap-… /mnt/snap
kopia snapshot create /mnt/snap
umount /mnt/snap
lvremove bedrock/cluster-snap-…
```

DRBD is unaware of the snapshot — it's mirroring at the block
layer below LVM.
