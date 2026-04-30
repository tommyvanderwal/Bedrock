# `tier_storage.py` — operational spec

Companion document for [`tier_storage.py`](tier_storage.py). This is the
**current** implementation reference: what each function does, what
invariants it preserves, where state lives, and the reasoning behind every
operational choice. Sources are at the bottom.

For the journey of how we got here (wrong turns, debug sessions, lessons
that informed the current shape), see [`docs/lessons-log.md`](../../docs/lessons-log.md).

---

## What this module does

Bedrock has three storage tiers — `scratch`, `bulk`, `critical` — visible
to all node-internal consumers as `/bedrock/<tier>`. The mountpoint is the
*stable abstraction*; the **backend behind the symlink changes as the
cluster grows or shrinks**, but `/bedrock/<tier>` is always a valid path
on every node.

| Tier      | Use                                  | Backend at N=1 | Backend at N≥2                     |
|-----------|--------------------------------------|----------------|-------------------------------------|
| scratch   | LLM caches, build artifacts, ephemera | local thin LV  | Garage S3 (RF=1) via local s3fs     |
| bulk      | ISOs, VM templates, snapshots         | local thin LV  | DRBD 2-way + XFS, NFS-served by master |
| critical  | configs, license keys, backups        | local thin LV  | DRBD 3-way (when N≥3) + XFS, NFS-served by master |

`scratch` is "RAID0-ish": no redundancy, lose-it-and-redownload.
`bulk` survives 1 node failure. `critical` survives 2 (when N≥3).

---

## Design invariants (review these for "can this break" analysis)

1. **`/bedrock/<tier>` is always a valid symlink** to a *real* mount-point.
   It is never overwritten with a half-state. Atomic swaps via `rename(2)`
   in `atomic_symlink()`. In-flight readers continue on the previous
   inode until they close.

2. **The on-disk config is the source of truth, in-memory/kernel state is
   reconciled to it.** This is the crash-safety property: if power is
   lost mid-transition, the next boot reads the on-disk config files
   (`/etc/drbd.d/*.res`, `/etc/fstab`, `/etc/garage.toml`,
   `/etc/bedrock/cluster.json`) and brings the system to that intended
   state. The reverse order (mutate kernel first, edit config later)
   would leave a window where a crash leaves persistent and runtime
   state diverged.

3. **DRBD node-ids are permanent for a resource.** Once peer X has been
   assigned `node-id 2` for resource `tier-bulk`, it keeps that ID for
   the resource's lifetime. We persist `{peer_name: node_id}` per
   resource in `cluster.json` (`tiers.<tier>.drbd_node_ids`) and
   `render_drbd_res()` honors it. New peers get the next free integer
   that has never been used for this resource. Freed slots are not
   reused until `drbdsetup forget-peer` clears the meta-disk bitmap.

4. **DRBD external metadata** is mandatory for tier resources. The data
   LV (e.g. `bedrock/tier-bulk`) holds *only* the XFS filesystem; a
   small (~32 MB) sibling meta LV (`bedrock/tier-bulk-meta`) holds DRBD
   metadata. This is what makes promotion of a local-LV-with-data to a
   DRBD-replicated-LV-with-the-same-data zero-copy: `create-md` only
   touches the meta LV, the existing XFS on the data LV is byte-for-byte
   preserved.

5. **`--max-peers=7` at every `create-md`.** DRBD's default is 1, which
   forces a brief metadata regeneration window if you ever grow past
   2-way. We always pre-allocate slots so we can grow online without
   touching meta-disks.

6. **s3fs always targets the *local* Garage daemon.** `url=http://127.0.0.1:3900`.
   Garage handles cross-node lookup internally via its own RPC; pointing
   the FUSE client at one specific remote node creates a single point of
   failure and a hang-on-dead-endpoint failure mode that has nothing to
   do with the cluster's actual health.

7. **mgmt + NFS server are co-located** on whichever node holds the DRBD
   primary for `tier-bulk` and `tier-critical`. The role moves as one
   unit during failover (see `bedrock storage transfer-mgmt-role`,
   queued).

8. **Garage cluster membership is independent of mgmt role.** Every node
   runs a Garage daemon; layout assigns capacity to each. The mgmt role
   doesn't affect Garage routing.

---

## Where state lives

| State | Location | Owner | When updated |
|-------|----------|-------|--------------|
| Tier mode (local / drbd-nfs / garage) | `/etc/bedrock/cluster.json` `tiers.<tier>` | mgmt node | On each transition |
| Tier master (NFS-export source) | `/etc/bedrock/cluster.json` `tiers.<tier>.master` | mgmt node | On promote / mgmt-role transfer |
| DRBD node-id assignments | `/etc/bedrock/cluster.json` `tiers.<tier>.drbd_node_ids` | mgmt node | When peer added |
| Per-tier symlink target | `/bedrock/<tier>` (real symlink) | each node locally | On any backend swap |
| DRBD resource config | `/etc/drbd.d/tier-<tier>.res` | each node locally | Identical content distributed via SSH/rsync |
| NFS exports | `/etc/exports.d/bedrock-tiers.exports` | mgmt node only | On master role change |
| NFS client mounts | `/etc/fstab` | each non-master node | On master role change |
| Garage cluster layout | Garage internal (admin RPC) | one node, replicated by Garage | On `garage layout apply` |
| Garage data | `/var/lib/garage/data/` | each node locally | On block resync |

---

## Operations (entry points)

### `setup_n1()` — single-node setup

Idempotent. Run on every node at install time (called from
`mgmt_install.install_full()` for the first node and
`agent_install.install()` for joiners).

```
ensure_thinpool()                     # bedrock VG + thinpool on /dev/vdb (or sdb/nvme1n1)
for each tier:
  ensure_thin_lv("tier-<tier>", size)
  ensure_xfs("/dev/bedrock/tier-<tier>", label=<tier>)  # XFS labels max 12 chars
  ensure_mounted(...)                 # mount at /var/lib/bedrock/local/<tier>
  atomic_symlink("/var/lib/bedrock/local/<tier>", "/bedrock/<tier>")
  set_tier_state(<tier>, mode="local", ...)
```

Failure modes:
- `find_data_disk()` raises if no candidate disk exists → operator must attach `/dev/vdb` (or sdb/nvme1n1) before retry.
- mkfs.xfs label limit is 12 chars; we use the bare tier name (`scratch`, `bulk`, `critical`) which all fit.

### `promote_local_to_drbd_master(tier, peers)` — N=1 → N=2 master side

Converts a locally-mounted thin LV into a DRBD-Primary-mounted device,
**preserving the existing XFS**. Called from
`transition_to_n2_master()` for `bulk` and `critical`.

Operations in order:
```
1. ensure_meta_lv(f"tier-{tier}-meta")          # 32 MB thick LV outside thin pool
2. write_drbd_resource(tier, peers)              # /etc/drbd.d/tier-<tier>.res
3. umount /var/lib/bedrock/local/<tier>          # release the LV
4. drbdadm create-md tier-<tier> --force --max-peers=7
5. drbdadm up tier-<tier>
6. drbdadm primary --force tier-<tier>           # because no peers yet
7. mount /dev/drbd<minor> /var/lib/bedrock/mounts/<tier>-drbd
8. fstab: replace local-LV line with DRBD-mount line (nofail,_netdev)
9. atomic_symlink(<drbd-mount>, "/bedrock/<tier>")
```

**Crash safety:** any step before (8) leaves the on-disk config still
pointing at the local LV. A crash and reboot would re-mount the local LV
(unchanged data) per fstab. Step 8 is the commitment point. Once fstab
references the DRBD mount, on next boot DRBD comes up first
(`bedrock-init`-ordered service deps), then mount picks the DRBD device.

### `join_drbd_peer(tier, peers)` — N=1 → N=2 peer side

Run on the joining node. Brings up DRBD as Secondary, lets initial sync
populate the LV from the master.

```
1. ensure_thin_lv(f"tier-{tier}", size)         # peer's tier LV (will be overwritten by sync)
2. ensure_meta_lv(f"tier-{tier}-meta")
3. write_drbd_resource(tier, peers)              # same content as master
4. drbdadm create-md tier-<tier> --force --max-peers=7
5. drbdadm up tier-<tier>
   # initial sync starts automatically; peer is Inconsistent → SyncTarget → UpToDate
```

The peer's `/etc/fstab` does NOT include the DRBD device — peer accesses
the tier via NFS mount from master. NFS mount comes via
`nfs_mount_drbd_tiers(master_drbd_ip)`.

### `nfs_export_drbd_tiers(allowed_subnets)` / `nfs_mount_drbd_tiers(master_drbd_ip)`

Mgmt-side: writes `/etc/exports.d/bedrock-tiers.exports`, runs
`exportfs -ra`, ensures `nfs-server` is enabled. Idempotent.

Peer side: writes fstab line per tier
(`<master-ip>:/var/lib/bedrock/mounts/<tier>-drbd /var/lib/bedrock/mounts/<tier>-nfs nfs rw,nolock,soft,timeo=50,retrans=3,_netdev,nofail 0 0`),
mounts each, and updates `/bedrock/<tier>` symlink to the NFS path.

We use **plain fstab + mount** rather than systemd `.mount` units because
systemd-escape's `\x2d` for the `-` in `bulk-nfs` proved fragile in
testing.

### `install_garage_local(drbd_ip, rpc_secret, admin_token)`

Per-node Garage install: package, user, data + meta LVs, config file,
systemd unit, start. The config:

```toml
metadata_dir = "/var/lib/garage/meta"
data_dir     = "/var/lib/garage/data"
db_engine    = "lmdb"
replication_factor = 1                    # scratch tier semantics
rpc_secret = "<shared cluster-wide>"
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "<this node's drbd_ip>:3901"   # peers reach us here
[s3_api]
api_bind_addr = "[::]:3900"
s3_region = "garage"
[admin]
api_bind_addr = "[::]:3903"
admin_token = "<shared cluster-wide>"
```

`rpc_secret` and `admin_token` are generated once on the first promote
and propagated to every Garage daemon in the cluster. They live in
`/etc/garage.toml` on every node.

### `garage_form_cluster(peers_drbd_ips)`

Run on the bootstrap node (typically the master). For each peer:
- Get the peer's full node id (locally or via SSH)
- `garage node connect <full-id>` from this node
- `garage layout assign -z dc1 -c <capacity-gb>G <short-id>` — same capacity per node
- `garage layout apply --version <next>`

**Why one zone (`dc1`)** even when nodes are physically diverse: Garage's
"zone redundancy" guarantees only matter at RF≥2 across zones. At RF=1
it's a no-op. Single-zone keeps the layout math simple. When/if we go
multi-DC later, we reassign zones.

### `s3fs_mount_scratch(access_key, secret_key)`

Mounts the `scratch` Garage bucket at `/var/lib/bedrock/mounts/scratch-s3fs`,
points `/bedrock/scratch` symlink at it.

```
fstab line:
scratch /var/lib/bedrock/mounts/scratch-s3fs fuse.s3fs \
  _netdev,allow_other,umask=0022,sigv4,endpoint=garage,\
  use_path_request_style,url=http://127.0.0.1:3900,\
  passwd_file=/etc/passwd-s3fs 0 0
```

Note `endpoint=garage` (matches Garage's `s3_region`; SigV4 requires the
client and server agree on region) and `url=http://127.0.0.1:3900`
(local Garage daemon, never a remote IP).

Requires EPEL on AlmaLinux 9 (`s3fs-fuse` is not in stock repos).

### Top-level transitions

- `transition_to_n2_master(self_drbd_ip, peer, rpc_secret, admin_token)`:
  Master side of N=1 → N=2. Promotes bulk/critical local LVs to DRBD
  primary, exports NFS. Returns shared secrets for the peer to use.
- `transition_to_n2_peer(self_drbd_ip, master, ...)`: Peer side. Drops
  local bulk/critical mounts, joins DRBD as Secondary, NFS-mounts from
  master, installs Garage.
- `finalize_n2_garage(...)`: Forms the Garage cluster after both daemons
  are up, creates the `scratch` bucket and key.
- `promote_critical_to_3way(third_peer)`: Adds a third DRBD peer to
  `tier-critical`. **Currently uses `drbdadm adjust`** but the LINBIT-
  blessed path requires node-id stability (see Known Issues).

---

## Known issues / current limitations

### 1. `render_drbd_res()` renumbers node-ids on every call

Currently the function assigns node-ids by `enumerate(peers)` order —
which means rewriting the config with a different peer set causes every
peer's node-id to potentially change. This conflicts with invariant #3
(node-ids are permanent). Symptom: `drbdadm adjust` fails with "peer
node id cannot be my own node id" and other cryptic errors.

**Fix queued:** persist `tiers.<tier>.drbd_node_ids = {peer_name: id}` in
`cluster.json`, pass it into `render_drbd_res()`. New peers get the next
free integer; existing peers keep their assigned id forever.

### 2. `transition_to_n2_peer` re-uses sim-1's data path mounts

Peer's local thin LV for bulk/critical is created (default size) and
unmounted. The XFS on it gets overwritten by DRBD initial sync from
master. Until `drbdadm create-md`, the LV data is the empty filesystem
from `mkfs.xfs` in `setup_n1`; this is wasteful but correct.

### 3. NFS client uses `soft` mount option

`soft,timeo=50,retrans=3` returns I/O errors after 50×0.1s × 3 retries
(~15 s) instead of hanging forever. Trade-off: applications that ignore
EIO can lose writes during master failover. Acceptable for Bedrock's
read-mostly tier usage; revisit if we get write-heavy consumers.

### 4. `promote_critical_to_3way` uses raw `drbdadm adjust`

Should use the LINBIT-blessed pattern: edit on-disk config first,
distribute, run `drbdadm --dry-run adjust`, then `drbdadm adjust`. The
current implementation does this in spirit but doesn't perform the
dry-run safety check.

### 5. No `drbd_remove_peer()` yet

Removing a peer from a running resource is a planned function. Design
sketch in [`docs/scenarios/storage-tiers-deep-dive-2026-04-30.md`](../../docs/scenarios/storage-tiers-deep-dive-2026-04-30.md).
Will follow the LINBIT-blessed `drbdadm adjust` flow with `drbdsetup`
fallback for cases where the on-disk config has already diverged from
kernel state.

### 6. No `garage_drain_node()` yet

Removing a Garage node currently requires manual operator steps (layout
remove + apply + watch worker queue + repair). Should be wrapped as a
function with proper waiting and verification.

### 7. No `transfer_mgmt_role()` yet

Failing over the mgmt + NFS-server role between nodes is currently
manual (rsync /opt/bedrock, copy systemd units, repoint NFS clients).

---

## Why each design choice — quick reference

**Why mountpoints, not S3 endpoints, as the stable abstraction?**
Because Bedrock's internal consumers (libvirt for ISOs, mgmt for tier
files) are POSIX-native. Asking libvirt to fetch via S3 each time means
adding boto3-like dependencies and a new failure mode. The mountpoint
abstraction works at any cluster size with the same client code.

**Why DRBD external metadata (separate meta LV)?**
- Data LV size = `/dev/drbdN` size byte-for-byte (no end-of-LV reservation).
- `drbdadm create-md --force` does not touch the data LV, so we can
  promote a locally-mounted-XFS LV to DRBD-replicated without copying
  data.
- Meta LV can live outside the thin pool, so a thin-pool fill doesn't
  block DRBD metadata writes.

**Why Garage RF=1 for scratch (not RF=2)?**
- Scratch is "regenerable from source" by design — the tier holds LLM
  caches and similar, not durable data.
- RF=1 doubles capacity efficiency (8 nodes × 1 TB → 8 TB usable, vs.
  4 TB at RF=2).
- Graceful node-drain at RF=1 IS supported by Garage (per-partition
  resync via `block_resync` worker; see Garage source `src/block/resync.rs`).

**Why DRBD-NFS for bulk and critical (not Garage RF=2/3)?**
- Bulk holds ISOs and templates — large, infrequent writes, frequent
  reads. NFS is fine. No need for object-store metadata overhead.
- DRBD's synchronous write semantics give stronger durability guarantees
  than Garage's eventual-consistency block resync.
- The mgmt node already runs the NFS server for the ISO library; adding
  bulk/critical to the same nfs-server is incremental.
- bulk + critical fit under per-node thin-pool budgets; we don't need
  per-bucket replication-factor configurability.

**Why s3fs FUSE for the scratch endpoint, not direct S3?**
- Inside Bedrock, code paths assume POSIX (`/bedrock/scratch/foo.bin`).
  s3fs lets us keep that interface while the backend is S3.
- Trade-off: s3fs is famously imperfect at POSIX semantics (file locks,
  directory atomicity). Acceptable for scratch's "cache, not source of
  truth" semantics.
- Alternative considered: rclone mount or goofys. s3fs has the
  longest production track record on RHEL family.

**Why mgmt + NFS server coupled to one node?**
- `cluster.json` is the source of truth for cluster topology. It needs
  one writer to avoid update conflicts.
- The DRBD primary for bulk/critical is naturally the right place to
  serve NFS — local read of the XFS filesystem rather than NFS-of-DRBD
  via a different node.
- Failover is an explicit operator action (queued
  `transfer-mgmt-role`), not automatic — Bedrock's witness model is
  about *split-brain prevention*, not automatic role migration.

---

## Sources

### DRBD 9
- [drbdsetup-9.0(8) man page](https://manpages.debian.org/testing/drbd-utils/drbdsetup-9.0.8.en.html) — authoritative for `disconnect`, `del-peer`, `forget-peer`, `new-peer`
- [drbdadm-9.0(8) man page](https://manpages.debian.org/testing/drbd-utils/drbdadm-9.0.8.en.html) — `adjust` semantics
- [LINBIT/drbd-utils source — `user/v9/drbdadm_adjust.c`](https://github.com/LINBIT/drbd-utils/blob/master/user/v9/drbdadm_adjust.c) — lines 858–868: `adjust_net()` schedules `del_peer_cmd` for any kernel connection without a config match. Line 806: `/* disconnect implicit by del-peer */`.
- [LINBIT DRBD 9 User Guide](https://linbit.com/drbd-user-guide/drbd-guide-9_0-en/) — §5.24 (remove DRBD entirely), §7.3.3 (replace failed node), §7.5 (quorum recovery). Note: there is no dedicated chapter on online peer removal; authority is the man pages + adjust source.
- [LINBIT community forum — "Remove old drbd setup"](https://forums.linbit.com/t/remove-old-drbd-setup/539) — LINBIT staff (Devin) recommending `drbdadm adjust` for live config changes.
- [DRBD `--max-peers` documented behavior](https://linbit.com/drbd-user-guide/drbd-guide-9_0-en/#s-max-peers) — set at `create-md` time; changing later requires meta-disk regeneration.

### Garage
- [Garage Layout documentation](https://garagehq.deuxfleurs.fr/documentation/operations/layout/) — `layout assign`, `apply`, `remove`.
- [Garage Recovering from failures](https://garagehq.deuxfleurs.fr/documentation/operations/recovering/) — node decommission procedure (lacks "wait" step).
- [Garage Durability and repairs](https://garagehq.deuxfleurs.fr/documentation/operations/durability-repairs/) — `worker list`, `worker set`, `block list-errors`, `repair tables`, `repair blocks`.
- [Garage CLI reference](https://garagehq.deuxfleurs.fr/documentation/reference-manual/cli/) — full command list.
- [Garage source — `src/block/resync.rs`](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/block/resync.rs) — lines 362–510: per-partition offload-then-delete logic.
- [Garage source — `src/rpc/layout/history.rs`](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/rpc/layout/history.rs) — multi-version layout, old versions preserved until `sync_ack_map_min` advances.
- [Garage source — `src/rpc/rpc_helper.rs`](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/rpc/rpc_helper.rs) — `block_read_nodes_of()` (line ~570): tries current layout owners first, falls back to old versions in reverse.

### s3fs-fuse
- [s3fs-fuse(1) man page](https://github.com/s3fs-fuse/s3fs-fuse/blob/master/doc/man/s3fs.1.in) — mount options including `endpoint`, `sigv4`, `use_path_request_style`, `url`.
- [s3fs README on Garage compatibility](https://github.com/s3fs-fuse/s3fs-fuse/wiki/FAQ) — region-name handling for non-AWS S3.

### NFS
- [`exports(5)` man page](https://manpages.debian.org/testing/nfs-kernel-server/exports.5.en.html) — `rw,sync,no_root_squash,no_subtree_check` semantics.
- [Linux Documentation — NFS client mount options](https://www.kernel.org/doc/Documentation/filesystems/nfs/) — `soft` vs `hard`, `timeo`, `retrans`.

### Bedrock project
- [`BEDROCK.md`](../../BEDROCK.md) — design principles: workload tiers (cattle/pet/vipet), 1-node growth path, KISS framework refusal, Python primary.
- [`docs/architecture.md`](../../docs/architecture.md) — control plane, data plane, mgmt LAN vs DRBD ring.
- [`docs/03-witness-and-orchestrator.md`](../../docs/03-witness-and-orchestrator.md) — bedrock-failover quorum model (independent of tier storage).
- [`docs/scenarios/storage-tiers-1to4-2026-04-30.md`](../../docs/scenarios/storage-tiers-1to4-2026-04-30.md) — POC log of the original 4-node scale-up.
- [`docs/scenarios/storage-tiers-deep-dive-2026-04-30.md`](../../docs/scenarios/storage-tiers-deep-dive-2026-04-30.md) — root-cause analysis of sim-1 removal blockers.

### LVM thin
- [`lvmthin(7)` man page](https://manpages.debian.org/testing/lvm2/lvmthin.7.en.html) — thin pool creation, snapshot semantics, monitoring.

### Linux atomic operations
- [`rename(2)` man page](https://man7.org/linux/man-pages/man2/rename.2.html) — atomic across single filesystem; basis for `atomic_symlink()`.
