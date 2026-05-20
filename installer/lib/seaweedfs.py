"""SeaweedFS lifecycle helpers — Bedrock's unified S3 stack.

Filer metadata in leveldb3 on the critical-tier DRBD volume at N≥2
(local LV at N=1); upgrade path to PostgreSQL via
`weed shell` → fs.meta.save / fs.meta.load is bidirectional and
documented in the SeaweedFS project wiki.

Components SeaweedFS-side:

  master    — cluster coordinator. Light. Runs on every node so
              we have HA at N>=2. Knows the volume topology.
  volume    — stores the actual file bytes (in "needle" files).
              One volume server per node.
  filer     — POSIX-style namespace on top of volumes. SQLite
              metadata DB. Single instance on the master (D-07).
              Moves with mgmt-master role via tier-cluster DRBD.
  s3        — S3 API gateway, depends on filer. Single instance
              co-resident with filer.

For v1.0 we run `weed server -master -volume -filer -s3` in
ALL-IN-ONE mode on the master node, with -volume.dir pointing at
local LV storage. On a 2-node HA cluster, the second node runs
the same all-in-one mode but is a master+volume peer; only the
master-elected node activates the filer+s3 sub-roles (handled by
cluster_arbiter.py-style mobility via tier-cluster DRBD).

This module provides:

  * ensure_install()          — drop /usr/local/bin/weed into place
  * write_master_config()     — render master.toml with cluster peers
  * write_filer_config()      — render filer.toml with the SQLite
                                metadata store pointing at
                                /var/lib/bedrock/cluster/seaweedfs/filer.db
  * write_systemd_units()     — bedrock-weed-master, -volume,
                                -filer, -s3 unit files
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


def _read_cluster() -> dict:
    try:
        return json.loads(CLUSTER_JSON.read_text())
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
    master.toml."""
    cluster = _read_cluster()
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
    /var/lib/bedrock/cluster/seaweedfs/ (lives on the tier-cluster
    DRBD volume per D-07/D-10).

    Note: SeaweedFS v4.x dropped the SQLite filer store in favour of
    leveldb3. Other store backends (postgres, mysql, tikv, etcd)
    remain available; switching is bidirectional via `weed shell` →
    `fs.meta.save` / `fs.meta.load`.
    """
    FILER_HOME.mkdir(parents=True, exist_ok=True)
    FILER_TOML.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""\
        # Bedrock-managed SeaweedFS filer config — DO NOT edit by hand.
        #
        # Per docs/post-alpha-rewrite-notes.md D-10: the metadata store
        # lives on the tier-cluster DRBD volume so it moves with the
        # mgmt-master role. SeaweedFS 4.x removed sqlite; we use the
        # embedded leveldb3 store which is feature-complete for our
        # POSIX-namespace + S3 use case.

        [leveldb3]
        enabled = true
        dir = "{FILER_HOME}"
        """)
    FILER_TOML.write_text(body)
    log.info("seaweedfs: wrote %s (filer db at %s)", FILER_TOML, FILER_HOME)


def write_env_file(*, volume_max: int = 50,
                   disk_type: str = "") -> None:
    """Render /etc/bedrock/seaweedfs.env consumed by all four
    weed systemd units. Variables:

      SEAWEED_LOOPBACK_IP    — this node's cluster /32, bind addr
      SEAWEED_MASTER_PEERS   — comma-joined master:9333 list (every
                                node, in stable sorted order)
      SEAWEED_VOLUME_DISK_TYPE — operator-declared class for this
                                node's volume server (`ssd`/`hdd`/etc.)
      SEAWEED_VOLUME_MAX     — max volumes per directory (sized to LV
                                capacity; default 50)

    Idempotent: identical inputs produce identical file."""
    my_lo = _my_loopback()
    if not my_lo:
        raise RuntimeError(
            "seaweedfs: loopback_ip not in state.json — can't render env"
        )
    # Master subset: same odd-only rule as write_master_config.
    all_lo = sorted([my_lo] + _peer_loopbacks(), key=_loopback_octet)
    n_nodes = len(all_lo)
    if n_nodes <= 1:
        master_subset = all_lo
    elif n_nodes == 2:
        master_subset = all_lo[:1]
    else:
        odd_n = n_nodes if n_nodes % 2 == 1 else n_nodes - 1
        master_subset = all_lo[:odd_n]
    if len(master_subset) <= 1:
        master_peers = "none"
    else:
        master_peers = ",".join(f"{ip}:{MASTER_PORT}" for ip in master_subset)
    # Filer/volume clients dial ALL elected masters (one of them is
    # the Raft leader, the others are followers — the client picks).
    filer_masters = ",".join(f"{ip}:{MASTER_PORT}" for ip in master_subset)

    env = {
        "SEAWEED_LOOPBACK_IP":      my_lo,
        "SEAWEED_MASTER_PEERS":     master_peers,
        "SEAWEED_FILER_MASTERS":    filer_masters,
        "SEAWEED_VOLUME_DISK_TYPE": disk_type,
        "SEAWEED_VOLUME_MAX":       str(int(volume_max)),
    }
    SEAWEED_ENV.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEAWEED_ENV.with_suffix(SEAWEED_ENV.suffix + ".tmp")
    tmp.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    import os as _os
    _os.replace(tmp, SEAWEED_ENV)
    log.info("seaweedfs: wrote %s (peers=%s)", SEAWEED_ENV, master_peers)


def write_s3_config() -> None:
    """Render the S3 API config. v1.0 testbed default: anonymous
    Read+Write+List. Operator can lock down per-bucket via
    `weed shell` once credentials are in place. Future:
    pull credentials from rqlite operators/secrets table."""
    S3_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "identities": [
            {
                "name": "anonymous",
                # Read + Write + List + Tagging covers the
                # marker-PUT/GET round trip the scale-lifecycle test
                # does, and the typical Kopia/awscli/rclone backup
                # flows. Operators must override this before any
                # production deploy.
                "actions": ["Read", "Write", "List", "Tagging",
                            "Admin"],
            }
        ]
    }
    S3_CONFIG.write_text(json.dumps(cfg, indent=2))


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
    tier-cluster volume is mounted. Starts filer + s3 gateway on
    this node. Idempotent."""
    log.info("seaweedfs: starting filer + s3 on this node")
    # Clear any stuck start-rate-limit from a previous failed start
    # attempt (env file race during install, mount-not-ready, etc.).
    subprocess.run(
        ["systemctl", "reset-failed",
         "bedrock-weed-filer.service",
         "bedrock-weed-s3.service"],
        check=False, timeout=10,
    )
    _systemctl("start", "bedrock-weed-filer.service")
    _systemctl("start", "bedrock-weed-s3.service")


def demote_filer_host() -> None:
    """Called by cluster_arbiter.demote_arbiter_host() before
    unmounting the tier-cluster volume. Stops filer + s3.
    Idempotent."""
    log.info("seaweedfs: stopping filer + s3 on this node")
    _systemctl("stop", "bedrock-weed-s3.service")
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
    """Called by the install / orchestrator path on every node.
    Volume server runs on EVERY node (peer-of-everyone HA pattern);
    master ONLY runs on the odd-numbered subset (SeaweedFS Raft
    refuses even peer counts), recomputed each call from the live
    cluster.json + state.json so additions/removals re-balance the
    quorum. Filer + s3 follow the mgmt master via cluster_arbiter.

    If this node was in the master subset before but isn't now, the
    master unit is stopped + disabled (cluster shrank from N=3 to N=2
    is the common case — the 3rd master must step down)."""
    my_lo = _my_loopback()
    all_lo = sorted([my_lo] + _peer_loopbacks(), key=_loopback_octet)
    n_nodes = len(all_lo)
    if n_nodes <= 1:
        master_subset = all_lo
    elif n_nodes == 2:
        master_subset = all_lo[:1]
    else:
        odd_n = n_nodes if n_nodes % 2 == 1 else n_nodes - 1
        master_subset = all_lo[:odd_n]
    i_run_master = my_lo in master_subset

    if i_run_master:
        # Reset-failed first: weed-master may have crash-looped earlier
        # (env file not yet written → "Failed to load environment files"
        # → restart → ... → StartLimitBurst). Clear that before we try
        # to start it for real.
        subprocess.run(
            ["systemctl", "reset-failed",
             "bedrock-weed-master.service",
             "bedrock-weed-volume.service",
             "bedrock-weed-filer.service",
             "bedrock-weed-s3.service"],
            check=False, timeout=10,
        )
        subprocess.run(
            ["systemctl", "enable", "--now",
             "bedrock-weed-master.service",
             "bedrock-weed-volume.service"],
            check=False, timeout=30,
        )
    else:
        # Stop the master if we were running it before. Volume always
        # runs (every node serves bytes regardless of master role).
        log.info("seaweedfs: not in master subset at N=%d "
                 "(loopback %s, subset %s) — stopping master if active",
                 n_nodes, my_lo, master_subset)
        subprocess.run(
            ["systemctl", "disable", "--now",
             "bedrock-weed-master.service"],
            check=False, timeout=30,
        )
        subprocess.run(
            ["systemctl", "enable", "--now",
             "bedrock-weed-volume.service"],
            check=False, timeout=30,
        )


# ─────────────────────────────────────────────────────────────────────
# ISO library — `weed mount` (FUSE) of the filer's /isos subtree on
# every node. libvirt sees /mnt/isos/<name>.iso as a regular local
# path; SeaweedFS replicates the bytes via volume servers. ISOs are
# uploaded once into the filer (via S3 or via /mnt/isos from any node)
# and become visible cluster-wide.
# ─────────────────────────────────────────────────────────────────────

ISO_MOUNTPOINT  = Path("/mnt/isos")
ISO_FILER_PATH  = "/isos"
ISO_MOUNT_UNIT  = "bedrock-isos-mount.service"


def ensure_iso_library_mount() -> None:
    """Install a systemd unit that FUSE-mounts the filer's /isos
    subtree at /mnt/isos, and start it. Idempotent.

    The filer endpoint is the **current mgmt-master's loopback /32**
    (read from cluster.json's `mgmt_master` field). At N=1 that's
    "self"; at N≥2 it follows the master via mesh routing — the same
    /32 stays reachable as the master role moves (the mesh updates
    routes within seconds of a transition).
    """
    import json as _json

    ISO_MOUNTPOINT.mkdir(parents=True, exist_ok=True)

    # Resolve the master's loopback from cluster.json. Fall back to
    # this node's own loopback if cluster.json isn't ready yet (very
    # early bootstrap); that's safe because at that point the master
    # IS self.
    filer_host = _my_loopback()
    try:
        cluster = _json.loads(CLUSTER_JSON.read_text())
        mm = cluster.get("mgmt_master", "")
        if mm:
            mm_lo = (cluster.get("nodes") or {}).get(mm, {}).get("loopback_ip", "")
            if mm_lo:
                filer_host = mm_lo
    except Exception:
        pass
    filer_target = f"{filer_host}:8888"

    unit = (
        "[Unit]\n"
        "Description=Bedrock ISO library (SeaweedFS FUSE mount)\n"
        "After=network-online.target bedrock-d.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        # weed mount doesn't sd_notify; Type=simple lets systemd report
        # active as soon as the process exists. weed retries internally
        # if the filer isn't up yet, so the mount eventually completes
        # in the background — never block the caller.
        "Type=simple\n"
        f"ExecStartPre=/usr/bin/mkdir -p {ISO_MOUNTPOINT}\n"
        f"ExecStart=/usr/local/bin/weed mount -filer={filer_target} "
        f"-dir={ISO_MOUNTPOINT} -filer.path={ISO_FILER_PATH} "
        "-allowOthers -dirAutoCreate\n"
        f"ExecStopPost=/bin/sh -c 'fusermount -u {ISO_MOUNTPOINT} 2>/dev/null || true'\n"
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
    # --no-block so we never deadlock the caller waiting for the FUSE
    # mount to come up; weed retries internally until the filer is
    # reachable.
    subprocess.run(
        ["systemctl", "enable", "--no-block", ISO_MOUNT_UNIT],
        check=False, timeout=10,
    )
    action = "restart" if needs_restart else "start"
    subprocess.run(
        ["systemctl", action, "--no-block", ISO_MOUNT_UNIT],
        check=False, timeout=10,
    )
    log.info("seaweedfs: ISO library mounted at %s via filer=%s",
             ISO_MOUNTPOINT, filer_target)


def seed_iso_library(source_dir: Path = Path("/opt/bedrock/iso")) -> None:
    """Copy any ISOs staged in `source_dir` (e.g. virtio-win.iso baked
    into the install ISO) into the filer's /isos subtree. Runs once
    on the mgmt-master after the filer is up. Idempotent — `cp -n`
    skips files that already exist in the namespace.
    """
    if not source_dir.exists() or not source_dir.is_dir():
        return
    target = ISO_MOUNTPOINT
    if not target.is_mount():
        # Mount isn't up yet — ensure it.
        ensure_iso_library_mount()
    isos = list(source_dir.glob("*.iso"))
    if not isos:
        return
    for src in isos:
        dst = target / src.name
        if dst.exists():
            continue
        subprocess.run(["cp", "-n", str(src), str(dst)],
                       check=False, timeout=300)
        log.info("seaweedfs: seeded ISO %s -> /isos/%s",
                 src.name, src.name)


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
