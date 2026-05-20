"""Arbiter rqlite mobility — DRBD-promote + IP-add + service-start.

Per docs/post-alpha-rewrite-notes.md D-04..D-08:

  * The arbiter is a SECOND rqlite daemon co-resident with the
    elected mgmt master, providing the 3rd Raft voter that
    rqlite (which doesn't natively understand Bedrock's
    witness-weighted election) needs at N=2 physical.
  * The arbiter's data + WAL live on a shared DRBD volume named
    `tier-critical`, mounted at `/var/lib/bedrock/cluster/`. The
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

# tier-critical: the shared DRBD volume hosting all cluster-singleton
# services (rqlite-arbiter, SeaweedFS filer metadata, future
# singletons). Must align with the resource name set up by
# tier_storage.setup_cluster_tier() (Phase E does the install-side
# plumbing).
TIER_RESOURCE   = "tier-critical"
MOUNT_POINT     = Path("/var/lib/bedrock/cluster")
ARBITER_DATA    = MOUNT_POINT / "rqlite"
ARBITER_SVC     = "bedrock-rqlited-arbiter.service"

# Reserved arbiter octet at the top of the cluster /24, per D-05.
# Derivation of the cluster_byte itself lives in cluster_addr; we just
# combine here.
ARBITER_OCTET   = 254

CLUSTER_JSON    = Path("/etc/bedrock/cluster.json")
STATE_JSON      = Path("/etc/bedrock/state.json")

# Under the unified bedrock-d daemon: bedrock-d.main() wires its
# BedrockState here so cluster_arbiter can read netd's live election
# outcome directly instead of state.json["role"] (which lags behind
# netd by an rqlite round-trip, and at N=2 may never settle because
# rqlite can't form quorum until the arbiter we're deciding about is
# running). This makes netd the single source of truth for "am I
# the cluster's mgmt master right now".
SHARED_STATE = None


def attach_state(state) -> None:
    """Called once from bedrock-d main() with the shared BedrockState
    so i_should_host_arbiter() can consult netd's election outcome."""
    global SHARED_STATE
    SHARED_STATE = state


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
    """Returns 'Primary' / 'Secondary' / 'Unknown' for tier-critical.
    'Unknown' covers both "drbdadm errored" and "resource not
    configured" (N=1 case, where no DRBD resource exists at all)."""
    rc, out, _ = _run(["drbdadm", "role", TIER_RESOURCE])
    if rc != 0:
        return "Unknown"
    head = (out or "").strip().split("/")[0]
    return head or "Unknown"


def _drbd_resource_exists() -> bool:
    """True if tier-critical is a configured DRBD resource on this
    node. False at N=1 (no DRBD needed — singleton services run
    directly on the local FS) or before the tier is set up by the
    install path."""
    rc, _, _ = _run(["drbdadm", "dump", TIER_RESOURCE])
    return rc == 0


def _cluster_size() -> int:
    """How many nodes are in the cluster snapshot. N=1 means we
    can skip every DRBD step (no peer, no replication needed) and
    just run the singleton services on the local FS."""
    try:
        cluster = json.loads(CLUSTER_JSON.read_text())
    except Exception:
        return 0
    return len(cluster.get("nodes") or {})


def _drbd_promote() -> None:
    log.info("arbiter: drbdadm primary %s", TIER_RESOURCE)
    rc, _, err = _run(["drbdadm", "primary", TIER_RESOURCE], timeout=30)
    if rc == 0:
        return
    # Failover case: the previous primary is unreachable (we got here
    # via cluster election → set_mgmt_master → converge), so DRBD's
    # "Need access to UpToDate data" check refuses without --force.
    # The election (lib/election.py) + witness DRBD-UUID blessing
    # (lib/witness.py) are the single source of authority for who
    # owns the data — if they say we're master, we are. --force here
    # is correct.
    if "uptodate" in err.lower() or "need access" in err.lower():
        log.warning("arbiter: drbdadm primary refused (peer unreachable); "
                    "retrying with --force per election authority")
        rc, _, err = _run(
            ["drbdadm", "--", "--force", "primary", TIER_RESOURCE],
            timeout=30,
        )
        if rc == 0:
            return
    raise RuntimeError(f"drbdadm primary {TIER_RESOURCE} failed: {err.strip()}")


def _drbd_secondary() -> None:
    log.info("arbiter: drbdadm secondary %s", TIER_RESOURCE)
    _run(["drbdadm", "secondary", TIER_RESOURCE], timeout=30)


# ─────────────────────────────────────────────────────────────────────
# Mount steps
# ─────────────────────────────────────────────────────────────────────


def _is_mounted(path: Path) -> bool:
    """True iff `path` itself is a mount point (not just on a mounted FS).

    findmnt -T <path> returns the containing filesystem, which for a
    not-yet-mounted /var/lib/bedrock/cluster returns the root mount
    ("/" on xfs) — so _is_mounted would return True even though
    the DRBD device isn't mounted there. That made promote_to_arbiter_host
    skip the mount step and the filer happily wrote leveldb3 to the
    root FS, which is invisible to peers and disappears at failover.

    Use `mountpoint -q` (or equivalently findmnt without -T) which
    returns 0 ONLY if the path is a true mount point."""
    rc, _, _ = _run(["mountpoint", "-q", str(path)])
    return rc == 0


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
    """Take over hosting the cluster-singleton services on this node.

    Two modes:

      * N=1 (no tier-cluster DRBD resource): the singleton directory
        is just a regular path on the local FS. No DRBD-promote, no
        mount, no .254 floating IP, no arbiter rqlite (the single
        per-node rqlite is already the only voter and self-elected
        leader). filer + s3 still start so the S3 endpoint is up.

      * N>=2 (tier-cluster DRBD exists): full sequence —
          1. DRBD-promote tier-cluster
          2. Mount /var/lib/bedrock/cluster
          3. Ensure arbiter data dir exists (mode 0700)
          4. Claim 100.X.Y.254/32 on lo
          5. Render /etc/bedrock/rqlited-arbiter.env
          6. systemctl start bedrock-rqlited-arbiter
        Then filer + s3 start in either mode.

    Returns the post-state dict. Raises on a step that genuinely
    couldn't complete. Idempotent: safe to call on every role tick.
    """
    n = _cluster_size()
    drbd_present = _drbd_resource_exists()
    ip = ""

    if drbd_present:
        log.info("arbiter: promoting (N=%d, tier-cluster DRBD present)", n)
        _drbd_promote()
        _mount()
        ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        ip = _ip_add()
    else:
        # N=1 mode (or pre-DRBD-setup): create the singleton dir
        # directly on the local FS so filer's DB has a home.
        log.info("arbiter: promoting (N=%d, no tier-cluster DRBD — "
                 "running singletons directly on local FS)", n)
        MOUNT_POINT.mkdir(parents=True, exist_ok=True, mode=0o755)
        ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        # No .254/32 needed — there's no rqlite-arbiter at N=1.
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
    if drbd_present:
        rqlite_setup.render_arbiter_env_file()
        _svc_start(ARBITER_SVC)
    # else: N=1 → no arbiter rqlite; the single per-node rqlite is
    # the sole Raft voter and is already running.
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

    # Witness claim — announce our DRBD-UUID so the witness either
    # accepts us as the new blessed master OR (if a peer beat us to
    # the claim) reflects that fact back on its next reply, which
    # election.compute then converts to a NoQuorum + self-demote on
    # the next tick. We don't roll back inside this function:
    #   - the witness may take >2 s to age out a stale bless
    #     (CLAIM_HOLDDOWN_MS = 15 s), and rolling back instantly
    #     prevents takeover during that window (observed v32 8b:
    #     sim-1 promoted, rolled back on sim-2's stale bless, sim-2
    #     never released .254 because witness kept refusing sim-1).
    #   - election.compute's bless-mismatch path already drives the
    #     demote one tick later if the witness sticks with the peer.
    if SHARED_STATE is not None and SHARED_STATE.netd_ws is not None:
        try:
            try:
                from . import witness as _witness
            except ImportError:
                from lib import witness as _witness  # type: ignore
            ws = SHARED_STATE.netd_ws
            if ws is not None and ws.discovered:
                try:
                    out = subprocess.check_output(
                        ["drbdadm", "current-uuid", TIER_RESOURCE],
                        timeout=3,
                    )
                    uuid_hex = out.decode().strip()
                except Exception as _ue:
                    log.warning("arbiter: drbdadm current-uuid failed: %s",
                                _ue)
                    uuid_hex = ""
                if uuid_hex:
                    _witness.send_claim(ws, uuid_hex)
                    log.info("arbiter: witness claim sent (drbd-uuid=%s)",
                             uuid_hex[:8])
        except Exception as e:
            log.warning("arbiter: witness claim path errored: %s", e)

    return arbiter_status()


def demote_arbiter_host() -> dict:
    """Stop hosting the singleton services. Reverse of promote.

    Two modes mirroring promote_to_arbiter_host():

      * If tier-cluster DRBD is present: stop filer + s3, stop
        arbiter rqlite, release .254 IP, unmount, drbdadm secondary.

      * N=1 (no DRBD): stop filer + s3, nothing else to do — the
        rqlite is still running for cluster state (it's the only
        voter at N=1, and demote on a master that's still the only
        node doesn't actually demote anything, just stops singletons
        that would re-start on the next converge tick).

    Idempotent.
    """
    log.info("arbiter: demoting this node (was singleton host)")
    drbd_present = _drbd_resource_exists()
    # SeaweedFS S3 + filer first — they use the mount, must stop
    # before umount. Also valid at N=1; they just stop.
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
    if drbd_present:
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
    """Decide whether this node should currently host the cluster
    singletons (arbiter rqlite + .254 + DRBD primary + filer + s3).

    Authority (in order):
      1. netd's live election outcome via SHARED_STATE — this is the
         real-time decision based on peer_liveness + witness vote.
         Used by the unified bedrock-d daemon.
            "leader"   → True  (host)
            "fenced"   → False (we're isolated; do not promote)
            "noquorum" → False (we lost quorum; demote in progress)
            "follower" → False (peer is master)
      2. Fallback to state.json["role"] for standalone / pre-
         unification installs. Returns False if missing.

    Why netd wins: at N=2 (no rqlite arbiter yet), rqlite can't form
    quorum until the arbiter we're deciding about is started. Reading
    state.json["role"] (projected from rqlite) deadlocks. netd's
    election + witness vote is the only path that doesn't need
    rqlite quorum to decide.
    """
    if SHARED_STATE is not None:
        outcome = (SHARED_STATE.last_election_outcome or "").lower()
        if outcome == "leader":
            return True
        if outcome in ("fenced", "noquorum", "follower"):
            return False
        # outcome == "" / "init" → fall through to legacy path while
        # the daemon is still warming up.
    try:
        s = json.loads(STATE_JSON.read_text())
        return "mgmt" in (s.get("role") or "")
    except Exception:
        return False


def converge() -> dict:
    """Single-shot converge: if I should host singletons and don't,
    promote; if I shouldn't and do, demote. Called from the
    orchestrator's revision-watcher and from boot_orchestrator
    after role settles.

    "Hosting" at N>=2 means: arbiter rqlite running on .254 + DRBD
    primary + mount + filer + s3. At N=1 it means: filer + s3 only.
    Detection looks at the arbiter rqlite service for N>=2 and the
    filer service for N=1.
    """
    should_host = i_should_host_arbiter()
    status = arbiter_status()
    drbd_present = _drbd_resource_exists()
    if drbd_present:
        am_host = status["service_active"] and status["ip_present"]
    else:
        # N=1: am_host = filer is running
        try:
            try:
                from . import seaweedfs
            except ImportError:
                import sys
                sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import seaweedfs  # type: ignore
            am_host = seaweedfs.is_filer_active()
        except Exception:
            am_host = False
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
