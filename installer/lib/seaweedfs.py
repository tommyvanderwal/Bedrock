"""SeaweedFS lifecycle helpers — Bedrock's unified S3 stack.

Filer metadata lives in leveldb3 on the cluster-singleton DRBD
volume so it moves with the mgmt-master role; switching to another
store backend (postgres, mysql, tikv, etcd) is bidirectional via
`weed shell` → fs.meta.save / fs.meta.load.

Components SeaweedFS-side:

  master    — cluster coordinator. Light. Runs on the Raft-3 member
              set (N=1→1, N=2→1, N≥3→3 lowest-octet nodes).
              Knows the volume topology.
  volume    — stores the actual file bytes (in "needle" files).
              One volume server per node, on every node.
  filer     — POSIX-style namespace on top of volumes. leveldb3
              metadata store. Single instance bound to the .254 VIP.
              Moves with mgmt-master role via cluster-singleton DRBD.
  s3        — S3 API gateway, depends on filer. Single instance
              co-resident with filer; also runs on every node.

Each component runs as its own bedrock-weed-* systemd unit, not a
single all-in-one process. weed-volume + weed-s3 run on every node;
weed-master runs on the Raft-3 set; weed-filer is a singleton on the
.254 VIP, started/stopped with the arbiter-host role.

This module provides:

  * ensure_install()          — drop /usr/local/bin/weed into place
  * write_master_config()     — render master.toml with cluster peers
  * write_filer_config()      — render filer.toml with the leveldb3
                                metadata store under
                                /var/lib/bedrock/cluster/seaweedfs/
  * write_env_file()          — render seaweedfs.env consumed by units
  * write_s3_config()         — render the S3 gateway identities
  * promote_to_master_volume_host() — enable volume+s3 (+master if in
                                the Raft-3 set) on this node
  * promote_to_filer_host()   — start filer+s3 on this node (called
                                from cluster_arbiter alongside the
                                arbiter rqlite promote)
  * demote_filer_host()       — stop filer+s3 (reverse)

What this module DOES NOT do: per-collection replication policy
config — that's per-collection at the API level (`weed shell` ->
`collection.create`). The Bedrock CLI exposes that as
`bedrock storage tier <name> replication=001` etc.
"""
from __future__ import annotations

import json
import logging
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

log = logging.getLogger("bedrock.seaweedfs")

WEED_BIN          = Path("/usr/local/bin/weed")
SEAWEEDFS_HOME    = Path("/var/lib/bedrock/seaweedfs")
VOLUME_DIR        = SEAWEEDFS_HOME / "volumes"
MASTER_DIR        = SEAWEEDFS_HOME / "master"
FILER_HOME        = Path("/var/lib/bedrock/cluster/seaweedfs")
FILER_DB          = FILER_HOME / "filer.db"

MASTER_TOML       = Path("/etc/bedrock/seaweedfs-master.toml")
FILER_TOML        = Path("/etc/seaweedfs/filer.toml")
S3_CONFIG         = Path("/etc/bedrock/seaweedfs-s3.json")
SEAWEED_ENV       = Path("/etc/bedrock/seaweedfs.env")

# Default ports — all SeaweedFS components bind to localhost +
# the node's loopback /32. External S3 access uses the front-end
# IP (operator's LAN); internal cluster traffic uses the mesh.
MASTER_PORT       = 9333    # HTTP API
MASTER_GRPC_PORT  = 19333
VOLUME_PORT       = 8080
VOLUME_GRPC_PORT  = 18080
FILER_PORT        = 8888
FILER_GRPC_PORT   = 18888
S3_PORT           = 8333    # S3 API (this is what external clients connect to)

CLUSTER_JSON      = Path("/etc/bedrock/cluster.json")
STATE_JSON        = Path("/etc/bedrock/state.json")


# ─────────────────────────────────────────────────────────────────────
# Install + config rendering
# ─────────────────────────────────────────────────────────────────────


def ensure_install() -> None:
    """Verify weed is installed (install.sh drops it into place);
    create the directory tree. Idempotent."""
    if not WEED_BIN.exists():
        raise RuntimeError(
            f"seaweedfs: {WEED_BIN} not found — install.sh should have "
            f"placed it. Re-run install or fetch manually from "
            f"https://github.com/seaweedfs/seaweedfs/releases."
        )
    for d in (SEAWEEDFS_HOME, VOLUME_DIR, MASTER_DIR):
        d.mkdir(parents=True, exist_ok=True, mode=0o755)


def _read_cluster(strong: bool = False) -> dict:
    try:
        from . import cluster_state
        return cluster_state.load_cluster(
            level="strong" if strong else "none")
    except Exception:
        return {}


def _read_state() -> dict:
    try:
        return json.loads(STATE_JSON.read_text())
    except Exception:
        return {}


def _peer_loopbacks() -> list[str]:
    """All other nodes' loopback IPs — used to construct the master
    peer list. Stable sorted order so every node renders an identical
    master.toml.

    Reads the Raft-committed node set (strong), not the local replica:
    the master-peer list MUST be the same on every node, and a render
    from a partial local view (e.g. mid-reboot before quorum reforms)
    would emit a self-only peer list, so that node's master never peers
    and its volume server never registers with the leader."""
    cluster = _read_cluster(strong=True)
    state = _read_state()
    me = state.get("node_name", "")
    out: list[str] = []
    for name, info in sorted((cluster.get("nodes") or {}).items()):
        if name == me:
            continue
        lo = (info or {}).get("loopback_ip", "")
        if lo:
            out.append(lo)
    return out


def _my_loopback() -> str:
    return (_read_state() or {}).get("loopback_ip", "")


def _loopback_octet(ip: str) -> int:
    """Last octet — deterministic sort key for odd-master-subset."""
    try:
        return int(ip.rsplit(".", 1)[-1])
    except (ValueError, IndexError):
        return 9999


def _n_cluster_nodes() -> int:
    """Number of nodes in cluster.json. Used by init_collections to
    pick a replication factor that the current cluster can actually
    satisfy — pinning /iso/ to 001 on an N=1 box bricks ISO uploads.
    Returns 1 if cluster.json is missing or empty (fresh install)."""
    cluster = _read_cluster()
    nodes = cluster.get("nodes") or {}
    return max(1, len(nodes))


def write_master_config() -> None:
    """Render the master.toml — peer list pointing at every other
    node's master grpc, defaultReplication based on cluster size
    so a fresh install just works.

    The master.toml is identical on every node (peers swap their
    own loopback for the others'), so render is deterministic and
    idempotent.
    """
    peers = _peer_loopbacks()
    my_lo = _my_loopback()
    if not my_lo:
        raise RuntimeError(
            "seaweedfs: my loopback IP unknown — state.json must be "
            "populated before SeaweedFS install"
        )

    # SeaweedFS master uses Raft; Raft refuses to start with an
    # even-numbered peer set ("Only odd number of masters are
    # supported"). Pick a deterministic odd subset of nodes:
    #   N=1 → 1 master  (self only)
    #   N=2 → 1 master  (lowest-loopback only)
    #   N=3 → 3 masters
    #   N=4 → 3 masters (lowest 3 by loopback octet)
    #   N=5 → 5 masters
    # If self is NOT in the elected master subset, this node still
    # joins as a volume-only server; the master service won't run
    # here. Volume server addresses ALL masters in mlist anyway.
    all_lo = sorted([my_lo] + peers, key=_loopback_octet)
    n_nodes = len(all_lo)
    if n_nodes <= 1:
        master_subset = all_lo
    elif n_nodes == 2:
        master_subset = all_lo[:1]
    else:
        # largest odd ≤ n_nodes
        odd_n = n_nodes if n_nodes % 2 == 1 else n_nodes - 1
        master_subset = all_lo[:odd_n]
    peers_arg = ",".join(f"{ip}:{MASTER_PORT}" for ip in master_subset)
    default_repl = "000" if n_nodes <= 1 else "001"

    MASTER_TOML.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""\
        # Bedrock-managed SeaweedFS master config — DO NOT edit by hand.
        # Generated from cluster.json + state.json.

        [master.maintenance]
        # SeaweedFS' built-in volume layout maintenance scan interval.
        # Default 4 hours is fine for Bedrock's quiet workload.
        scriptInterval = 17280

        [master.replication]
        # Cluster-default replication. Per-collection overrides apply
        # via `weed shell -> collection.create -> replication=…`.
        defaultReplication = "{default_repl}"
        """)
    MASTER_TOML.write_text(body)
    log.info("seaweedfs: wrote %s (peers=%s, default_repl=%s)",
             MASTER_TOML, peers_arg, default_repl)


def write_filer_config() -> None:
    """Render filer.toml — pins leveldb3 as the metadata store under
    /var/lib/bedrock/cluster/seaweedfs/, which lives on the
    cluster-singleton DRBD volume so it moves with the mgmt-master role.

    leveldb3 is the embedded store and is feature-complete for the
    POSIX-namespace + S3 use case. Other store backends (postgres,
    mysql, tikv, etcd) are also available; switching is bidirectional
    via `weed shell` → `fs.meta.save` / `fs.meta.load`.
    """
    FILER_HOME.mkdir(parents=True, exist_ok=True)
    FILER_TOML.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""\
        # Bedrock-managed SeaweedFS filer config — DO NOT edit by hand.
        #
        # The metadata store lives on the cluster-singleton DRBD volume
        # so it moves with the mgmt-master role. The embedded leveldb3
        # store is feature-complete for the POSIX-namespace + S3 use case.

        [leveldb3]
        enabled = true
        dir = "{FILER_HOME}"
        """)
    FILER_TOML.write_text(body)
    log.info("seaweedfs: wrote %s (filer db at %s)", FILER_TOML, FILER_HOME)


def _master_set() -> list[str]:
    """The Raft-3 weed-master member set: the three lowest-octet
    loopbacks. Computed locally from the cluster snapshot, so it works
    without rqlite (during install) and when membership state is empty.

    N=1 → 1 master; N=2 → 1 master (single-node Raft);
    N≥3 → 3 masters."""
    my_lo = _my_loopback()
    if not my_lo:
        return []
    all_lo = sorted([my_lo] + _peer_loopbacks(), key=_loopback_octet)
    n_nodes = len(all_lo)
    if n_nodes <= 2:
        return all_lo[:1]
    return all_lo[:3]


def _filer_vip() -> str:
    """The cluster-singleton VIP (.254/32) where the filer binds.
    Derived from this cluster's CGNAT /24."""
    my_lo = _my_loopback()
    if not my_lo:
        return ""
    # e.g. 100.X.Y.Z -> 100.X.Y.254
    return ".".join(my_lo.split(".")[:3] + ["254"])


def write_env_file(*, volume_max: int = 50,
                   disk_type: str = "") -> None:
    """Render /etc/bedrock/seaweedfs.env consumed by all weed
    systemd units. Variables:

      SEAWEED_LOOPBACK_IP      — this node's cluster /32 (volume
                                  + master bind, FUSE publicUrl)
      SEAWEED_FILER_VIP        — the .254 cluster VIP where the
                                  filer binds; identical on every node
      SEAWEED_MASTER_PEERS     — comma-joined master:9333 list of
                                  the Raft-3 member set
      SEAWEED_VOLUME_DISK_TYPE — operator-declared class for this
                                  node's volume server (`ssd`/`hdd`)
      SEAWEED_VOLUME_MAX       — max volumes per directory

    Idempotent: identical inputs produce identical file."""
    my_lo = _my_loopback()
    if not my_lo:
        raise RuntimeError(
            "seaweedfs: loopback_ip not in state.json — can't render env"
        )
    master_set = _master_set()
    master_peers = ",".join(f"{ip}:{MASTER_PORT}" for ip in master_set) or "none"
    filer_vip = _filer_vip()

    env = {
        "SEAWEED_LOOPBACK_IP":      my_lo,
        "SEAWEED_FILER_VIP":        filer_vip,
        "SEAWEED_MASTER_PEERS":     master_peers,
        # Alias of SEAWEED_MASTER_PEERS for units that reference this name.
        "SEAWEED_FILER_MASTERS":    master_peers,
        "SEAWEED_VOLUME_DISK_TYPE": disk_type,
        "SEAWEED_VOLUME_MAX":       str(int(volume_max)),
    }
    SEAWEED_ENV.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEAWEED_ENV.with_suffix(SEAWEED_ENV.suffix + ".tmp")
    tmp.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    import os as _os
    _os.replace(tmp, SEAWEED_ENV)
    log.info("seaweedfs: wrote %s (master_set=%s, filer_vip=%s)",
             SEAWEED_ENV, master_peers, filer_vip)


def write_s3_config() -> None:
    """Render /etc/bedrock/seaweedfs-s3.json.

    SeaweedFS 4.25 refuses to start when the config has identities
    without credentials (logged as "no admin/credentials supplied
    — set AWS_ACCESS_KEY_ID etc."), so the config carries:

    - One ``admin`` identity carrying generated credentials so the
      gateway has a valid auth identity at startup.
    - One ``anonymous`` identity with broad actions for testbed
      convenience (Kopia/awscli/rclone push without auth).

    Credentials are generated once per cluster (sourced from
    ``/etc/bedrock/cluster.key`` so every node deterministically
    derives the same admin creds — the same secret material
    underwriting witness AEAD also underwrites S3 admin). This file
    bootstraps the gateway; IAM identities live inside the filer DB
    via ``weed s3 -iam.filerBucketsPath=/buckets``.
    """
    S3_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    access_key, secret_key = _derive_admin_credentials()
    cfg = {
        "identities": [
            {
                "name": "admin",
                "credentials": [
                    {"accessKey": access_key, "secretKey": secret_key}
                ],
                "actions": ["Admin", "Read", "Write", "List", "Tagging"],
            },
            {
                "name": "anonymous",
                # Testbed convenience: kopia/awscli/rclone PUT-without-auth
                # for the e2e marker round trip. Operators override this
                # before any production deploy.
                "actions": ["Read", "Write", "List", "Tagging"],
            },
        ],
    }
    S3_CONFIG.write_text(json.dumps(cfg, indent=2))


def _derive_admin_credentials() -> tuple[str, str]:
    """Deterministically derive (access_key, secret_key) from the
    cluster_key so every node renders the same admin identity. The
    cluster_key is 32 random bytes from /etc/bedrock/cluster.key
    (created at install). Falls back to fixed testbed creds if the
    key isn't present yet (very early bootstrap)."""
    import hashlib
    key_path = Path("/etc/bedrock/cluster.key")
    if not key_path.exists():
        return ("bedrock-admin", "bedrock-admin-secret")
    raw = key_path.read_bytes()
    if len(raw) == 33 and raw[-1:] == b"\n":
        raw = raw[:32]
    # Two non-overlapping hashes — same input, different domain tags
    # — so leaking one doesn't leak the other.
    access_key = hashlib.sha256(b"bedrock-s3-access\0" + raw).hexdigest()[:20]
    secret_key = hashlib.sha256(b"bedrock-s3-secret\0" + raw).hexdigest()
    return (access_key, secret_key)


# ─────────────────────────────────────────────────────────────────────
# Lifecycle — start/stop helpers used by cluster_arbiter
# ─────────────────────────────────────────────────────────────────────


def _systemctl(action: str, unit: str) -> tuple[int, str, str]:
    r = subprocess.run(["systemctl", action, unit],
                       capture_output=True, text=True, timeout=30)
    return (r.returncode, r.stdout, r.stderr)


def _svc_active(unit: str) -> bool:
    r = subprocess.run(
        ["systemctl", "is-active", "--quiet", unit],
        capture_output=True,
    )
    return r.returncode == 0


def is_filer_active() -> bool:
    return _svc_active("bedrock-weed-filer.service")


def promote_to_filer_host() -> None:
    """Called by cluster_arbiter.promote_to_arbiter_host() after the
    cluster-singleton volume is mounted. Starts ONLY the filer (the
    cluster singleton on .254). The weed-s3 gateway is a PER-NODE service
    (runs on every node, bound 0.0.0.0:8333, started/restarted by
    promote_to_master_volume_host) — it is NOT tied to the arbiter role and
    must NOT be touched here. Idempotent."""
    log.info("seaweedfs: starting filer on this node")
    # Clear any stuck start-rate-limit from a previous failed start
    # attempt (env file race during install, mount-not-ready, etc.).
    # stderr silenced — `reset-failed` on a not-yet-loaded unit
    # complains, but the failure mode is harmless.
    subprocess.run(
        ["systemctl", "reset-failed", "bedrock-weed-filer.service"],
        check=False, timeout=10,
        stderr=subprocess.DEVNULL,
    )
    _systemctl("start", "bedrock-weed-filer.service")


def demote_filer_host() -> None:
    """Called by cluster_arbiter.demote_arbiter_host() before unmounting the
    cluster-singleton volume. Stops ONLY the filer (the singleton). The
    weed-s3 gateway is PER-NODE and stays running through a failover — stopping
    it here used to kill the demoting node's own S3 gateway. Idempotent."""
    log.info("seaweedfs: stopping filer on this node")
    _systemctl("stop", "bedrock-weed-filer.service")


def reconcile_master_config() -> None:
    """Re-render master.toml + seaweedfs.env from the current cluster
    snapshot. Called from the orchestrator's revision-watcher so a
    node-register event refreshes the SeaweedFS master peer set.
    Idempotent — atomic write, only changes file contents when
    cluster size or membership actually shifted."""
    try:
        write_env_file()
        write_master_config()
    except RuntimeError:
        # cluster.json not ready yet; will retry on next revision
        pass


def promote_to_master_volume_host() -> None:
    """Called by install / orchestrator on every node.

    Topology (see docs/storage-architecture.md):
    - **weed-volume**: runs on EVERY node, bound 0.0.0.0:8080.
    - **weed-s3**:     runs on EVERY node, bound 0.0.0.0:8333.
    - **weed-master**: runs only on the Raft-3 member set
                       (N=1→1, N=2→1, N≥3→3 lowest-octet nodes).
    - **weed-filer**:  singleton on .254 (owned by cluster_arbiter).

    The master set is computed locally via the deterministic
    lowest-octet rule (`_master_set`). This function is idempotent.

    If this node is no longer in the master set (e.g. a new
    lower-octet node joined and bumped us out), the master unit is
    stopped + disabled."""
    my_lo = _my_loopback()
    master_set = _master_set()
    i_run_master = my_lo in master_set

    # Re-render config from the current (strong) cluster view before
    # (re)starting the units. A render done during an earlier partial
    # boot view (mid-reboot, before quorum reformed) can emit a
    # self-only master peer list — that node's master then never peers
    # and its volume server never registers with the leader. Regenerate
    # seaweedfs.env + master.toml here so every boot converges on the
    # full Raft-3 peer set. daemon-reload picks up any unit env change.
    try:
        write_env_file()
        write_master_config()
        subprocess.run(["systemctl", "daemon-reload"],
                       check=False, timeout=10, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.warning("seaweedfs: pre-start config re-render failed: %s", e)

    # Reset-failed first: any of these may have crash-looped earlier
    # (env file not yet written → "Failed to load environment files"
    # → restart → ... → StartLimitBurst). Clear before we try to
    # start them for real. stderr silenced because units that haven't
    # been daemon-reloaded yet emit a "unit not loaded" message that
    # we don't care about — reset-failed is fire-and-forget.
    subprocess.run(
        ["systemctl", "reset-failed",
         "bedrock-weed-master.service",
         "bedrock-weed-volume.service",
         "bedrock-weed-filer.service",
         "bedrock-weed-s3.service"],
        check=False, timeout=10,
        stderr=subprocess.DEVNULL,
    )

    # Volume + S3 on EVERY node, always. stderr silenced because the
    # unit files have `WantedBy=` empty by design (see
    # configs/bedrock-weed-*.service) — they're enabled imperatively
    # by us, not at boot. `enable` on such a unit prints "no
    # installation config" but still creates the runtime symlink we
    # want via `--now`.
    # `restart` (not `enable --now`): these units have empty WantedBy
    # so `enable` makes no symlink, and `--now` won't restart an already-
    # running server — so a freshly re-rendered config (above) wouldn't
    # take effect. `restart` starts a stopped unit and re-applies config
    # on a running one.
    subprocess.run(
        ["systemctl", "restart",
         "bedrock-weed-volume.service",
         "bedrock-weed-s3.service"],
        check=False, timeout=30,
        stderr=subprocess.DEVNULL,
    )

    # Master: only if in the Raft-3 set. Same WantedBy= empty-by-design
    # reason for the stderr silencing as above.
    if i_run_master:
        subprocess.run(
            ["systemctl", "restart",
             "bedrock-weed-master.service"],
            check=False, timeout=30,
            stderr=subprocess.DEVNULL,
        )
    else:
        log.info("seaweedfs: not in master Raft-3 set "
                 "(loopback %s, set %s) — stopping master if active",
                 my_lo, master_set)
        subprocess.run(
            ["systemctl", "disable", "--now",
             "bedrock-weed-master.service"],
            check=False, timeout=30,
            stderr=subprocess.DEVNULL,
        )


# ─────────────────────────────────────────────────────────────────────
# Shared FUSE namespace — `weed mount` of the entire filer root at
# /mnt/bedrock on every node. libvirt sees /mnt/bedrock/iso/<name>.iso
# as a regular local path; SeaweedFS replicates the bytes via volume
# servers. ISOs are uploaded once into the filer (via S3 or via the
# FUSE mount on any node) and become visible cluster-wide.
# ─────────────────────────────────────────────────────────────────────

FUSE_MOUNTPOINT = Path("/mnt/bedrock")
# Unit name stays `.service` (not `.mount`): `weed mount` is a
# long-running FUSE-helper process, not a one-shot mount() syscall,
# so we run it as a Service unit. Naming it `.mount` would require
# the strict systemd [Mount]/What=/Where= shape which doesn't fit
# a fuse-helper that auto-reconnects.
ISO_MOUNT_UNIT  = "bedrock-fuse-mount.service"


def ensure_iso_library_mount() -> None:
    """Install a systemd `.mount` unit that FUSE-mounts the filer at
    `/mnt/bedrock` on every node. Identical config across the cluster.

    Per docs/storage-architecture.md the filer is a singleton on
    `.254/32` (the cluster VIP). All nodes point at that same VIP
    so the mount target string doesn't change when the arbiter-host
    flips — the VIP moves with the arbiter and the FUSE client
    auto-reconnects.
    """
    FUSE_MOUNTPOINT.mkdir(parents=True, exist_ok=True)

    filer_target = f"{_filer_vip()}:8888"

    unit = (
        "[Unit]\n"
        "Description=Bedrock shared FUSE namespace (SeaweedFS filer)\n"
        "After=network-online.target bedrock-d.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        # weed mount doesn't sd_notify; Type=simple lets systemd report
        # active as soon as the process exists. weed retries internally
        # if the filer isn't up yet, so the mount eventually completes
        # in the background — never block the caller.
        "Type=simple\n"
        f"ExecStartPre=/usr/bin/mkdir -p {FUSE_MOUNTPOINT}\n"
        f"ExecStart=/usr/local/bin/weed mount -filer={filer_target} "
        f"-dir={FUSE_MOUNTPOINT} "
        "-allowOthers -dirAutoCreate\n"
        f"ExecStopPost=/bin/sh -c 'fusermount -u {FUSE_MOUNTPOINT} 2>/dev/null || true'\n"
        "Restart=on-failure\n"
        "RestartSec=3s\n"
        "TimeoutStopSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    unit_path = Path(f"/etc/systemd/system/{ISO_MOUNT_UNIT}")
    existing = unit_path.read_text() if unit_path.exists() else ""
    needs_restart = (existing != unit)
    if needs_restart:
        unit_path.write_text(unit)
        subprocess.run(["systemctl", "daemon-reload"], check=False, timeout=10)
    # IDEMPOTENCY GUARD (RCA L54): this runs on EVERY converge pass (~1/s). Blindly
    # re-`enable`+`start`ing an already-enabled+running unit churns systemd every
    # cycle (sd-exec helpers, symlink rewrites) and was a measurable chunk of the
    # idle-node fork storm. Only act when the state actually needs changing:
    #   * enable only if not already enabled,
    #   * restart if the unit text changed, else start only if not already active.
    # In steady state this is two cheap is-enabled/is-active checks and no writes.
    enabled = subprocess.run(
        ["systemctl", "is-enabled", "--quiet", ISO_MOUNT_UNIT],
        check=False, timeout=10).returncode == 0
    if not enabled:
        # --no-block so we never deadlock the caller waiting for the FUSE mount.
        subprocess.run(["systemctl", "enable", "--no-block", ISO_MOUNT_UNIT],
                       check=False, timeout=10)
    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", ISO_MOUNT_UNIT],
        check=False, timeout=10).returncode == 0
    if needs_restart:
        subprocess.run(["systemctl", "restart", "--no-block", ISO_MOUNT_UNIT],
                       check=False, timeout=10)
    elif not active:
        subprocess.run(["systemctl", "start", "--no-block", ISO_MOUNT_UNIT],
                       check=False, timeout=10)
    if needs_restart or not enabled or not active:
        log.info("seaweedfs: FUSE namespace (re)mounted at %s via filer=%s",
                 FUSE_MOUNTPOINT, filer_target)


def init_collections() -> None:
    """One-shot per cluster: configure the three SeaweedFS
    collections via ``weed shell``. Idempotent — ``fs.configure
    -apply`` overwrites the previous config for the same
    locationPrefix, so re-running on a cluster that already has
    these is a no-op.

    Collections (per docs/storage-architecture.md):

    - ``scratch``  — replication ``000`` (1 copy, RAID0). Available
      at N≥1. Lost on the hosting node's failure.
    - ``standard`` — replication ``001`` (2 copies on different
      servers). Available at N≥2. Default for ISOs, templates,
      snapshots, ordinary backups.
    - ``critical`` — replication ``002`` (3 copies on different
      servers). Available at N≥3. Customer backups, things that
      must survive 2 simultaneous node losses.

    Called from cluster_init step ``seaweedfs_init_collections``.
    """
    if not WEED_BIN.exists():
        log.warning("init_collections: weed binary missing at %s",
                    WEED_BIN)
        return
    # `fs.configure` is the bucket/path config command in weed
    # shell. -apply makes it persistent. The default master URL
    # comes from /etc/bedrock/seaweedfs.env (SEAWEED_MASTER_PEERS);
    # we point at the first one explicitly so the shell connects
    # cleanly without needing the env file present.
    master_set = _master_set()
    if not master_set:
        log.warning("init_collections: no master in master_set yet — "
                    "deferring until calm orchestrator settles membership")
        return
    master_url = f"{master_set[0]}:{MASTER_PORT}"

    # Replication has to be satisfiable by the current cluster size,
    # or SeaweedFS hangs writes at volume-assign time (filer retries
    # "rpc error: code = Canceled" for 30s, then FUSE close returns
    # I/O error). At N=1 only 000 is satisfiable; N=2 adds 001; N=3+
    # adds 002. The cluster-default replication in MASTER_TOML
    # follows the same rule (see write_master_config). init_collections
    # matches it per-prefix so e.g. /iso/ doesn't get pinned to 001
    # on an N=1 box and brick every ISO upload.
    n_nodes = _n_cluster_nodes()
    standard_repl = "000" if n_nodes <= 1 else "001"
    critical_repl = (
        "000" if n_nodes <= 1
        else "001" if n_nodes <= 2
        else "002"
    )

    commands = [
        # locationPrefix MUST end with a slash for fs.configure to
        # match the directory tree.
        "fs.configure -locationPrefix=/scratch/   -collection=scratch  -replication=000 -apply",
        f"fs.configure -locationPrefix=/iso/       -collection=standard -replication={standard_repl} -apply",
        f"fs.configure -locationPrefix=/templates/ -collection=standard -replication={standard_repl} -apply",
        f"fs.configure -locationPrefix=/snapshots/ -collection=standard -replication={standard_repl} -apply",
        f"fs.configure -locationPrefix=/backups/   -collection=critical -replication={critical_repl} -apply",
    ]
    script = "\n".join(commands) + "\n"
    try:
        r = subprocess.run(
            [str(WEED_BIN), "shell", "-master", master_url],
            input=script, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            log.warning("init_collections: weed shell rc=%d stderr=%s",
                        r.returncode,
                        (r.stderr or "")[:200])
            return
        log.info("init_collections: configured 5 path policies "
                 "(scratch=000, iso/templates/snapshots=001, "
                 "backups=002) via master=%s", master_url)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("init_collections: %s", e)


def ensure_bucket(name: str) -> bool:
    """Idempotently create an S3 bucket on the local SeaweedFS cluster
    (under the filer's `/buckets/` path) via `weed shell s3.bucket.create`.
    A kopia-s3 backup repo lives inside a bucket, and the bucket must exist
    before kopia connects — this is the plumbing that creates it on the
    cluster's own SeaweedFS. Re-running for an existing bucket is a no-op.
    Returns True if the bucket exists on the cluster afterwards."""
    import re
    if not re.match(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$", name):
        raise ValueError(
            f"invalid S3 bucket name {name!r}: use 3-63 chars, lowercase "
            f"letters/digits/'.'/'-', start and end alphanumeric")
    if not WEED_BIN.exists():
        log.warning("ensure_bucket: weed binary missing at %s", WEED_BIN)
        return False
    master_set = _master_set()
    if not master_set:
        log.warning("ensure_bucket: no SeaweedFS master available yet")
        return False
    master_url = f"{master_set[0]}:{MASTER_PORT}"
    # s3.bucket.create is idempotent (an existing bucket is reported, not an
    # error); s3.bucket.list confirms the bucket is present afterwards.
    script = f"s3.bucket.create -name {name}\ns3.bucket.list\n"
    try:
        r = subprocess.run(
            [str(WEED_BIN), "shell", "-master", master_url],
            input=script, capture_output=True, text=True, timeout=30,
        )
        present = name in (r.stdout or "")
        if present:
            log.info("ensure_bucket: bucket %r present (master=%s)",
                     name, master_url)
        else:
            log.warning("ensure_bucket: bucket %r not confirmed (rc=%d): %s",
                        name, r.returncode,
                        (r.stdout or r.stderr or "")[:200])
        return present
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("ensure_bucket: %s", e)
        return False


def seed_iso_library(source_dir: Path = Path("/opt/bedrock/iso")) -> None:
    """Copy any ISOs staged in `source_dir` (e.g. virtio-win.iso baked
    into the install ISO) into the filer's `/iso/` subtree, visible
    as `/mnt/bedrock/iso/<name>.iso` on every node. Runs once on the
    arbiter-host after the filer is up. Idempotent — skips files
    that already exist in the namespace.
    """
    if not source_dir.exists() or not source_dir.is_dir():
        return
    target = FUSE_MOUNTPOINT / "iso"
    if not FUSE_MOUNTPOINT.is_mount():
        # Mount isn't up yet — ensure it.
        ensure_iso_library_mount()
    target.mkdir(parents=True, exist_ok=True)
    isos = list(source_dir.glob("*.iso"))
    if not isos:
        return
    for src in isos:
        dst = target / src.name
        if dst.exists():
            continue
        subprocess.run(["cp", "-n", str(src), str(dst)],
                       check=False, timeout=300)
        log.info("seaweedfs: seeded ISO %s -> %s/%s",
                 src.name, target, src.name)


# ─────────────────────────────────────────────────────────────────────
# CLI for operator debugging
# ─────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "config").lower()
    try:
        if cmd == "config":
            ensure_install()
            write_env_file()
            write_master_config()
            write_filer_config()
            write_s3_config()
            print("OK — configs written.")
        elif cmd == "promote":
            promote_to_filer_host()
        elif cmd == "demote":
            demote_filer_host()
        elif cmd == "reconcile":
            reconcile_master_config()
        else:
            print(f"unknown: {cmd!r} (config|promote|demote|reconcile)",
                  file=sys.stderr)
            sys.exit(2)
    except Exception as e:
        print(f"seaweedfs: {e}", file=sys.stderr)
        sys.exit(1)
