# installer/lib/seaweedfs.py

SeaweedFS lifecycle helpers — Bedrock's S3 stack. The module renders all
SeaweedFS config (master, filer, s3, env), checks the binary is installed, and
starts/stops the SeaweedFS sub-roles on a node. The volume server + s3 gateway
run on every node; the master is a Raft set on the lowest-octet nodes; the filer
plus its s3 are a singleton that rides the `.254` cluster VIP. Install paths and
the orchestrator call the config renderers + `promote_to_master_volume_host`; the
filer promote/demote pair is driven by `cluster_arbiter` alongside the arbiter
rqlite move. It reads membership from `cluster_state.load_cluster()` +
`/etc/bedrock/state.json` and never writes rqlite. Per-collection replication
policy is set separately, via `weed shell` — the Bedrock CLI exposes it as
`bedrock storage tier <name> replication=…`.

Key paths and ports (module constants):
- `weed` binary: `/usr/local/bin/weed` (`WEED_BIN`).
- Local storage: `/var/lib/bedrock/seaweedfs/{volumes,master}`.
- Filer metadata (leveldb3): `/var/lib/bedrock/cluster/seaweedfs/` (on the
  cluster-singleton DRBD volume, so it moves with the master role).
- Config: `/etc/bedrock/seaweedfs-master.toml`, `/etc/seaweedfs/filer.toml`,
  `/etc/bedrock/seaweedfs-s3.json`, `/etc/bedrock/seaweedfs.env`.
- Ports: master 9333 / grpc 19333; volume 8080 / grpc 18080; filer 8888 / grpc
  18888; s3 8333.
- `FUSE_MOUNTPOINT = /mnt/bedrock`; FUSE-mount unit `bedrock-fuse-mount.service`.

## Functions / Classes

### `ensure_install() -> None`
Verify `weed` is present and create the local directory tree.
- **In:** none.
- **Out:** `None`. Raises `RuntimeError` if `/usr/local/bin/weed` is missing.
  `mkdir -p` (mode 0755) on `SEAWEEDFS_HOME`, `VOLUME_DIR`, `MASTER_DIR`.
  Idempotent.

### `write_master_config() -> None`
Render `/etc/bedrock/seaweedfs-master.toml`.
- **In:** none (reads cluster snapshot + state.json indirectly).
- **Out:** `None`. Writes the master.toml (`[master.maintenance] scriptInterval`
  + `[master.replication] defaultReplication`). Raises `RuntimeError` if this
  node's loopback IP is unknown. `defaultReplication` is `000` at N≤1, else
  `001`. Deterministic / idempotent across nodes.

### `write_filer_config() -> None`
Render `/etc/seaweedfs/filer.toml` pinning the leveldb3 metadata store.
- **In:** none.
- **Out:** `None`. Writes filer.toml with `[leveldb3] enabled=true` and
  `dir = /var/lib/bedrock/cluster/seaweedfs`; `mkdir -p` on `FILER_HOME` and the
  toml's parent dir.

### `write_env_file(*, volume_max: int = 50, disk_type: str = "") -> None`
Render `/etc/bedrock/seaweedfs.env`, consumed by every `weed` systemd unit.
- **In:** `volume_max` → max volumes per directory; `disk_type` → operator class
  for this node's volume server (`ssd`/`hdd`).
- **Out:** `None`. Atomic write (tmp + `os.replace`) of `SEAWEED_LOOPBACK_IP`,
  `SEAWEED_FILER_VIP` (`.254`), `SEAWEED_MASTER_PEERS` and `SEAWEED_FILER_MASTERS`
  (both the Raft master `ip:9333` list, or `none`), `SEAWEED_VOLUME_DISK_TYPE`,
  `SEAWEED_VOLUME_MAX`. Raises `RuntimeError` if loopback IP unknown. Idempotent.

### `write_s3_config() -> None`
Render `/etc/bedrock/seaweedfs-s3.json` with an `admin` + `anonymous` identity.
- **In:** none.
- **Out:** `None`. Writes JSON: `admin` with derived credentials and actions
  `[Admin, Read, Write, List, Tagging]`, and `anonymous` (no credentials) with
  `[Read, Write, List, Tagging]`. `mkdir -p` on the parent dir. Admin creds are
  derived from `/etc/bedrock/cluster.key` (see `_derive_admin_credentials`).

### `is_filer_active() -> bool`
- **In:** none. **Out:** `True` if `bedrock-weed-filer.service` is active
  (`systemctl is-active --quiet`).

### `promote_to_filer_host() -> None`
Start the filer + s3 gateway on this node.
- **In:** none. **Out:** `None`. `systemctl reset-failed` then `start` of
  `bedrock-weed-filer.service` and `bedrock-weed-s3.service`. Idempotent. Called
  by `cluster_arbiter.promote_to_arbiter_host()` after the cluster-singleton
  volume is mounted.

### `demote_filer_host() -> None`
Stop the filer + s3 gateway on this node.
- **In:** none. **Out:** `None`. `systemctl stop` of s3 then filer. Idempotent.
  Called by `cluster_arbiter.demote_arbiter_host()` before the volume unmounts.

### `reconcile_master_config() -> None`
Re-render the env file + master.toml from the current cluster snapshot.
- **In:** none. **Out:** `None`. Calls `write_env_file()` then
  `write_master_config()`; swallows `RuntimeError` (cluster.json not ready,
  retried on the next revision). Called by the orchestrator's revision-watcher.

### `promote_to_master_volume_host() -> None`
Enable/start the always-on volume + s3 units, and the master unit only if this
node is in the Raft set.
- **In:** none. **Out:** `None`. `systemctl reset-failed` on all four weed units,
  then `enable --now` for `bedrock-weed-volume` + `bedrock-weed-s3` on every node.
  If this node's loopback is in `_master_set()`, `enable --now`
  `bedrock-weed-master`; otherwise `disable --now` it. Idempotent. Called by
  install / orchestrator on every node.

### `ensure_iso_library_mount() -> None`
Install + (re)start the FUSE-mount unit that mounts the filer at `/mnt/bedrock`.
- **In:** none. **Out:** `None`. Writes `/etc/systemd/system/bedrock-fuse-mount.service`
  (a `Type=simple` service running `weed mount -filer=<.254>:8888 -dir=/mnt/bedrock
  -allowOthers -dirAutoCreate`); `daemon-reload` only when the unit text changed;
  `enable --no-block` then `start`/`restart --no-block`. `mkdir -p` on
  `/mnt/bedrock`. Never blocks the caller (weed retries internally).

### `init_collections() -> None`
One-shot per cluster: configure the five path policies via `weed shell`.
- **In:** none. **Out:** `None`. Runs `weed shell -master <first-master>:9333`
  feeding five `fs.configure ... -apply` commands; returns early (logging a
  warning) if the weed binary is missing or `_master_set()` is empty.
  Replication is clamped to what the cluster size can satisfy. Idempotent
  (`-apply` overwrites the prior config for the same `locationPrefix`). Called
  from the `seaweedfs_init_collections` cluster_init step.

### `seed_iso_library(source_dir: Path = Path("/opt/bedrock/iso")) -> None`
Copy staged ISOs into the filer's `/iso/` subtree.
- **In:** `source_dir` → directory of `*.iso` files to seed.
- **Out:** `None`. Returns early if `source_dir` is absent/not a dir or holds no
  ISOs. Ensures the FUSE mount is up (`ensure_iso_library_mount()` if
  `/mnt/bedrock` is not a mount), `mkdir -p /mnt/bedrock/iso`, then `cp -n` each
  ISO that is not already present. Idempotent. Runs on the arbiter-host after the
  filer is up.

### Private helpers (mechanical)
`_read_cluster()` / `_read_state()` load the cluster snapshot (via
`cluster_state.load_cluster()`) and `/etc/bedrock/state.json`, returning `{}` on
any error. `_peer_loopbacks()` returns other nodes' loopback IPs in sorted order;
`_my_loopback()` this node's; `_loopback_octet(ip)` the last octet (9999 on parse
failure) as the sort key. `_n_cluster_nodes()` clamps the node count to ≥1.
`_master_set()` returns the deterministic Raft master member set. `_filer_vip()`
derives `100.X.Y.254` from the local loopback. `_derive_admin_credentials()`
returns `(access_key, secret_key)`. `_systemctl(action, unit)` / `_svc_active(unit)`
wrap `systemctl`.

## How it works

**Topology this module enforces:**

```
              ┌──────────── every node ─────────────┐
              │  weed-volume  (0.0.0.0:8080)         │   stores file bytes
              │  weed-s3      (0.0.0.0:8333)         │   S3 gateway
              └──────────────────────────────────────┘
   weed-master ── Raft set, lowest-octet nodes:
        N=1 → 1 master    N=2 → 1 master    N≥3 → 3 masters
   weed-filer + its s3 ── singleton on the .254 cluster VIP
        (leveldb3 metadata on the cluster-singleton DRBD volume,
         so it rides the master role; promote/demote via cluster_arbiter)
```

**Master subset selection.** SeaweedFS master uses Raft, which refuses an
even-numbered peer set. Both selectors sort all loopbacks by last octet and pick
a deterministic subset:

```
sorted loopbacks (by last octet) = [lo0, lo1, lo2, lo3, ...]
  N <= 1  → [lo0]                 (self only)
  N == 2  → [lo0]                 (lowest-octet only; single-node Raft)
  N >= 3  → [lo0, lo1, lo2]       (lowest 3)
```
`write_master_config` computes its peer list as "largest odd ≤ N" (so N=4 → 3,
N=5 → 5), while `_master_set` — which env rendering and the start logic use —
caps strictly at 3. They agree for N≤3 and pin the master count that the units
actually run to at most 3. Because every node sorts the same loopback list, each
node renders an identical master.toml and agrees on the same master set without
coordination. A node not in the set still runs as a volume-only peer.

**Config rendering guards.** The renderers that need this node's loopback IP
(`write_master_config`, `write_env_file`) raise `RuntimeError` when state.json
has no `loopback_ip`, so config never gets written with a blank bind address.
`reconcile_master_config()` is the only caller that swallows that error — it is
fired by the revision-watcher and simply retries on the next cluster revision.
`write_env_file` writes via tmp + `os.replace` so a concurrent reader never sees
a half-written env file.

**Replication clamping.** Replication must be satisfiable by the live node count
or SeaweedFS hangs writes at volume-assign time. Both the cluster default
(`write_master_config`: `000` at N≤1 else `001`) and the per-path policies
(`init_collections`) follow the same rule:

```
            N=1     N=2     N>=3
scratch     000     000     000
iso/        000     001     001     (standard)
templates/  000     001     001
snapshots/  000     001     001
backups/    000     001     002     (critical)
```
This keeps e.g. `/iso/` from being pinned to `001` on an N=1 box, which would
brick every ISO upload.

**Start/stop sequencing.** `promote_to_master_volume_host()` always
`reset-failed`s the four weed units first (a unit that crash-looped before the
env file existed otherwise sits in `StartLimitBurst`), then `enable --now`s
volume + s3 unconditionally, then `enable --now` (in-set) or `disable --now`
(out-of-set) the master. All `systemctl` calls use `check=False` and silence
stderr — the unit files have an empty `WantedBy=`, so `enable` prints "no
installation config" while still creating the runtime symlink that `--now`
needs. The filer pair is started/stopped separately by
`promote_to_filer_host` / `demote_filer_host`, which `cluster_arbiter` calls
when the `.254` VIP and its DRBD volume move:

```
  arbiter promote: mount cluster-singleton DRBD → promote_to_filer_host()
                       (reset-failed → start filer → start s3)
  arbiter demote:  demote_filer_host() → unmount cluster-singleton DRBD
                       (stop s3 → stop filer)
```

**Shared FUSE namespace.** `ensure_iso_library_mount()` installs a
`Type=simple` service running `weed mount` against `<.254>:8888`. Every node
points at the same VIP, so the mount target string is stable when the
arbiter-host flips — the VIP moves and the FUSE client auto-reconnects. The unit
is only rewritten + `daemon-reload`ed when its text changes, and all start/enable
calls use `--no-block` so the caller never waits on the mount coming up. libvirt
then sees `/mnt/bedrock/iso/<name>.iso` as a local path, replicated cluster-wide
by the volume servers.

**Admin credentials.** `_derive_admin_credentials()` reads
`/etc/bedrock/cluster.key` (32 random bytes; a trailing newline on a 33-byte file
is trimmed) and derives `access_key` (first 20 hex of
`sha256("bedrock-s3-access\0" + key)`) and `secret_key` (full hex of
`sha256("bedrock-s3-secret\0" + key)`). The two domain-tagged hashes mean leaking
one doesn't reveal the other, and every node derives the same admin identity from
the shared key. When the key file is absent (very early bootstrap) it falls back
to fixed testbed creds `("bedrock-admin", "bedrock-admin-secret")`.

**CLI entry point.** Running the module directly takes one subcommand:
`config` (default: `ensure_install` + `write_env_file` + the four `write_*`
renderers), `promote`, `demote`, or `reconcile`. Errors print to stderr and exit
non-zero.

## Why
The volume + s3 gateway run everywhere so any node can serve S3 and store bytes,
while the filer is a single writer on the VIP — pointing every FUSE mount and S3
client at `.254` keeps namespace metadata consistent and lets it follow the
master role over DRBD without changing any client's target address.
