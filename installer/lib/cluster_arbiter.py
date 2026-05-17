"""Arbiter rqlite mobility — DRBD-promote + IP-add + service-start.

Per docs/post-alpha-rewrite-notes.md D-04..D-08:

  * The arbiter is a SECOND rqlite daemon co-resident with the
    elected mgmt master, providing the 3rd Raft voter that
    rqlite (which doesn't natively understand Bedrock's
    witness-weighted election) needs at N=2 physical.
  * The arbiter's data + WAL live on a shared DRBD volume named
    `tier-cluster`, mounted at `/var/lib/bedrock/cluster/`. The
    SeaweedFS filer's SQLite metadata also lives here (Phase E),
    so the same DRBD-promote + mount sequence moves all
    cluster-wide singletons together.
  * The arbiter's network identity is a SECONDARY `100.X.Y.254/32`
    added to `lo` on the hosting node (NOT a real interface).
    From rqlite's perspective the address is constant across
    master moves — the IP simply changes which `lo` it lives on.
    See D-05 and the post-alpha-rewrite-notes "Bedrock-specific
    insight" for why this dissolves the rqlite catch-22.

Public entry points:

    promote_to_arbiter_host(...)
        Called by the orchestrator on role=Leader. DRBD-promote,
        mount, claim .254/32, start arbiter. Idempotent — if any
        step is already done, that step no-ops.

    demote_arbiter_host(...)
        Called on role=Follower. Stop arbiter, release .254/32,
        unmount, DRBD-secondary. Reverse-idempotent.

    arbiter_status()
        Diagnostic — returns dict of {drbd_role, mounted, ip_present,
        service_active} so the dashboard / CLI can show "is this
        node currently hosting the arbiter?".
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("bedrock.cluster_arbiter")

# tier-cluster: the shared DRBD volume hosting all cluster-singleton
# services (rqlite-arbiter, SeaweedFS filer metadata, future
# singletons). Must align with the resource name set up by
# tier_storage.setup_cluster_tier() (Phase E does the install-side
# plumbing).
TIER_RESOURCE   = "tier-cluster"
MOUNT_POINT     = Path("/var/lib/bedrock/cluster")
ARBITER_DATA    = MOUNT_POINT / "rqlite"
ARBITER_SVC     = "bedrock-rqlited-arbiter.service"

# Reserved arbiter octet at the top of the cluster /24, per D-05.
# Derivation of the cluster_byte itself lives in cluster_addr; we just
# combine here.
ARBITER_OCTET   = 254

CLUSTER_JSON    = Path("/etc/bedrock/cluster.json")
STATE_JSON      = Path("/etc/bedrock/state.json")


def _run(cmd: list[str], check: bool = False, timeout: int = 30) -> tuple[int, str, str]:
    """Shell out + capture. Never raises; caller decides on rc."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True,
            check=False, timeout=timeout,
        )
        return (r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return (124, "", f"timeout after {timeout}s: {' '.join(cmd)}")
    except FileNotFoundError as e:
        return (127, "", str(e))


def arbiter_loopback_ip() -> str:
    """The arbiter's `100.X.Y.254/32` IP for this cluster. Reads
    cluster_uuid from cluster.json and derives the deterministic
    cluster prefix (same algorithm as for node loopback IPs)."""
    try:
        cluster = json.loads(CLUSTER_JSON.read_text())
    except Exception:
        return ""
    uuid = cluster.get("cluster_uuid", "")
    if not uuid:
        return ""
    try:
        from . import cluster_addr
    except ImportError:
        import sys
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import cluster_addr  # type: ignore
    prefix = cluster_addr.cluster_loopback_prefix(uuid)
    return f"{prefix}.{ARBITER_OCTET}"


# ─────────────────────────────────────────────────────────────────────
# DRBD steps
# ─────────────────────────────────────────────────────────────────────


def _drbd_role() -> str:
    """Returns 'Primary' / 'Secondary' / 'Unknown' for tier-cluster."""
    rc, out, _ = _run(["drbdadm", "role", TIER_RESOURCE])
    if rc != 0:
        return "Unknown"
    head = (out or "").strip().split("/")[0]
    return head or "Unknown"


def _drbd_promote() -> None:
    log.info("arbiter: drbdadm primary %s", TIER_RESOURCE)
    rc, _, err = _run(["drbdadm", "primary", TIER_RESOURCE], timeout=30)
    if rc != 0:
        # `--force` shouldn't be needed if the cluster is healthy;
        # the witness-aware election path is what authorises us to
        # take over. Leave forcing to operators / Phase D's
        # generation-guarded promote path.
        raise RuntimeError(f"drbdadm primary {TIER_RESOURCE} failed: {err.strip()}")


def _drbd_secondary() -> None:
    log.info("arbiter: drbdadm secondary %s", TIER_RESOURCE)
    _run(["drbdadm", "secondary", TIER_RESOURCE], timeout=30)


# ─────────────────────────────────────────────────────────────────────
# Mount steps
# ─────────────────────────────────────────────────────────────────────


def _is_mounted(path: Path) -> bool:
    rc, out, _ = _run(["findmnt", "-n", "-T", str(path)])
    return rc == 0 and out.strip() != ""


def _mount() -> None:
    MOUNT_POINT.mkdir(parents=True, exist_ok=True)
    if _is_mounted(MOUNT_POINT):
        return
    # The DRBD device naming convention follows tier_storage's
    # render_drbd_res — /dev/drbd<N> with N picked at create time.
    # We resolve it from the live config rather than hard-coding.
    rc, out, _ = _run(["drbdadm", "sh-dev", TIER_RESOURCE])
    if rc != 0 or not out.strip():
        raise RuntimeError(f"could not resolve DRBD device for {TIER_RESOURCE}")
    dev = out.strip().split()[0]
    log.info("arbiter: mount %s %s", dev, MOUNT_POINT)
    rc, _, err = _run(["mount", dev, str(MOUNT_POINT)], timeout=20)
    if rc != 0:
        raise RuntimeError(f"mount {dev} → {MOUNT_POINT}: {err.strip()}")


def _umount() -> None:
    if not _is_mounted(MOUNT_POINT):
        return
    log.info("arbiter: umount %s", MOUNT_POINT)
    _run(["umount", str(MOUNT_POINT)], timeout=20)


# ─────────────────────────────────────────────────────────────────────
# IP steps
# ─────────────────────────────────────────────────────────────────────


def _arbiter_ip_present() -> bool:
    ip = arbiter_loopback_ip()
    if not ip:
        return False
    rc, out, _ = _run(["ip", "-4", "addr", "show", "dev", "lo"])
    return rc == 0 and f"{ip}/32" in out


def _ip_add() -> str:
    ip = arbiter_loopback_ip()
    if not ip:
        raise RuntimeError("arbiter loopback IP unknown (cluster.json missing?)")
    if _arbiter_ip_present():
        return ip
    log.info("arbiter: ip addr add %s/32 dev lo", ip)
    rc, _, err = _run(["ip", "addr", "add", f"{ip}/32", "dev", "lo"])
    if rc != 0 and "File exists" not in err:
        raise RuntimeError(f"ip addr add {ip}/32: {err.strip()}")
    return ip


def _ip_del() -> None:
    ip = arbiter_loopback_ip()
    if not ip or not _arbiter_ip_present():
        return
    log.info("arbiter: ip addr del %s/32 dev lo", ip)
    _run(["ip", "addr", "del", f"{ip}/32", "dev", "lo"])


# ─────────────────────────────────────────────────────────────────────
# Service steps
# ─────────────────────────────────────────────────────────────────────


def _svc_active(unit: str) -> bool:
    rc, _, _ = _run(["systemctl", "is-active", "--quiet", unit])
    return rc == 0


def _svc_start(unit: str) -> None:
    if _svc_active(unit):
        return
    log.info("arbiter: systemctl start %s", unit)
    _run(["systemctl", "start", unit], timeout=30)


def _svc_stop(unit: str) -> None:
    if not _svc_active(unit):
        return
    log.info("arbiter: systemctl stop %s", unit)
    _run(["systemctl", "stop", unit], timeout=30)


# ─────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────


def promote_to_arbiter_host() -> dict:
    """Take over hosting the arbiter on this node. Sequence:

      1. DRBD-promote tier-cluster (idempotent)
      2. Mount /var/lib/bedrock/cluster (idempotent)
      3. Ensure arbiter data dir exists with mode 0700
      4. Claim 100.X.Y.254/32 on lo (idempotent)
      5. Render /etc/bedrock/rqlited-arbiter.env (idempotent)
      6. systemctl start bedrock-rqlited-arbiter (idempotent)

    Returns the post-state dict from arbiter_status(). Raises on
    a step that genuinely couldn't complete — DRBD-promote failure
    (peer still primary?), mount failure (FS corruption?), etc.

    Idempotent: safe to call on every role-change tick. If we're
    already hosting the arbiter, all steps no-op.
    """
    log.info("arbiter: promoting this node to arbiter host")
    _drbd_promote()
    _mount()
    ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    ip = _ip_add()
    # Now that the IP is on lo, materialise the env file. (The env
    # file's BEDROCK_ARBITER_BIND_IP refers to .254 — we need .254
    # bound before rqlited tries to bind, which is what the ExecStart
    # in the unit will do.)
    try:
        from . import rqlite_setup
        from . import seaweedfs
    except ImportError:
        import sys
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_setup  # type: ignore
        from lib import seaweedfs  # type: ignore
    rqlite_setup.render_arbiter_env_file()
    _svc_start(ARBITER_SVC)
    # SeaweedFS filer + S3 also follow the master role (D-07: every
    # cluster-wide singleton lives on the tier-cluster DRBD volume).
    # filer's SQLite metadata DB is at /var/lib/bedrock/cluster/
    # seaweedfs/filer.db — same mount.
    try:
        seaweedfs.promote_to_filer_host()
    except Exception as e:
        log.warning("arbiter: SeaweedFS filer promote failed: %s", e)
    log.info("arbiter: promotion complete (ip=%s mount=%s)",
             ip, MOUNT_POINT)
    return arbiter_status()


def demote_arbiter_host() -> dict:
    """Stop hosting the arbiter. Reverse of promote: stop S3 + filer
    (they rely on the mount), stop the arbiter rqlite, release the
    .254 IP, unmount tier-cluster, drbdadm secondary.

    Idempotent: safe to call on every role-change tick. If we're
    not currently the arbiter host, all steps no-op.
    """
    log.info("arbiter: demoting this node (was arbiter host)")
    # SeaweedFS S3 + filer first — they use the mount, must stop
    # before umount.
    try:
        try:
            from . import seaweedfs
        except ImportError:
            import sys
            sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import seaweedfs  # type: ignore
        seaweedfs.demote_filer_host()
    except Exception as e:
        log.warning("arbiter: SeaweedFS filer demote failed: %s", e)
    _svc_stop(ARBITER_SVC)
    _ip_del()
    _umount()
    _drbd_secondary()
    return arbiter_status()


def arbiter_status() -> dict:
    """Read-only snapshot. No side effects."""
    return {
        "drbd_role":       _drbd_role(),
        "mounted":         _is_mounted(MOUNT_POINT),
        "ip_present":      _arbiter_ip_present(),
        "service_active":  _svc_active(ARBITER_SVC),
        "loopback_ip":     arbiter_loopback_ip(),
    }


def i_should_host_arbiter() -> bool:
    """Decide from state.json: does this node currently hold the
    mgmt master role? Returns False if state.json is missing or
    role isn't mgmt+compute."""
    try:
        s = json.loads(STATE_JSON.read_text())
        return "mgmt" in (s.get("role") or "")
    except Exception:
        return False


def converge() -> dict:
    """Single-shot converge: if I should host arbiter and don't,
    promote; if I shouldn't and do, demote. Called from the
    orchestrator's revision-watcher and from boot_orchestrator
    after role settles.
    """
    should_host = i_should_host_arbiter()
    status = arbiter_status()
    am_host = status["service_active"] and status["ip_present"]
    if should_host and not am_host:
        return promote_to_arbiter_host()
    if not should_host and am_host:
        return demote_arbiter_host()
    return status


if __name__ == "__main__":
    # CLI: useful for manual operator testing.
    #   python3 cluster_arbiter.py status
    #   python3 cluster_arbiter.py promote
    #   python3 cluster_arbiter.py demote
    #   python3 cluster_arbiter.py converge
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    try:
        if cmd == "status":
            out = arbiter_status()
        elif cmd == "promote":
            out = promote_to_arbiter_host()
        elif cmd == "demote":
            out = demote_arbiter_host()
        elif cmd == "converge":
            out = converge()
        else:
            print(f"unknown command: {cmd!r} (status|promote|demote|converge)",
                  file=sys.stderr)
            sys.exit(2)
        print(json.dumps(out, indent=2))
    except Exception as e:
        print(f"arbiter: {e}", file=sys.stderr)
        sys.exit(1)
