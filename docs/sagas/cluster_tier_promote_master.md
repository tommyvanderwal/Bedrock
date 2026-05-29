# Saga: `cluster_tier_promote_master`

**Module:** `bedrock_d/install/cluster_tier.py` — class `ClusterTierPromoteMaster`

## Summary

Converts the cluster-singleton tier (DRBD resource + tier name `cluster`,
mounted at `/var/lib/bedrock/cluster`) from a plain directory on the root FS
(N=1) into a **DRBD Primary** (N≥2), preserving the on-disk contents
(SeaweedFS filer leveldb3, arbiter rqlite data dir, S3 IAM database) byte for
byte. External DRBD metadata lives on its own LV, so the data LV is never
rewritten during the move.

- **What:** local `cluster` singleton → DRBD Primary, mirrored to one peer.
- **When:** the cluster first reaches N≥2 while the tier is still `mode=local`.
- **Where:** runs on the mgmt-master only — the node holding `.254` and the
  live filer state. The first peer runs
  [`cluster_tier_join_peer`](cluster_tier_join_peer.md) to become Secondary.
- **End state:** `tiers.cluster.mode == "drbd"` in rqlite; `/dev/drbd1101`
  Primary + mounted at `/var/lib/bedrock/cluster`; singletons restarted;
  `/etc/bedrock/cluster-drbd-ready` written so the arbiter promote can move
  `.254` on a later failover.

**Trigger.** `cluster_tier_watcher()` in `mgmt/orchestrator.py` ticks every
10 s, reading the rqlite snapshot via `cluster_state.load_cluster()`. It
submits this saga when `mgmt_master == self`, `len(nodes) >= 2`, and
`tiers.cluster.mode == "local"`. The peer is the lowest-octet other node. The
watcher submits with `target_node = self` and runs it synchronously in a
thread, then records the peer key in an in-memory set so it won't re-submit a
completed or in-flight promote.

**Inputs (`ctx`)**

| key | type | meaning |
|-----|------|---------|
| `peer_node` | str | First peer to mirror to (lowest-octet other node) |
| `peer_loopback` | str | Peer's `100.X.Y.Z/32` for the DRBD link address |

**Outputs (`ctx`)**

| key | set by | meaning |
|-----|--------|---------|
| `_already_drbd` | `check_preconditions` | Short-circuit flag when the tier is already `mode=drbd` |
| `_promote_result` | `promote_local_to_drbd` | `transition_to_n2_master`'s return dict (`{"peers": [...]}`) |

**Steps**

| # | Step | What it does |
|---|------|--------------|
| 1 | [`check_preconditions`](#1-check_preconditions) | Self is still master, tier still `local`, peer params sane |
| 2 | [`promote_local_to_drbd`](#2-promote_local_to_drbd) | Stop singletons, snapshot data, create LV pair + `.res`, `create-md`/`up`/`primary`, mount, restore, fix fstab, restart singletons |
| 3 | [`record_tier_state_rqlite`](#3-record_tier_state_rqlite) | Re-affirm `tiers.cluster` row so every node sees `mode=drbd` |

## Detail

### 1. `check_preconditions`

Read-only. Reads the cluster snapshot (`cluster_state.load_cluster()`) and
raises to abort when:

- `mgmt_master != self` — the cluster failed over while the watcher waited; the
  new master's watcher fires its own promote.
- `peer_node` or `peer_loopback` ctx is empty — caller bug.
- `peer_node` is missing from `nodes` — bootstrap window race; the watcher
  retries next tick.

If the tier is already `mode=drbd`, sets `ctx["_already_drbd"] = True` and
returns; steps 2 and 3 then no-op.

- **Revert:** none (read-only).
- **Idempotent:** yes.

### 2. `promote_local_to_drbd`

Thin wrapper over `tier_storage.transition_to_n2_master(self_loopback_ip,
peer)`, which builds the two-node peer list and calls
`promote_local_to_drbd_master("cluster", peers)`. Skipped when
`_already_drbd`.

Short-circuits to a no-op if the resource is already `Primary` and mounted
(`drbdadm status` + `mountpoint -q`). Otherwise:

```
1. ensure data + meta LV pair  bedrock-data-cluster (5 GiB thin) +
                               bedrock-meta-cluster (DRBD9 meta-size formula)
2. write /etc/drbd.d/cluster.res  full-mesh peer blocks, --max-peers=7,
                               resync-rate 100M, c-min-rate 0, c-plan-ahead 0
3. stop singletons             bedrock-weed-s3, bedrock-weed-filer,
                               bedrock-rqlited-arbiter  (quiesce leveldb3)
4. cp -a /var/lib/bedrock/cluster/. -> /var/lib/bedrock-promote-snapshot/
5. umount /var/lib/bedrock/cluster  (only if it is a mountpoint; at N=1 it is
                               a plain dir, so usually a no-op)
6. drbdadm create-md --force --max-peers=7 ; drbdadm up cluster
                               both skipped if the resource is already
                               configured (DRBD9 rejects create-md/up on a
                               configured device -> lets the step re-run)
7. drbdadm primary --force cluster  (initial sync source; no-op if Primary)
8. mount /dev/drbd1101 /var/lib/bedrock/cluster
                               mkfs.xfs on first promote; existing XFS reused
9. cp -a snapshot/. -> /var/lib/bedrock/cluster/ ; rm snapshot
10. rewrite /etc/fstab          drop any prior cluster line, add the DRBD line
11. restart the three singletons
```

`transition_to_n2_master` then writes the `tiers.cluster` rqlite row
(`mode=drbd`) and writes `/etc/bedrock/cluster-drbd-ready`, releasing
`cluster_arbiter` to host `.254` + the arbiter rqlite via DRBD handoff on a
later failover.

- **Revert:** no demote-to-local saga. `tier_storage.drbd_demote_to_local()`
  is the operator helper for shrinking to N=1 / decommission (it removes
  `cluster.res`, rewrites fstab to the local LV, `drbdadm down`, remounts the
  data LV, sets `mode=local`; persistent state changes before the kernel-side
  down, so a reboot mid-flight still lands at the local mount). Manual recovery
  from a half-promote: `drbdadm down cluster && drbdadm wipe-md cluster` on both
  nodes, `umount /var/lib/bedrock/cluster` on the master, restore the singleton
  dir onto the root FS, set the `cluster` row back to `mode=local` in rqlite.
- **Idempotent:** yes. Re-running walks `promote_local_to_drbd_master` again;
  each sub-step's guard no-ops completed work (`create-md`/`up` skip when the
  resource is configured; `primary --force`, fstab rewrite, and snapshot
  restore are repeat-safe). The data LV survives a crash because external
  metadata leaves it byte-untouched until the mount; the snapshot copy under
  `/var/lib/bedrock-promote-snapshot/` is recreated each run.

### 3. `record_tier_state_rqlite`

Re-affirms the `cluster` tier row via
`tier_storage.set_tier_state("cluster", mode=..., master=..., peers=...,
backend_path="/var/lib/bedrock/cluster")`. Step 2 already wrote it; this
re-write bumps `bedrock_meta.revision` so every node's `rqlite_subscriber`
re-projects within ~2 s and sees `mode=drbd`. `set_tier_state` is master-only
(followers no-op) and `INSERT OR REPLACE`. Skipped when `_already_drbd`.

The peer's [`cluster_tier_join_peer`](cluster_tier_join_peer.md) saga polls
rqlite for exactly this `mode=drbd` before joining as Secondary.

- **Revert:** none meaningful (re-affirming an already-correct row).
- **Idempotent:** yes (`INSERT OR REPLACE`).

## Kopia / backup snapshot compatibility

`bedrock-data-cluster` is a thin LV in the same pool as VM disks, so
`lvcreate --snapshot --thinpool` captures point-in-time views without copying
blocks. DRBD mirrors at the block layer below LVM and is unaware of the
snapshot; the data LV must stay a thin LV (not thick, raw, or non-LVM) for this
path to work. Resolve the VG name at runtime — never hardcode it.

```bash
fsfreeze --freeze /var/lib/bedrock/cluster
lvcreate --snapshot -n cluster-snap-$(date +%s) <VG>/bedrock-data-cluster
fsfreeze --unfreeze /var/lib/bedrock/cluster
mount -o ro,nouuid /dev/<VG>/cluster-snap-... /mnt/snap
kopia snapshot create /mnt/snap
umount /mnt/snap
lvremove <VG>/cluster-snap-...
```
