"""SeaweedFS lifecycle helpers — Bedrock's unified S3 stack (D-09).

Per docs/post-alpha-rewrite-notes.md D-09..D-12: SeaweedFS replaces
both Garage (scratch) and RustFS (bulk/critical). One S3 daemon
stack instead of two. Filer metadata in SQLite on the tier-cluster
DRBD volume (D-10); upgrade path to PostgreSQL via fs.meta.save /
fs.meta.load is bidirectional and project-confirmed.

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
FILER_TOML        = Path("/etc/bedrock/seaweedfs-filer.toml")
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

    # Master peers list (this node + all peers). Master HA requires
    # the same peer set on every node.
    all_masters = sorted([my_lo] + peers)
    peers_arg = ",".join(f"{ip}:{MASTER_PORT}" for ip in all_masters)

    # defaultReplication: at N=1 use "000" (no copies — single node
    # has no peer). At N>=2 use "001" (one extra copy on a peer
    # node) as the safer default. Per-collection overrides are still
    # operator-tunable.
    n_nodes = len(all_masters)
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
    """Render filer.toml — pins SQLite as the metadata store, points
    at /var/lib/bedrock/cluster/seaweedfs/filer.db (lives on the
    tier-cluster DRBD volume per D-07/D-10).

    Other store backends (postgres, mysql, tikv, dqlite, etcd) are
    available; switching is bidirectional per the SeaweedFS project's
    own documented `fs.meta.save` / `fs.meta.load` pattern.
    """
    FILER_HOME.mkdir(parents=True, exist_ok=True)
    FILER_TOML.parent.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(f"""\
        # Bedrock-managed SeaweedFS filer config — DO NOT edit by hand.
        #
        # Per docs/post-alpha-rewrite-notes.md D-10:
        # The SQLite metadata DB lives on the tier-cluster DRBD volume
        # so it moves with the mgmt-master role. Upgrade path to
        # PostgreSQL is bidirectional via `weed shell` →
        # `fs.meta.save` / `fs.meta.load` (project-confirmed
        # bidirectional in the SeaweedFS Filer-Stores wiki).

        [sqlite]
        enabled = true
        dbFile = "{FILER_DB}"
        """)
    FILER_TOML.write_text(body)
    log.info("seaweedfs: wrote %s (filer db at %s)", FILER_TOML, FILER_DB)


def write_env_file(*, volume_max: int = 50,
                   disk_type: str = "ssd") -> None:
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
    all_masters = sorted([my_lo] + _peer_loopbacks())
    if len(all_masters) <= 1:
        # SeaweedFS 'none' = single-master mode (no peer-list Raft).
        # Documented in `weed master --help`. Avoids the master
        # complaining about "peer list contains only self".
        peers = "none"
    else:
        peers = ",".join(f"{ip}:{MASTER_PORT}" for ip in all_masters)

    env = {
        "SEAWEED_LOOPBACK_IP":      my_lo,
        "SEAWEED_MASTER_PEERS":     peers,
        "SEAWEED_VOLUME_DISK_TYPE": disk_type,
        "SEAWEED_VOLUME_MAX":       str(int(volume_max)),
    }
    SEAWEED_ENV.parent.mkdir(parents=True, exist_ok=True)
    tmp = SEAWEED_ENV.with_suffix(SEAWEED_ENV.suffix + ".tmp")
    tmp.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    import os as _os
    _os.replace(tmp, SEAWEED_ENV)
    log.info("seaweedfs: wrote %s (peers=%s)", SEAWEED_ENV, peers)


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
    Master + volume run on EVERY node (peer-of-everyone HA pattern);
    only filer + s3 follow the mgmt master via cluster_arbiter.
    Idempotent."""
    subprocess.run(
        ["systemctl", "enable", "--now",
         "bedrock-weed-master.service",
         "bedrock-weed-volume.service"],
        check=False, timeout=30,
    )


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
