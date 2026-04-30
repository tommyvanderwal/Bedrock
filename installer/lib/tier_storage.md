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
| Garage cluster layout | Garage internal (admin RPC) | one node, replicated by Garage | On `POST /v2/ApplyClusterLayout` |
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

### Garage admin API helpers (`_garage_api`, `_garage_admin_token`)

Every Garage interaction in `tier_storage.py` goes through the v2
admin API at `http://127.0.0.1:3903`, not through the `garage` CLI.
The two helpers:

- `_garage_admin_token(host=None)` — `awk` the `admin_token` value out
  of `/etc/garage.toml` on `host` (None = local). Same value
  cluster-wide.
- `_garage_api(method, path, body=None, *, host=None, token=None,
  check=True)` — issue the request. Local calls go through stdlib
  `urllib`; remote calls use `curl` over our `ssh()` helper (since
  the admin port may not be routable cluster-wide). Returns parsed
  JSON.

Bodies for API endpoints:
- `POST /v2/UpdateClusterLayout` →
  `{"roles": [{"id": <full-hex>, "zone": "dc1",
   "capacity": <bytes>, "tags": []}]}` for assigning,
  or `{"roles": [{"id": <full-hex>, "remove": true}]}` for removing.
- `POST /v2/ApplyClusterLayout` → `{"version": <int>}` (version comes
  from `GET /v2/GetClusterLayout`'s `version` field +1).
- `POST /v2/ConnectClusterNodes` → `["<id@addr>", ...]` (bare array).
- `POST /v2/CreateBucket` → `{"globalAlias": "scratch"}`.
- `POST /v2/CreateKey` → `{"name": "scratch-key"}`. Response includes
  `accessKeyId` and `secretAccessKey` directly.
- `POST /v2/AllowBucketKey` → `{"bucketId": <hex>, "accessKeyId":
  <id>, "permissions": {"read": true, "write": true, "owner": true}}`.
- `POST /v2/SetWorkerVariable?node=self` → `{"variable": ...,
  "value": ...}`.
- `POST /v2/LaunchRepairOperation?node=*` → `{"repairType":
  "tables"}` (or `"blocks"`).
- `GET /v2/ListBlockErrors?node=self` → wrapped MultiResponse;
  drained-clean signal is `len(success.<self>) == 0`.
- `POST /v2/ListWorkers?node=self` (body `{}`) → wrapped
  MultiResponse of worker structs; drained-clean signal is every
  "Block resync worker" entry having `state=="idle"` AND
  `queueLength in (0, null)`.

Why API, not CLI: see lessons-log
[L24](../../docs/lessons-log.md#l24). Short version: CLI output is
human-readable and label-changes between Garage versions silently
break parsing; the admin API has structured JSON whose schema is in
the OpenAPI v2 spec.

### `garage_form_cluster(peers_drbd_ips)`

Run on the bootstrap node (typically the master). For each peer:
- Read the peer's full node id via `GET /v2/GetNodeInfo?node=self` on
  that node's admin API.
- Resolve its RPC `<addr>` from `GET /v2/GetClusterStatus` (cold-start
  fallback: drbd_ip + GARAGE_RPC_PORT).
- `POST /v2/ConnectClusterNodes` with the array of `<id@addr>` strings.
- `POST /v2/UpdateClusterLayout` with one `{"id":..,"zone":"dc1",
  "capacity": <bytes>, "tags": []}` per peer.
- `POST /v2/ApplyClusterLayout` with `version = current+1` (read from
  `GET /v2/GetClusterLayout`).

All calls go through `_garage_api()` (see "Garage admin API" below).
The CLI is *not* used here — see lessons-log L24 for the rationale.

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

### `migrate_scratch_into_garage(verify_md5=True)` — local LV → Garage bucket

Called from `s3fs_mount_scratch()` at the N=1 → N=2 promotion: copies
existing local-LV scratch data into the Garage bucket *before* the
`/bedrock/scratch` symlink swap, so operator data isn't lost during a
planned topology change. Symmetric counterpart of
`migrate_scratch_out_of_garage()` at the N=2+ → N=1 collapse.

Same playbook as the reverse direction (rsync via the FUSE/local
mount, MD5 verify, atomic symlink swap, lsof drain, umount source,
drop fstab line) — only the direction reversed.

Full deep-dive (visual flow, exact commands, crash-safety table,
failure modes, sources):
[`tier_storage__migrate_scratch_into_garage.md`](tier_storage__migrate_scratch_into_garage.md).

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

### 1. ~~`render_drbd_res()` renumbers node-ids on every call~~  **FIXED**

`render_drbd_res()` now reads persistent assignments from
`cluster.json.tiers.<tier>.drbd_node_ids` via `get_drbd_node_id()`. New
peers get the next free integer; existing peers keep their id forever;
freed slots are not reused until `forget-peer` clears the bitmap (then
explicit `free_drbd_node_id()` removes from the map).

**Caveat for the existing testbed:** any DRBD resource that was created
*before* this fix may have kernel state with one set of node-ids and a
new on-disk config with a different set. `drbdadm adjust` will fail on
those resources. One-time fix: read kernel state via `drbdsetup show
<res>`, populate `cluster.json.tiers.<tier>.drbd_node_ids` to match,
then re-render the config.

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

### 5. ~~No `drbd_remove_peer()` yet~~  **IMPLEMENTED**

See section "drbd_remove_peer" below. Uses LINBIT-blessed config-edit
+ `drbdadm --dry-run adjust` + `drbdadm adjust` flow. Aborts on
unexpected dry-run output.

### 6. ~~No `garage_drain_node()` yet~~  **IMPLEMENTED**

See section "garage_drain_node" below. Polls
`POST /v2/ListWorkers?node=self` until all `Block resync` workers
state=idle with queueLength in (0, null); verifies
`GET /v2/ListBlockErrors?node=self` returns an empty array before
stopping the daemon; runs `POST /v2/LaunchRepairOperation?node=*`
for `tables` and `blocks` after. (See lessons-log L24 — every Garage
interaction is now via the v2 admin API, not the CLI.)

### 7. ~~No `transfer_mgmt_role()` yet~~  **IMPLEMENTED**

See section "transfer_mgmt_role" below. Ten-step playbook: stop
services on old, demote DRBD, promote on new, mount, rsync mgmt
files, copy systemd units, configure exports, start services,
re-point NFS clients on every other peer, swap symlinks, update
cluster.json. Idempotent.

### 8. ~~CLI wiring for the new helpers is queued~~  **WIRED**

The cluster-wide helpers are exposed as `bedrock storage <verb>`
subcommands. All of them are *cluster-wide* operations that the
operator runs from the mgmt master — the helper itself does the
SSH-fanout to peers as needed:

| Subcommand | Helper(s) called | Where to run | What it does |
|---|---|---|---|
| `bedrock storage promote` | `transition_to_n2_master` + `_peer` + `finalize_n2_garage` + `s3fs_mount_scratch` | mgmt master | N=1 → N=2 promotion |
| `bedrock storage promote-critical-3way <peer>` | `promote_critical_to_3way` | mgmt master | N=2 → N=3 for the critical tier |
| `bedrock storage transfer-mgmt <new-master>` | `transfer_mgmt_role` | any node with SSH to both | Move mgmt + NFS + DRBD primary |
| `bedrock storage remove-peer <name>` | `garage_drain_node` + `drbd_remove_peer` × 2 | mgmt master | Drain Garage, remove DRBD from bulk + critical, drop from cluster.json |
| `bedrock storage collapse-to-n1` | `drbd_demote_to_local` × 2 + `migrate_scratch_out_of_garage` | the last surviving node | Standalone N=1 from a fully-drained cluster |

`migrate_scratch_into_garage` is *not* exposed as a CLI verb — it
runs automatically from `s3fs_mount_scratch` during `bedrock storage
promote` (the N=1 → N=2 path). See
[`tier_storage__migrate_scratch_into_garage.md`](tier_storage__migrate_scratch_into_garage.md).

Per-node setup (`init`, `bootstrap`, `join`, `leave`) is the only
class of command that runs on the target node itself; everything
else is cluster-side.

Refusal rules baked into the wiring:
- `remove-peer` refuses to remove the current mgmt master (operator
  must `transfer-mgmt` first).
- `remove-peer` refuses if it would leave the cluster empty
  (operator should use `collapse-to-n1` instead on the last node).
- `collapse-to-n1` refuses if `cluster.json` still shows multiple
  nodes, and refuses if run on a non-surviving node.

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
- [Garage admin API v2 reference manual](https://garagehq.deuxfleurs.fr/documentation/reference-manual/admin-api/) — every operation we orchestrate goes through this.
- [Garage admin API OpenAPI v2.1.0](https://garagehq.deuxfleurs.fr/api/garage-admin-v2.json) — schemas for each request/response (machine-readable).
- [Garage Layout documentation](https://garagehq.deuxfleurs.fr/documentation/operations/layout/) — concept reference for `UpdateClusterLayout` + `ApplyClusterLayout`.
- [Garage Recovering from failures](https://garagehq.deuxfleurs.fr/documentation/operations/recovering/) — node decommission procedure (lacks "wait" step; `garage_drain_node` adds it).
- [Garage Durability and repairs](https://garagehq.deuxfleurs.fr/documentation/operations/durability-repairs/) — concept reference for `ListWorkers`, `SetWorkerVariable`, `ListBlockErrors`, `LaunchRepairOperation`.
- [Garage source — `src/api/admin/`](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main-v2/src/api/admin/) — admin API handlers (per-endpoint Rust code).
- [Garage source — `src/block/resync.rs`](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main-v2/src/block/resync.rs) — line 537 (worker name `Block resync worker #N`) + line 551 (`queueLength` field). Per-partition offload-then-delete logic.
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

---

## Visual reference — relationships and dependencies

### Overall data plane at N=4

```
  every node:                                    mgmt+NFS-server (master) only:
  ┌────────────────────────────────┐             ┌──────────────────────────────────┐
  │ /bedrock/scratch ─── symlink ──┼─→ s3fs FUSE │ /bedrock/{bulk,critical} symlink │
  │                                │   to local  │   ── local DRBD-backed XFS mount │
  │ /bedrock/bulk    ─── symlink ──┼─→ NFS mount │      ↓                           │
  │ /bedrock/critical── symlink ──┼─→ NFS mount  │   /dev/drbd1100 (tier-bulk)      │
  │                                │   from      │   /dev/drbd1101 (tier-critical)  │
  │ Garage daemon  :3900 :3901 :3903│   master    │      ↓ (sync over 10.99.0.x)     │
  └─────────┬──────────────────────┘             └──────────────────────────────────┘
            │
            │ Garage internal RPC (per-partition routing)
            ▼
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  Garage cluster — every node holds its share of partitions (RF=1)       │
  │  Each block lives on exactly one node; cross-node lookup via RPC.       │
  └─────────────────────────────────────────────────────────────────────────┘
```

### Where state lives at a glance

```
 ┌──────────────────── ON DISK (source of truth on reboot) ────────────────────┐
 │                                                                              │
 │  /etc/bedrock/cluster.json     ← cluster topology, tier modes,               │
 │      ├── nodes.<name>.{host,drbd_ip,role,pubkey,...}                        │
 │      └── tiers.<tier>.                                                       │
 │              {mode,master,peers,backend_path,version,                        │
 │               drbd_node_ids:{<peer_name>:<int>}}     ← persistent IDs        │
 │                                                                              │
 │  /etc/bedrock/state.json      ← per-node identity (mgmt_url, hardware)      │
 │  /etc/drbd.d/tier-<t>.res     ← DRBD resource configs (identical on all)    │
 │  /etc/exports.d/bedrock-tiers.exports  ← NFS exports (master only)          │
 │  /etc/fstab                   ← mounts (every node, slightly different)     │
 │  /etc/garage.toml             ← Garage daemon config (identical on all)     │
 │  /etc/passwd-s3fs             ← s3fs creds (every node)                     │
 │                                                                              │
 │  /var/lib/garage/{meta,data}  ← Garage's persistent storage                 │
 │  /dev/bedrock/tier-<t>        ← XFS data LV (DRBD-backed at N>=2)          │
 │  /dev/bedrock/tier-<t>-meta   ← DRBD external meta LV                       │
 └──────────────────────────────────────────────────────────────────────────────┘

 ┌──────────────────── IN MEMORY / KERNEL (rebuilt on boot) ───────────────────┐
 │                                                                              │
 │  DRBD kernel state            ← reconciled from /etc/drbd.d/* on `up`       │
 │      _peer_node_id assignments are PERMANENT once allocated                 │
 │  NFS kernel state             ← reconciled from /etc/exports.d/* on        │
 │                                  `exportfs -ra`                              │
 │  Mount points                 ← reconciled from /etc/fstab on `mount -a`    │
 │  Garage layout                ← persisted internally by Garage; gossiped    │
 │                                  cluster-wide                                │
 │  /bedrock/<tier> symlinks     ← persisted as filesystem objects             │
 │                                  (atomic_symlink uses rename(2))             │
 └──────────────────────────────────────────────────────────────────────────────┘
```

### Crash-safety invariant in one diagram

```
            t = 0         t = 1               t = 2 (commit)        t = 3
            ─────         ─────               ─────                 ─────
state on    OLD config    NEW config (just    NEW config            NEW config
disk:                     written)
                          ─ if power lost     ─ if power lost
                          here, on next       here, on next
                          boot the kernel     boot the kernel
                          will reconcile      will already be
                          to NEW              consistent
state in    OLD kernel    OLD kernel          NEW kernel            NEW kernel
kernel:                   (not yet adjusted)  (drbdadm adjust       (steady state)
                                               just ran)

                          ──────────────────────────────────────
                          window where on-disk and kernel disagree,
                          but persistent state is already correct so
                          a reboot reaches NEW automatically
```

This is why every state-changing helper writes to disk first
(everywhere it needs to land), then runs the kernel-side reconciliation
command. The reverse ordering would leave a window where a crash
reverts the kernel to OLD on next boot, undoing the operator's intent.

---

## `drbd_remove_peer(resource, leaving_peer_name, surviving_peers, surviving_hosts)`

### Top-of-section summary
Online removal of a peer from a running DRBD tier resource. The
surviving primary's `/dev/drbd<minor>` is **not interrupted** —
filesystems on top of it continue serving I/O throughout. After this
function returns, the leaving peer's connection is gone from kernel
state on every survivor, and its bitmap slot in the meta-disk has been
cleared so a future *different* peer added to the same resource can
reuse the slot via a bitmap-based resync rather than a full sync.

Pre-conditions:
- Caller has identified the peer to remove by its DRBD-config hostname
  (e.g. `bedrock-sim-1.bedrock.local`)
- Caller knows the surviving peer set (everything except the leaving
  one) as `[{"name": ..., "drbd_ip": ...}, ...]`
- This node and every host in `surviving_hosts` have root SSH set up
  among them (cluster init handles this)
- The leaving peer is currently a Secondary (or already gone). It must
  not be the active Primary — the caller is expected to have
  live-migrated workloads off first.

Post-conditions:
- `/etc/drbd.d/tier-<resource>.res` on every surviving host no longer
  contains the leaving peer
- Kernel state on every surviving host shows the resource with one
  fewer peer
- `cluster.json.tiers.<resource>.drbd_node_ids` has the leaving peer
  removed (its id is now free for re-assignment)
- `cluster.json.tiers.<resource>.peers` list is updated

### Visual flow

```
                          start (resource is N-way, leaving peer present)
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  1. write_drbd_resource(resource, surviving_peers)                 │
       │     -> /etc/drbd.d/tier-<resource>.res LOCALLY                     │
       │     -> render_drbd_res reads persistent node-ids,                  │
       │        survivors keep theirs; leaving peer just isn't in output    │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  2. SSH-fanout: distribute the new .res file to every survivor    │
       │     (base64-encoded to avoid shell quoting)                       │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  3. drbdadm --dry-run adjust on each survivor                     │
       │     ABORT if dry-run shows anything other than del-peer (or      │
       │     disconnect, del-path, down) — protects against subtle config │
       │     drift triggering an unintended resource cycle                │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  4. drbdadm adjust on each survivor                               │
       │     -> kernel: del-peer for the leaving id (no /dev/drbd<minor>   │
       │        interruption, surviving connections untouched)              │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  5. drbdsetup forget-peer <res> <leaving-id> on each survivor     │
       │     (best-effort) — clears bitmap slot in external meta-disk     │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────────────┐
       │  6. cluster.json: drop leaving peer from drbd_node_ids; bump     │
       │     tier version; persist                                         │
       └────────────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                          end (resource is (N-1)-way, leaving peer gone)
```

### Step-by-step exact commands (for reviewer verification)

For resource `r`, leaving peer named `L`, survivors `[A, B]`, hosts
`[hostA, hostB]`:

```bash
# 1. Render new config locally (this node):
drbdadm dump <r>      # for diff/inspection
write_drbd_resource(r, [A, B])
# /etc/drbd.d/tier-r.res now lists only A and B with their stable node-ids

# 2. Distribute via SSH to each survivor:
ssh root@hostA 'cat > /etc/drbd.d/tier-r.res' < /etc/drbd.d/tier-r.res
ssh root@hostB 'cat > /etc/drbd.d/tier-r.res' < /etc/drbd.d/tier-r.res

# 3. Dry-run preview on each survivor:
ssh root@hostA 'drbdadm --dry-run adjust tier-r'
ssh root@hostB 'drbdadm --dry-run adjust tier-r'
# expected output: "drbdsetup del-peer tier-r <leaving-id>" (and possibly
# disconnect on the same id, which is implicit). Anything else = abort.

# 4. Apply on each survivor:
ssh root@hostA 'drbdadm adjust tier-r'
ssh root@hostB 'drbdadm adjust tier-r'

# 5. Free the meta-disk bitmap slot (requires connection torn down,
#    which step 4 did):
ssh root@hostA 'drbdsetup forget-peer tier-r <leaving-id>'
ssh root@hostB 'drbdsetup forget-peer tier-r <leaving-id>'

# 6. Persistent state update (this node):
free_drbd_node_id(r, L)
set_tier_state(r, peers=[A.name, B.name])
```

### Crash-safety analysis

| Crash point | Persistent state | Kernel state | Recovery on next boot |
|-------------|------------------|--------------|------------------------|
| Before step 1 | OLD config everywhere | OLD (N peers) | Boot → OLD config → OLD kernel state. No-op. |
| Between 1 and 2 | NEW on this node, OLD elsewhere | OLD everywhere | Boot here → NEW config → adjust on `up` removes leaving peer locally. Other survivors still on OLD until distribute completes. Idempotent retry. |
| Between 2 and 4 | NEW everywhere | OLD everywhere | Boot → all surviving nodes' DRBD comes up with NEW config → del-peer issued automatically. Same end state. |
| Between 4 and 5 | NEW everywhere | NEW (peer gone) | Boot → reconcile to NEW. Bitmap slot still holds leaving peer's bitmap (small consequence: future *different* peer added to same id triggers full resync instead of bitmap-based). |
| After step 5 | NEW everywhere, drbd_node_ids still has leaving | NEW | Boot → consistent. drbd_node_ids cleanup is best-effort cosmetic. |

In every case, persistent state encodes the operator's intent;
kernel reconciliation on next boot completes the operation.

### Failure modes and what they look like

- **Dry-run shows new-peer or new-path:** indicates the on-disk config
  has diverged from kernel state in unexpected ways (likely
  node-id renumbering elsewhere, or a manual config edit).
  `drbd_remove_peer` aborts. Operator fix: run `drbdsetup show <res>`
  on every node and reconcile by hand, then retry.
- **adjust fails on one host but succeeds on others:** partial state.
  Persistent state on disk is identical everywhere (we distributed
  before applying). Re-run `drbdadm adjust tier-<resource>` on the
  failed host to retry; idempotent.
- **forget-peer fails:** non-fatal. Bitmap slot remains, costing a
  potential future full-resync. Log and continue.

### When NOT to use this function

- The leaving peer is the current Primary. Move workloads off first
  via `bedrock vm migrate` and `transfer_mgmt_role`; only then drop
  it as a DRBD peer.
- The resource is degraded (a different peer is in `Inconsistent` /
  `SyncSource` state). Wait for sync to complete; otherwise removing
  a peer changes quorum math and may invalidate the running primary's
  ability to commit writes.

---

## `garage_drain_node(departing_node_id_short, surviving_admin_host, departing_node_admin_host, ...)`

### Top-of-section summary
Graceful, online decommission of a Garage node. Works at any
replication factor including RF=1, because Garage's `block_resync`
worker is offload-then-delete: each block is copied from the source
to its new owner BEFORE being deleted from the source. Reads during
the transition fall back to the multi-version layout history (Garage
internally retains old layout versions until `sync_ack_map_min`
advances past them). After this function returns, the departing
node's data is fully migrated to surviving nodes, the Garage daemon
on the departing node is stopped, and the cluster has one fewer
member.

Pre-conditions:
- The departing node is currently in the Garage cluster's layout
- `surviving_admin_host` can reach the cluster's admin API (any node
  works; the layout commands gossip through the cluster)
- `departing_node_admin_host` is the mgmt-LAN address of the node
  whose Garage daemon will be drained and stopped
- `surviving_admin_host` can SSH (root) to `departing_node_admin_host`

Post-conditions:
- Departing node has zero blocks owned in the new layout
- `garage block list-errors` is empty everywhere — no data was lost
- `garage repair tables` and `garage repair blocks` have run cluster-wide
- Garage daemon is stopped + disabled on the departing node
- `garage status` shows the surviving nodes only

### Visual flow

```
                                start
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  1. POST /v2/UpdateClusterLayout                               │
        │     body: {"roles":[{"id":<full-hex>,"remove":true}]}          │
        │     (on a surviving admin host)                                │
        │     -> stages the removal in the layout history                │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  2. POST /v2/ApplyClusterLayout {"version": cur+1}             │
        │     -> commits NEW layout. NEW partitions for departing node's │
        │        load are now mapped to surviving nodes.                 │
        │     -> Garage's block_resync worker on the DEPARTING node     │
        │        starts pushing blocks to their NEW owners.              │
        │     -> Reads continue working: layout history routes them to   │
        │        the OLD owner (still alive) until each block has been   │
        │        copied (rpc_helper.rs:570 falls back to old_versions).  │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  3. POST /v2/SetWorkerVariable?node=self  ×2                   │
        │     body: {"variable":"resync-tranquility","value":"0"}        │
        │     body: {"variable":"resync-worker-count","value":"8"}       │
        │     (on the DEPARTING node — speeds the drain by removing the  │
        │      throttle that exists for impact-friendly steady state)    │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  4. WAIT — poll POST /v2/ListWorkers?node=self on the          │
        │     departing node until every "Block resync worker #N"        │
        │     entry shows state="idle" AND queueLength in (0, null).     │
        │     Bounded by max_wait_seconds.                               │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  5. GET /v2/ListBlockErrors?node=self  (on departing node)     │
        │     The success-array MUST be empty. A non-empty list means    │
        │     data-loss candidates — abort, do not stop the node.        │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  6. POST /v2/LaunchRepairOperation?node=*  ×2                  │
        │     body: {"repairType":"tables"}                              │
        │     body: {"repairType":"blocks"}                              │
        │     -> ensures metadata tables and block reference counts are  │
        │        consistent across the surviving cluster.                │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  7. systemctl stop garage   (on departing node)                │
        │     systemctl disable garage                                    │
        │     -> the node leaves the cluster cleanly. Surviving cluster  │
        │        already has all data; no further coordination needed.   │
        └────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                                end
```

### Why each step matters

- **Step 2 must run BEFORE step 7.** If garage is stopped on the
  departing node before the resync has copied its blocks to new
  owners, those blocks are lost. The layout-history-based read
  fallback only works as long as the old owner is reachable.
- **Step 3 is optional but practical.** The default tranquility (~2)
  paces resync to be unnoticeable in mixed-workload steady state. For
  a controlled drain we want to finish; tranquility=0 + 8 workers
  saturates the disk/network.
- **Step 4 is the critical wait.** No way to skip; the only way to
  know it's safe to stop the source is that all of its blocks have
  been pushed to new owners.
- **Step 5 catches the unhappy case.** If any block failed to copy
  (network blip, bug, bad disk), it stays in the error queue. We
  refuse to stop the node — the operator must investigate.
- **Step 6 reconciles metadata.** Tables and block-ref counters can
  briefly disagree during layout transitions. The `repair` commands
  scan and fix; idempotent.

### Failure modes

- **Drain timeout (step 4):** `max_wait_seconds` exceeded with workers
  still busy. Possible causes: very large dataset on slow links,
  network partition, repeated transient errors. Function raises;
  operator can re-run (idempotent — Garage resumes the drain) or
  investigate per-block via `garage block info <hash>`.
- **Errored blocks (step 5):** `block list-errors` non-empty.
  Function raises. Use `garage block retry-now --all` to retry
  transient errors; investigate persistent ones (often disk-level).
- **Repair commands fail (step 6):** logged with `check=False`; not
  fatal because tables eventually re-sync via Merkle gossip. Re-run
  `garage repair` later if needed.

### When NOT to use this function

- The cluster's RF would be invalidated by the removal. Garage refuses
  the layout change if the new layout cannot satisfy the configured
  replication factor (e.g. removing a node from a 3-node RF=3 cluster
  fails the redundancy check). For Bedrock at RF=1 this never bites
  (any 1+ node satisfies RF=1).
- The departing node has stale or corrupt data. `garage repair
  blocks` should be run BEFORE the drain in that case to surface
  errors while the source is still in the cluster.

---

## `transfer_mgmt_role(old_master_host, new_master_host, ...)`

### Top-of-section summary
Move the mgmt + NFS-server + DRBD-primary role from one node to
another, in a single coordinated handoff. Outage measured during
testing: ~5–10 s for NFS clients to re-establish; mgmt API briefly
returns 503 during step 8. This function is idempotent — safe to
re-run if interrupted.

The role is "the node that holds bulk + critical DRBD primary, runs
the FastAPI dashboard + Victoria* metrics/logs, and is the NFS-server
endpoint that other nodes mount from." All three are coupled by
design (see "Why each design choice" below).

Pre-conditions:
- New master's DRBD secondaries for tier-bulk and tier-critical are
  currently `disk:UpToDate` (no in-flight sync to the new master).
  The function checks this and refuses if not.
- New master and every other peer are SSH-reachable as root.
- Old master may be offline (special case: rsync step uses
  `check=False`; if old master is gone, mgmt files won't be
  rsynced — operator must have a separate backup or have done
  the rsync earlier).

Post-conditions:
- New master is DRBD primary for tier-bulk and tier-critical
- New master is exporting NFS for both tiers
- New master is running `bedrock-mgmt`, `bedrock-vm`, `bedrock-vl`
- Every other peer's `/etc/fstab` has new master's DRBD IP for the
  NFS mounts; their NFS clients are remounted against new master
- Every node's `/bedrock/<tier>` symlink points at the right local
  path (DRBD-direct on new master, NFS-from-new-master on peers)
- Every node's `cluster.json.tiers.{bulk,critical}.master` field
  is updated to new master's name

### Visual flow

```
              ┌─────────────────────────────────────────────────────────┐
              │                                                         │
              │  Cluster before:                                        │
              │                                                         │
              │  old-master                  peer-A   peer-B            │
              │  ┌──────────┐                ┌─────┐  ┌─────┐           │
              │  │ DRBD pri │                │ sec │  │ sec │           │
              │  │ NFS srv  │ ◀── nfs ──     │     │  │     │           │
              │  │ mgmt FAPI│                └─────┘  └─────┘           │
              │  │ V-metrics│                                            │
              │  │ V-logs   │                                            │
              │  └──────────┘                                            │
              │                                                         │
              └─────────────────────────────────────────────────────────┘
                                          │
                            transfer_mgmt_role()
                                          │
              ┌─────────────────────────────────────────────────────────┐
              │                                                         │
              │  Cluster after:                                         │
              │                                                         │
              │  old-master              new-master       peer-B        │
              │  ┌──────────┐            ┌──────────┐     ┌─────┐      │
              │  │ DRBD sec │            │ DRBD pri │     │ sec │      │
              │  │ (no NFS) │  ── nfs ──▶│ NFS srv  │ ◀── │ nfs │      │
              │  │          │            │ mgmt FAPI│     │mount│      │
              │  │          │            │ V-metrics│     │     │      │
              │  │          │            │ V-logs   │     │     │      │
              │  └──────────┘            └──────────┘     └─────┘      │
              │                                                         │
              └─────────────────────────────────────────────────────────┘
```

### Step-by-step ordered with crash-safety annotations

```
Step  Action                                         Persistent state changes here
────  ─────────────────────────────────────────────  ──────────────────────────────
 0   Resolve old_master_drbd_ip if not given        — read-only
 1   Verify new master DRBD UpToDate                — read-only
 2   Stop bedrock-* + nfs-server on old master      — runtime only
 3   Unmount + secondary on old master              — runtime only
 4   Primary + mount on new master                  fstab ON NEW MASTER
                                                    (DRBD-mount line added)
 5   rsync /opt/bedrock/{mgmt,iso,data,bin}         /opt/bedrock/* on new master
 6   Copy systemd unit files                        /etc/systemd/system/* on new master
 7   NFS exports on new master                      /etc/exports.d/* on new master
                                                    + nfs-server enabled
 8   Start bedrock-{vm,vl,mgmt} on new master       — runtime only
 9   Re-point NFS clients on every other peer       /etc/fstab on each peer
                                                    (sed in place + remount)
10   Symlink swaps + cluster.json updates           /bedrock/<tier> symlinks
                                                    + cluster.json on every node
```

After step 10, the old master is a "secondary peer" — its NFS
clients (if any) point at the new master, its DRBD is Secondary, its
mgmt services are stopped. Operator can reuse it as a normal compute
peer or decommission it via `drbd_remove_peer` + node removal.

### Failure modes / partial completion

The function is idempotent; re-run it on the same arguments to resume.

- **Step 1 fails (UpToDate check):** the new master's DRBD is not in
  sync. Function raises. Wait for sync, then retry.
- **Steps 2–4 partial:** if old master crashes between unmount and
  the new master's `drbdadm primary`, both could be Secondary
  briefly. Re-running the function (or just running `drbdadm primary
  tier-X` + `mount` on new master) recovers.
- **Step 9 partial:** some peers have new IP in fstab, others still
  on old. `umount` on the lagging peers fails (server unreachable);
  `mount` succeeds against new server. Operator should `mount -a`
  on each peer to converge.
- **Step 10 partial:** symlinks updated on some nodes, not others.
  This is the one step where users could see a stale view briefly
  (e.g. `/bedrock/bulk` on peer-B still points at the OLD nfs mount
  which is now serverless and hangs). Mitigation: run step 10 after
  step 9 has succeeded everywhere; operationally the `bedrock
  storage status` output exposes any drift.

### Why each design choice

- **Why mgmt + NFS server are coupled to one node:** `cluster.json` is
  the single writable source of truth for cluster topology. The DRBD
  primary node already mounts the bulk/critical XFS locally; it's
  the natural NFS-server (NFS-of-the-already-mounted-XFS rather than
  NFS-of-DRBD-via-different-host). Coupling avoids two-node
  coordination for "is mgmt and is NFS-server" being out of sync.
- **Why NFS clients re-point via fstab edit, not DNS / VIP:** Bedrock
  prefers persistent disk state as the source of truth (Rule L10
  in lessons-log). A VIP that floats between nodes hides the
  "which server am I really talking to" — useful during a test, but
  hard to reason about for crash-safety analysis. fstab is grep-able,
  predictable, and survives reboot.
- **Why we don't kill mgmt-clients (browsers) explicitly:** the
  FastAPI server on old master stops listening, the browser's
  WebSocket disconnects, the user re-loads with the new master's
  hostname. Acceptable for the operator-supervised role-transfer
  scenario.

---

## Updated source citations (deltas from the upstream Sources section)

### DRBD 9 — peer-removal helpers (new)
- [LINBIT/drbd-utils — `user/v9/drbdadm_adjust.c` line 858–868](https://github.com/LINBIT/drbd-utils/blob/master/user/v9/drbdadm_adjust.c#L858-L868) — `adjust_net()` schedules `del_peer_cmd` for any kernel connection without a config match.
- [LINBIT/drbd-utils — `user/v9/drbdadm_adjust.c` line ~806](https://github.com/LINBIT/drbd-utils/blob/master/user/v9/drbdadm_adjust.c) — comment `/* disconnect implicit by del-peer */`.
- [drbdadm-9.0(8) — `--dry-run` documentation](https://manpages.debian.org/testing/drbd-utils/drbdadm-9.0.8.en.html) — recommended pre-flight check before `adjust`.
- [drbdsetup-9.0(8) — `forget-peer` semantics](https://manpages.debian.org/testing/drbd-utils/drbdsetup-9.0.8.en.html) — "The connection must be taken down before this command may be used. In case the peer re-connects at a later point a bit-map based resync will be turned into a full-sync."

### Garage — graceful drain (new)
- [Garage source — `src/block/resync.rs` lines 362–510](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/block/resync.rs) — offload-then-delete logic: query `NeedBlockQuery` on new owners, send `PutBlock`, then delete locally.
- [Garage source — `src/rpc/layout/history.rs` lines 79–115, 270–302](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/rpc/layout/history.rs) — `apply_staged_changes()` pushes new version onto `versions`; old versions retained until `sync_ack_map_min` advances.
- [Garage source — `src/rpc/rpc_helper.rs` ~line 570](https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main/src/rpc/rpc_helper.rs) — `block_read_nodes_of()` falls back to `old_versions` in reverse for blocks not yet at new owner.
- [Garage Operations — Recovering from failures](https://garagehq.deuxfleurs.fr/documentation/operations/recovering/) — `layout remove` + `layout apply` documented procedure.
- [Garage Operations — Durability and repairs](https://garagehq.deuxfleurs.fr/documentation/operations/durability-repairs/) — `worker list`, `worker set`, `block list-errors`, `repair tables`, `repair blocks`.

### NFS server / client (already cited above; reinforced)
- [`exports(5)`](https://manpages.debian.org/testing/nfs-kernel-server/exports.5.en.html) — `rw,sync,no_root_squash,no_subtree_check`.
- [`nfs(5)` — soft / timeo / retrans](https://manpages.debian.org/testing/nfs-common/nfs.5.en.html) — client-side timeout semantics.

---

## `drbd_demote_to_local(tier, remove_meta=False)`

### Top-of-section summary
The inverse of `promote_local_to_drbd_master`. Turns a stand-alone
DRBD resource on this node back into a plain local LV mount. The
underlying XFS filesystem on the data LV is preserved byte-for-byte
because external metadata never touched it during the DRBD lifetime.
After this function returns, `/dev/drbd<minor>` is gone, the local LV
is mounted at `/var/lib/bedrock/local/<tier>`, and `/bedrock/<tier>`
points at the local mount.

Used at the **end of the cluster collapse path** when the last node
goes from "single-peer DRBD" to "no DRBD at all." Also useful for
operator recovery: if a node ends up in a bad DRBD state with no
peers and the meta-disk is consistent, demoting to local restores
service via the underlying XFS.

Pre-conditions:
- The resource has no connected peers. (If any peer is still
  connected, run `drbd_remove_peer` for it first.)
- The data LV's XFS has not been corrupted (caller's responsibility
  to know).

Post-conditions:
- `/etc/drbd.d/tier-<tier>.res` is removed (file moved to `.demoted`,
  then deleted on success). DRBD will not auto-up this resource on
  next boot.
- `/etc/fstab` has the local-LV mount line (no DRBD line).
- Resource is `down` in the kernel (no `/dev/drbd<minor>`).
- `/var/lib/bedrock/local/<tier>` is mounted.
- `/bedrock/<tier>` symlink → local mount (atomic swap).
- `cluster.json.tiers.<tier>.mode` = `local`.
- (optional, with `remove_meta=True`) `tier-<tier>-meta` LV is
  removed (~32 MB reclaimed).

### Visual flow

```
                    start (resource is single-peer DRBD on this node)
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  0. Verify no other peers connected — drbdsetup status     │
       │     If any peer-role line found: ABORT, return False.      │
       │     Caller must drbd_remove_peer them first.               │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  1. Stop NFS export of <tier>-drbd (best-effort)           │
       │     edit /etc/exports.d/bedrock-tiers.exports + exportfs   │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  2. mv /etc/drbd.d/tier-<t>.res → ...res.demoted           │
       │     ── persistent commit point: from this point on, a      │
       │     ── reboot will NOT bring the resource up               │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  3. /etc/fstab: drop DRBD-mount line, add local-LV line    │
       │     ── persistent state now reflects local-only intent     │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  4. drbdsetup down tier-<t>                                 │
       │     ── /dev/drbd<minor> goes away; data LV is freed        │
       │     (use drbdsetup not drbdadm: .res file is moved aside)  │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  5. mount /dev/<vg>/tier-<t> /var/lib/bedrock/local/<t>    │
       │     ── XFS is intact; same data the cluster was using      │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  6. atomic_symlink /bedrock/<t> → /var/lib/bedrock/local/<t>│
       │     ── public mountpoint now serves the local mount        │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  7. set_tier_state(<t>, mode=local) — cluster.json update  │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  8. (optional, remove_meta=True) lvremove tier-<t>-meta    │
       │     reclaim ~32 MB. Default keeps it in case the operator  │
       │     wants to re-promote later (re-run create-md on the     │
       │     existing meta LV would work).                          │
       └─────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                                    end
```

### Crash-safety analysis

| Crash point | Persistent state | Kernel state | Recovery on next boot |
|-------------|------------------|--------------|------------------------|
| Before step 2 | `.res` present, fstab has DRBD line | DRBD up | Boot → DRBD comes up → fstab mount works → cluster member resumes. No-op. |
| Between 2 and 3 | `.res.demoted`, fstab has DRBD line | DRBD up | Boot → DRBD won't auto-up (no .res). Mount fails (`/dev/drbd<minor>` missing). Operator: run `drbd_demote_to_local` again — it picks up at step 3. |
| Between 3 and 4 | `.res.demoted`, fstab has local-LV line | DRBD still up (LV held) | Boot → DRBD won't auto-up. fstab mount of local LV: succeeds. DRBD never came up — no conflict. End state correct. |
| Between 4 and 5 | as above | DRBD down | Boot → DRBD won't auto-up (no .res). fstab mount works. End state correct. |
| Between 5 and 6 | as above, mounted | DRBD down | Boot → fstab re-mount; symlink flip on next op. Edge case: `/bedrock/<tier>` momentarily points at the old DRBD mountpoint that no longer exists. Fix: re-run; idempotent. |
| After 6 | persistent state fully reflects local | DRBD down | Boot → arrives directly at end state. |

In every case, persistent state encodes the operator's intent;
re-running the function is idempotent and converges.

### Why `drbdsetup` (not `drbdadm`) for step 4

`drbdadm down <res>` requires `/etc/drbd.d/<res>.res` to be present
(drbdadm reads it to translate the resource name). We deliberately
moved the .res file aside in step 2 so that a crash between 2 and 4
won't leave DRBD bringing the resource up on next boot. After that
move, only `drbdsetup down <name>` works (it operates on kernel
state directly, no .res needed).

### When NOT to use this function

- Resource has connected peers. Run `drbd_remove_peer` for each first.
- The data LV's XFS has been corrupted — demote will mount a corrupt
  filesystem. Run `xfs_repair` first if there's reason to suspect.
- You want to preserve cluster status of "this is a DRBD resource we
  may re-add peers to later." `drbd_demote_to_local` removes the
  resource entirely; re-adding peers later means re-running
  `promote_local_to_drbd_master` from scratch.

---

## `migrate_scratch_out_of_garage(verify_md5=True, keep_garage=False)`

### Top-of-section summary
Migrates the scratch tier out of Garage and into a plain local LV;
optionally stops + decommissions Garage entirely. Used at the **end
of the cluster collapse path** when the last node returns to single-
node operation and Garage is no longer needed.

The migration uses `rsync` through the s3fs FUSE mount as the data
path (no new dependencies; s3fs is already mounted). Optional MD5
verification compares manifests of the source (s3fs view) and
destination (local LV) before the symlink commit.

Pre-conditions:
- This node is the only Garage cluster member. (After
  `garage_drain_node` has drained every other node and stopped Garage
  on them.)
- `/var/lib/bedrock/local/scratch`'s underlying LV exists. (Created
  in `setup_n1`; may currently be unmounted because s3fs is using
  the public symlink.)
- The local thin pool has at least 1.1× the current scratch dataset
  size free.

Post-conditions:
- All scratch data copied to the local LV.
- `/bedrock/scratch` symlink → local LV mount.
- s3fs is unmounted; fstab line for s3fs is gone.
- `cluster.json.tiers.scratch.mode` = `local`.
- (default) Garage daemon stopped + disabled, garage data LV
  removed, /var/lib/garage cleaned, /etc/garage.toml removed,
  systemd unit removed.
- (with `keep_garage=True`) Garage left running for unrelated uses;
  only scratch data was migrated out.

### Visual flow

```
                        start (scratch is on Garage RF=1, single node)
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  0. Pre-flight — local LV exists, thin pool has 1.1× of   │
       │     scratch dataset free space                             │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  1. mkfs.xfs (if needed) + mount local scratch LV at      │
       │     /var/lib/bedrock/local/scratch                         │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  2. rsync -aHX --inplace /bedrock/scratch/ → local/scratch │
       │     (the whole dataset, can take time for many GB)         │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  3. (optional, verify_md5=True): generate sorted manifests │
       │     of both sides, compare. Diff → /tmp/scratch-md5-*.log  │
       │     and raise. (Default ON; can disable for huge datasets) │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  4. atomic_symlink /bedrock/scratch → local mount          │
       │     ── COMMIT POINT: new opens go to local; old opens via  │
       │     ── s3fs (if any) keep working until they close         │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  5. WAIT — poll lsof +D /var/lib/bedrock/mounts/scratch-s3fs│
       │     until it shows 0 open file descriptors. Bounded by     │
       │     a 120s safety timeout (then lazy umount).              │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  6. umount s3fs; drop the fuse.s3fs line from /etc/fstab   │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  7. set_tier_state(scratch, mode=local) — cluster.json     │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
       ┌────────────────────────────────────────────────────────────┐
       │  8. (default keep_garage=False): systemctl stop+disable   │
       │     garage; lvremove garage-data; rm /var/lib/garage,     │
       │     /etc/garage.toml, /etc/systemd/system/garage.service  │
       └────────────────────────────────────────────────────────────┘
                                          │
                                          ▼
                                        end
```

### Why two rsync passes? (Per Tommy's question — actually one + symlink swap)

In the planned reverse path the scratch tier is read-mostly during
the migration window: nothing is actively *writing* (we're collapsing
a cluster). One rsync pass is sufficient because:

- All sources of scratch writes (other Garage members, other Bedrock
  nodes) have already been removed by `garage_drain_node` cycles.
- The only writer is the local node itself (this one). If the operator
  isn't actively producing new scratch content during the migration,
  one pass captures everything.

If we were doing this on a *running* cluster (e.g. converting scratch
storage backend mid-flight without bringing down workloads), we'd add
a second rsync pass right after the symlink swap to catch any writes
that happened during pass 1. The function's docstring mentions
"twice" — that's the future-proof variant; the current implementation
does pass 1 + verify + swap which is correct for the cluster-collapse
case.

### Handling open file handles during the swap (Tommy's specific concern)

`atomic_symlink()` in step 4 uses `rename(2)`, which is POSIX-atomic.
Its semantic is:

- **Existing fds opened via the OLD symlink target (s3fs path)
  continue working** on the s3fs inode until they're closed. POSIX
  guarantees this: the kernel pins inodes by fd, not by path.
- **New `open()` calls** through `/bedrock/scratch` follow the new
  symlink target (local LV).

So step 5 is the "wait for old-path opens to drain" check:
`lsof +D /var/lib/bedrock/mounts/scratch-s3fs` lists every fd whose
path is under that directory. When the count hits 0 (or just the
header line), nothing is using s3fs anymore and we can safely umount
it.

In a clean cluster-collapse run where no service is actively writing
scratch, this drains in seconds. In a busier scenario it could take
longer; the 120-s safety timeout falls back to `umount -l` (lazy
unmount), which detaches the mount from the namespace immediately
and lets the kernel clean up when the last fd closes.

### Why `--inplace` on rsync

`rsync --inplace` writes directly to the destination file (vs.
default which writes to a temp file and atomically renames). For
scratch data with no concurrent readers — which is our case — this
is faster (no second copy, no extra space) and safe (the destination
is brand new, no existing readers).

If we were migrating to a directory containing live data being read
by other processes, we'd drop `--inplace` to keep the rename-atomic
semantics. Not our case here.

### Crash-safety analysis

| Crash point | Persistent state | What happens on reboot |
|-------------|------------------|------------------------|
| Between 1 and 4 | s3fs still active, local LV mounted but unused | Boot → fstab has both lines (s3fs + new local LV). Both mount fine. `/bedrock/scratch` still points at s3fs. Re-run function: rsync sees most data already there, picks up from where it left off (idempotent). |
| Between 4 and 6 | `/bedrock/scratch` → local LV; fstab still has s3fs line | Boot → s3fs mounts again (per fstab); local LV mounts; `/bedrock/scratch` already points at local. Garage starts. Re-run function from step 5: drains s3fs handles, umounts, removes fstab line, stops Garage. Clean. |
| Between 6 and 8 | local-only state in fstab and cluster.json; Garage still running | Boot → garage starts but nothing uses it (no fstab s3fs line). Re-run function from step 8: stops + cleans Garage. |
| After 8 | full local-only, Garage gone | Boot → arrives at end state. |

In every case, re-running the function from scratch converges. The
only case where it doesn't fully roll back automatically is if the
operator wants to *abort* the migration and put scratch back on
Garage — that's a separate operation (re-create the s3fs fstab line,
restart Garage if disabled).

### When NOT to use this function

- Multiple Garage cluster members are still alive. The function
  assumes single-node Garage (we're collapsing). For a multi-node
  cluster, just stop using s3fs on this node — use
  `garage_drain_node` to leave the cluster instead.
- The local LV's space is insufficient. The function checks at step 0
  and aborts; operator must extend the thin pool first.
- The s3fs mount is broken (e.g. pointing at a dead Garage). Migration
  reads through s3fs, so a hung s3fs hangs the migration. Restart
  Garage and re-mount s3fs against `127.0.0.1:3900` first.

---

## Updated source citations (deltas from the upstream Sources section)

### DRBD 9 — demotion / metadata
- [`drbdsetup down`](https://manpages.debian.org/testing/drbd-utils/drbdsetup-9.0.8.en.html) — operates by name in kernel state, does not require the `.res` file. Used by `drbd_demote_to_local` after the .res has been moved aside.
- [DRBD external metadata](https://linbit.com/drbd-user-guide/drbd-guide-9_0-en/#s-external-meta-data) — preserves the underlying data LV's filesystem byte-for-byte; that's what makes `drbd_demote_to_local` zero-copy on the data LV side (the inverse of `promote_local_to_drbd_master`).
- [`drbd-systemd` units](https://github.com/LINBIT/drbd-utils/tree/master/scripts) — the `drbd.service` iterates `/etc/drbd.d/*.res` at boot. Removing the .res from that directory prevents auto-up; this is the persistent-state commit point in `drbd_demote_to_local`.

### POSIX semantics — atomic rename + open fds
- [`rename(2)` man page](https://man7.org/linux/man-pages/man2/rename.2.html) — atomic on the same filesystem. Used by `atomic_symlink()`.
- [`open(2)` man page on path resolution](https://man7.org/linux/man-pages/man2/open.2.html) — paths are resolved at open time; in-flight fds reference inodes, not paths. This is what gives the symlink-swap its "old fds keep working" property.

### rsync — `--inplace` semantics
- [`rsync(1)` — `--inplace` option](https://manpages.debian.org/testing/rsync/rsync.1.en.html#opt--inplace) — writes destination files in place rather than via temp + rename. Required when the destination is on a separate filesystem or when temp space is limited; safe when destination has no concurrent readers.

### lsof — open-file detection
- [`lsof(8)` — `+D` recursive directory check](https://manpages.debian.org/testing/lsof/lsof.8.en.html) — used to detect whether any process still has files open under the s3fs mount before unmounting.
