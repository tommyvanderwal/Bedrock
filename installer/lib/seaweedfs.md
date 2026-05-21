# `seaweedfs.py`

**Module purpose.** Install + configure SeaweedFS. The locked v1.0
topology (see `docs/storage-architecture.md`):

- **`weed-filer`** (port 8888) — POSIX namespace + S3 IAM
  identities, single leveldb3 on the DRBD-replicated tier-critical
  volume. Runs **only on the current arbiter-host** bound to
  `.254/32`. Failover = DRBD primary handoff + filer restart on the
  new host.
- **`weed-master`** (port 9333) — Raft-3 across exactly three
  regular cluster nodes (NOT on `.254`). Membership persisted in
  the rqlite table `seaweed_master_membership` and reshuffled by
  the **calm orchestrator loop** when a master-bearing node
  leaves. Re-shuffles are deliberate (resource-aware), not on the
  failover-critical path.
- **`weed-volume`** (port 8080) — bytes. Runs on **every** node,
  bound `0.0.0.0`. Data dir lives in the local `tier-bulk` and
  `tier-fast` thinpools (`bedrock-weed-volume-bulk` and
  `-fast` LVs); volume server registers per-LV mount with the
  master so the master's topology view is per-disk.
- **`weed-s3`** (port 8333) — S3 API gateway. Runs on **every**
  node, bound `0.0.0.0`. Authenticates against IAM identities
  stored inside the filer DB (`-iam.filerBucketsPath=/buckets`),
  so the S3 gateway needs filer reachability — which is always at
  `.254:8888`.
- **`weed mount`** (FUSE) — every node mounts the filer at
  `/mnt/bedrock` pointing at `.254:8888`. Same mount unit on every
  node — no per-node templating.

Three collections back the three replication policies:

| Collection | Replication code | Copies | Avail at N≥ | Purpose                |
|------------|------------------|--------|-------------|------------------------|
| `scratch`  | `000`            | 1      | 1           | RAID0; ephemeral.      |
| `standard` | `001`            | 2      | 2           | Default; ISOs, templates. |
| `critical` | `002`            | 3      | 3           | Customer backups.       |

## Constants

- `WEED_BIN = /opt/bedrock/bin/weed`.
- `SEAWEEDFS_HOME = /var/lib/bedrock/seaweedfs` — local-FS root for
  master raft data (`master/raft/`).
- `FILER_HOME = /var/lib/bedrock/cluster/seaweedfs` —
  DRBD-replicated filer leveldb3 + S3 IAM bucket files. Mount of
  `/dev/drbd1101` (tier-critical) lives at
  `/var/lib/bedrock/cluster`; this dir is the filer's subtree of
  it. Moves with the arbiter role.
- `VOLUME_DATA_DIRS = ["/var/lib/bedrock/weed-volume-fast",
  "/var/lib/bedrock/weed-volume-bulk"]` — mount points for the two
  local volume LVs.
- `MASTER_TOML = /etc/bedrock/seaweedfs-master.toml`.
- `FILER_TOML = /etc/seaweedfs/filer.toml`.
- `S3_CONFIG = /etc/bedrock/seaweedfs-s3.toml`.
- `SEAWEED_ENV = /etc/bedrock/seaweedfs.env` (consumed by all
  systemd units).
- `MASTER_PORT = 9333`, `VOLUME_PORT = 8080`,
  `FILER_PORT = 8888`, `S3_PORT = 8333`.
- `FUSE_MOUNTPOINT = /mnt/bedrock` — uniform FUSE mount on every
  node. Subdirs `iso/`, `templates/`, `snapshots/`, `backups/`,
  `scratch/` map to filer buckets bound to collections via
  `weed shell` (`fs.configure -locationPrefix=/iso/
  -collection=standard -replication=001`, etc.).

## Functions

### Bootstrap

- `ensure_install()` — checks `/opt/bedrock/bin/weed` exists.
  install.sh stages it from the ISO payload; this function raises
  a clear error if it's missing.

### Membership rules (calm orchestrator owns these)

- `_select_master_set(cluster_nodes) -> list[str]` — pure
  function. Given the current cluster's node list, returns the
  three nodes that SHOULD be the weed-master Raft set. Default:
  the three lowest-octet loopbacks. Tie-broken by node name.
  Smaller clusters: N=1 → 1 master, N=2 → 1 master (no Raft;
  single-leader), N≥3 → 3 masters. Called by the calm orchestrator
  loop on cluster-membership changes; NEVER by netd.
- `seaweed_master_membership` table in rqlite — authoritative set
  of master nodes. Written by the orchestrator when
  `_select_master_set` output differs from current state.
- `is_master_node(node_name) -> bool` — reads the table.

### Config rendering

- `write_master_config()` — render `seaweedfs-master.toml` with
  the current `seaweed_master_membership` list as Raft peers,
  default replication `001`, three collection definitions (scratch
  / standard / critical).
- `write_filer_config()` — render `filer.toml` with `[leveldb3]
  dir = /var/lib/bedrock/cluster/seaweedfs` and
  `master = "<vip>:9333"` (where `<vip>` is the cluster's mgmt
  VIP form — `100.X.Y.254` is the filer's bind, but the filer's
  master client points at one of the actual master loopbacks,
  rendered from the membership table).
- `write_s3_config()` — render `seaweedfs-s3.toml` with
  `iam.filerBucketsPath = "/buckets"` so identities live in the
  filer DB. No sidecar identities.json.
- `write_env_file(*, role_set)` — render `seaweedfs.env` with the
  per-role binds:
  - `SEAWEED_MASTER_BIND` = node's loopback IP (when this node is
    a master) else empty.
  - `SEAWEED_MASTER_PEERS` = comma-list of the master set's
    `ip:9333`. Empty when N=1 and this node is sole master.
  - `SEAWEED_VOLUME_BIND = 0.0.0.0` — always.
  - `SEAWEED_S3_BIND = 0.0.0.0` — always.
  - `SEAWEED_FILER_BIND` = `<vip>` only on the arbiter-host; the
    filer service starts on `.254` after the DRBD mount.
  - `SEAWEED_FUSE_FILER = <vip>:8888` — for the per-node mount
    unit. Identical on every node.

### Role transitions

- `promote_to_master_volume_host()` — called from
  `mgmt_install.install_full` (init) and `agent_install.install`
  (join). If this node is in `seaweed_master_membership`:
  `systemctl enable --now bedrock-weed-master`. Always:
  `systemctl enable --now bedrock-weed-volume bedrock-weed-s3
  bedrock-weed-mount`. Idempotent; safe to re-run on every
  orchestrator reconcile.
- `promote_to_filer_host()` — called by
  `cluster_arbiter.promote_to_arbiter_host` AFTER the DRBD volume
  is mounted at `/var/lib/bedrock/cluster`. Starts
  `bedrock-weed-filer`. The s3 gateway is **already** running
  (it's a per-node always-on service), but it'll start serving
  meaningful requests only once the filer at `.254:8888` is up.
- `demote_filer_host()` — called by
  `cluster_arbiter.demote_arbiter_host` BEFORE unmounting the
  DRBD volume. Stops `bedrock-weed-filer` only. The per-node
  weed-s3 keeps running — it'll error gracefully once the filer
  endpoint vanishes; clients retry against the new arbiter-host
  via the same `.254:8888` URL.
- `is_filer_active() -> bool` — short `systemctl is-active` check.

### FUSE mount (uniform across nodes)

- `ensure_fuse_mount()` — renders + applies
  `/etc/systemd/system/mnt-bedrock.mount`. Target:
  `weedfs#<vip>:8888/` mounted at `/mnt/bedrock`. Identical on
  every node — no per-node loopback substitution. Restarts on
  `.254`-VIP transitions are handled by the filer URL itself
  being constant; the mount only blips if the filer is briefly
  down during failover.
- `seed_iso_library(src_dir)` — copy any local ISOs (e.g.
  virtio-win.iso staged from the install payload) into
  `/mnt/bedrock/iso/` via the FUSE mount or via the S3 gateway.

### Collection setup (one-shot at cluster init)

- `init_collections()` — called once when N=1 starts up. Runs
  `weed shell` against the local master:
  ```
  fs.configure -locationPrefix=/scratch/   -collection=scratch  -replication=000 -apply
  fs.configure -locationPrefix=/iso/       -collection=standard -replication=001 -apply
  fs.configure -locationPrefix=/templates/ -collection=standard -replication=001 -apply
  fs.configure -locationPrefix=/snapshots/ -collection=standard -replication=001 -apply
  fs.configure -locationPrefix=/backups/   -collection=critical -replication=002 -apply
  ```
  Idempotent; safe to re-run.

### Helpers

- `_systemctl(action, *units)` — subprocess wrapper.
- `_read_cluster() / _read_state()` — short JSON reads.
- `_vip_address() -> str` — derives `100.X.Y.254` from
  `state.json`'s cluster CGNAT.

## What this module is NOT responsible for

- **Master Raft membership decisions.** Calm orchestrator only.
  This module only consumes `seaweed_master_membership` from
  rqlite and runs the appropriate systemd actions; it never picks
  who's a master.
- **Filer placement.** The filer follows the arbiter VIP; the
  arbiter VIP is owned by `cluster_arbiter.py` per
  `docs/cluster-quorum-spec.md`. This module's `promote_to_filer_host`
  is just a thin wrapper around `systemctl start
  bedrock-weed-filer` AFTER the DRBD mount succeeded.
- **Replication-code policy decisions.** Three collections, three
  fixed codes (000/001/002), set once at `init_collections`.
  Operators can `weed shell` to add more collections; not a
  Bedrock-managed feature.

## Failure modes

| Symptom                                  | Where to look                                          |
|------------------------------------------|--------------------------------------------------------|
| S3 returns 503 right after failover      | Filer not up yet on new arbiter-host; `journalctl -u bedrock-weed-filer` |
| Volume server doesn't register with master | `SEAWEED_MASTER_PEERS` env points at a node that left; orchestrator needs to re-render env on master-set change |
| FUSE mount returns ENOTCONN              | Filer at `.254:8888` is down (during arbiter failover); auto-recovers when filer comes back |
| IAM auth fails on every S3 request       | `iam.filerBucketsPath` not configured; identities living in sidecar JSON instead of filer DB |
| Master Raft loses quorum                 | Calm orchestrator hasn't re-shuffled `seaweed_master_membership` after a node left; check rqlite table |
