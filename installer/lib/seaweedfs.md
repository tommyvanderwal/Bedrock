# `seaweedfs.py`

**Module purpose.** Install + configure SeaweedFS (the bulk-tier
S3 + filesystem layer). SeaweedFS has four components and this
module owns where each runs:

- **`weed-master`** (Raft, port 9333) — cluster metadata.
  Refuses to run with an even number of peers (Raft requirement).
  Runs on the largest-odd-numbered-subset of nodes by loopback
  octet: N=1→1, N=2→1, N=3→3, N=4→3, N=5→5, …
- **`weed-volume`** (port 8080) — stores the actual bytes. Runs
  on EVERY node.
- **`weed-filer`** (port 8888) — POSIX namespace + leveldb3
  metadata. Runs ONLY on the cluster master; data lives on the
  DRBD-replicated `/var/lib/bedrock/cluster/seaweedfs/`.
- **`weed-s3`** (port 8333) — S3-compatible gateway in front of
  the filer. Runs ONLY on the master; anonymous Read+Write
  permissions (operator can lock down via `weed shell`).

The filer + s3 follow the master role via
`cluster_arbiter.promote_to_filer_host()` /
`demote_filer_host()`; the master + volume subset is recomputed
on every `promote_to_master_volume_host()` call.

## Constants

- `WEED_BIN = /opt/bedrock/bin/weed`.
- `SEAWEEDFS_HOME = /var/lib/bedrock/seaweedfs` — local-FS root
  for master + volume data dirs.
- `FILER_HOME = /var/lib/bedrock/cluster/seaweedfs` — DRBD-
  replicated filer leveldb3 data. Moves with the master role.
- `MASTER_TOML = /etc/bedrock/seaweedfs-master.toml`,
  `FILER_TOML = /etc/seaweedfs/filer.toml`,
  `S3_CONFIG = /etc/bedrock/seaweedfs-s3.json`,
  `SEAWEED_ENV = /etc/bedrock/seaweedfs.env`.
- `MASTER_PORT = 9333`, `VOLUME_PORT = 8080`,
  `FILER_PORT = 8888`, `S3_PORT = 8333`.
- `ISO_MOUNTPOINT = /mnt/isos` — FUSE mount of the filer's
  `/isos` subtree, used as libvirt's ISO library.

## Functions

### Bootstrap

- `ensure_install()` — checks `/opt/bedrock/bin/weed` exists.
  install.sh stages it from the ISO payload; this function just
  raises a clear error if it's missing.

### Master subset rule

- `_loopback_octet(ip) -> int` — last octet of a `100.X.Y.Z`
  loopback; used as deterministic sort key.
- `_peer_loopbacks() -> list[str]` — every OTHER node's loopback
  IP from cluster.json (excluding self), sorted by name.
- `_my_loopback() -> str` — from state.json.

  All three are used by the subset rule:

  ```
  all_lo = sorted([self] + peers, by_octet)
  if N <= 1: master_subset = all_lo
  elif N == 2: master_subset = [all_lo[0]]  # only lowest-octet
  else: master_subset = all_lo[:N if N % 2 else N-1]
  ```

### Config rendering

- `write_master_config()` — render `/etc/bedrock/seaweedfs-master.toml`
  with the master subset's loopback list as Raft peers.
  Identical content on every node (so a node joining the subset
  doesn't need a separate config push).
- `write_filer_config()` — render `/etc/seaweedfs/filer.toml`
  with `[leveldb3] dir = /var/lib/bedrock/cluster/seaweedfs`.
- `write_s3_config()` — render `/etc/bedrock/seaweedfs-s3.json`
  with the anonymous-R+W identity.
- `write_env_file(*, volume_max=50, disk_type="")` — render
  `/etc/bedrock/seaweedfs.env` consumed by all four systemd
  units:
  - `SEAWEED_LOOPBACK_IP` — bind addr.
  - `SEAWEED_MASTER_PEERS` — comma-list of `ip:9333` for the
    master subset. `"none"` when the subset is a single node
    (master complains about "peer list contains only self").
  - `SEAWEED_FILER_MASTERS` — comma-list of `ip:9333` for the
    filer's master client.
  - `SEAWEED_VOLUME_MAX`, `SEAWEED_VOLUME_DISK_TYPE`.

### Role transitions

- `promote_to_master_volume_host()` — called from
  `mgmt_install.install_full` (init) and
  `agent_install.install` (join). Computes the master subset.
  If we're in: `systemctl enable --now bedrock-weed-{master,volume}`.
  If we're not: `systemctl disable --now bedrock-weed-master` +
  `systemctl enable --now bedrock-weed-volume`. Idempotent.
- `promote_to_filer_host()` — called by
  `cluster_arbiter.promote_to_arbiter_host` AFTER the DRBD volume
  is mounted at `/var/lib/bedrock/cluster`. Starts
  `bedrock-weed-{filer,s3}`. Idempotent.
- `demote_filer_host()` — called by
  `cluster_arbiter.demote_arbiter_host` BEFORE unmounting the
  DRBD volume. Stops s3 first (it holds open client connections
  to the filer), then filer.
- `is_filer_active() -> bool` — short `systemctl is-active`
  check.

### ISO library FUSE mount

- `ensure_iso_library_mount()` — renders + applies
  `/etc/systemd/system/mnt-isos.mount`. The mount target is
  `weedfs#<master_loopback>:8888/isos` (the filer's `/isos`
  subtree). Re-rendered on every orchestrator revision tick so
  the mount target follows the master across failovers.
  Uses `Type=simple` + `--no-block` for the systemctl ops
  because `weed mount` doesn't sd_notify and `start` would
  block 30 s otherwise.
- `seed_iso_library(src_dir)` — copy any local ISOs (e.g.
  virtio-win.iso staged from the install payload) into the
  filer's `/isos/` namespace via the S3 gateway.

### Helpers

- `_systemctl(action, *units)` — subprocess wrapper.
- `_read_cluster() / _read_state()` — short JSON reads.
