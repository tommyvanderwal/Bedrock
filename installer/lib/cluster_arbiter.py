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
    cluster_uuid from local rqlite (level='none', works without
    quorum) and derives the deterministic cluster prefix (same
    algorithm as for node loopback IPs)."""
    try:
        try:
            from . import rqlite_client
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import rqlite_client  # type: ignore
        with rqlite_client.RqliteClient() as _rc:
            row = _rc.query_one(
                "SELECT cluster_uuid FROM cluster_info WHERE id = 1",
                level="none",
            )
        uuid = (row or {}).get("cluster_uuid", "") or ""
    except Exception:
        return ""
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
    """How many nodes are in the cluster (per local rqlite, level='none').
    N=1 means we can skip every DRBD step (no peer, no replication needed)
    and just run the singleton services on the local FS."""
    try:
        try:
            from . import rqlite_client
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import rqlite_client  # type: ignore
        with rqlite_client.RqliteClient() as _rc:
            row = _rc.query_one(
                "SELECT COUNT(*) AS c FROM nodes", level="none",
            )
        return int((row or {}).get("c") or 0)
    except Exception:
        return 0


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

    Runs the witness slot takeover protocol BEFORE any DRBD or service
    state change. See docs/cluster-quorum-spec.md for the load-bearing
    step-by-step. Witness is consulted ONLY here (and in
    demote_arbiter_host); rqlite is NOT touched — it's the service
    being recovered.

    N=1 mode (no tier-cluster DRBD resource): bind .254 + start
    singletons on local FS. .254 is bound at every N including N=1
    per docs/storage-architecture.md so client config (filer URL,
    mgmt URL) is identical from day 1.

    Idempotent: safe to call on every role tick. Witness checks are
    no-ops when we're already the master in cluster.json's view.
    """
    n = _cluster_size()
    drbd_present = _drbd_resource_exists()
    ip = ""

    if not drbd_present:
        # N=1: no DRBD, no witness consult. Simple path.
        log.info("arbiter: promoting (N=%d, no tier-cluster DRBD — "
                 "running singletons on local FS, .254 on lo)", n)
        MOUNT_POINT.mkdir(parents=True, exist_ok=True, mode=0o755)
        ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Bind .254 to lo so filer + mgmt URLs work uniformly from N=1.
        ip = _ip_add()
        try:
            try:
                from . import seaweedfs
            except ImportError:
                import sys as _sys
                _sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import seaweedfs  # type: ignore
            seaweedfs.promote_to_filer_host()
        except Exception as e:
            log.warning("arbiter: SeaweedFS filer promote failed: %s", e)
        log.info("arbiter: promotion complete (N=1; ip=%s mount=%s)",
                 ip, MOUNT_POINT)
        return arbiter_status()

    # N>=2 with tier-cluster DRBD. Run the takeover protocol.
    log.info("arbiter: promoting (N=%d, tier-cluster DRBD present)", n)

    # Idempotent fast-path: if I am already the hosting node per
    # cluster.json, skip the witness protocol. This handles every
    # tick after the initial promotion (converge_retry calls promote
    # repeatedly).
    am_already_host = (
        _drbd_role() == "Primary"
        and _is_mounted(MOUNT_POINT)
        and _arbiter_ip_present()
    )

    if not am_already_host:
        # Steps 1-5: takeover protocol. Refuse to promote if it fails.
        if not _run_takeover_protocol():
            log.error("arbiter: takeover protocol REFUSED — not promoting")
            return arbiter_status()

    # Steps 6: hardware/software state changes.
    _drbd_promote()
    _mount()
    ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    ip = _ip_add()
    try:
        from . import rqlite_setup
        from . import seaweedfs
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_setup  # type: ignore
        from lib import seaweedfs  # type: ignore
    rqlite_setup.render_arbiter_env_file()
    _svc_start(ARBITER_SVC)
    try:
        seaweedfs.promote_to_filer_host()
    except Exception as e:
        log.warning("arbiter: SeaweedFS filer promote failed: %s", e)
    log.info("arbiter: promotion complete (ip=%s mount=%s)",
             ip, MOUNT_POINT)
    return arbiter_status()


def _run_takeover_protocol() -> bool:
    """Steps 1-5 of the arbiter takeover protocol. Returns True if it
    is safe to proceed with drbdadm primary + service starts.

    NO rqlite calls on this path — the cluster's rqlite is the very
    service we're about to recover, so it cannot be a precondition.

    Fast-path: if cluster.json says I'm already the mgmt_master (or
    no master is recorded yet), there's nothing to take over from.
    The protocol's job is to gate failover from another node; for the
    first promotion at storage-promote or the periodic self-renew
    after I've already been confirmed master, we skip witness checks
    entirely. netd publishes our slot every tick regardless.
    """
    if SHARED_STATE is None or SHARED_STATE.netd_ws is None:
        # Pre-unification or netd not running. Fall back to "always
        # allow" — the legacy boot path relies on this.
        log.warning("arbiter: takeover protocol skipped (no shared state)")
        return True
    try:
        try:
            from . import witness as _witness
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import witness as _witness  # type: ignore
    except Exception as e:
        log.error("arbiter: witness import failed: %s", e)
        return False

    ws = SHARED_STATE.netd_ws
    my_id = ws.my_node_id

    # FAST PATH: no prior master OR self is the recorded master.
    # Nothing to take over from; proceed with the promotion. The
    # netd tick is already publishing our slot every second, so when
    # a witness IS present it will see us. No witness reachability
    # gate needed at this path (covers first-ever promote where the
    # operator hasn't deployed an Echo yet).
    last_master_id = _last_known_master_node_id()
    if last_master_id is None or last_master_id == my_id:
        log.info("arbiter: takeover protocol — no prior master to take "
                 "over from (last_master=%r, self=%d); proceeding",
                 last_master_id, my_id)
        return True

    # Witness reachability decides whether we can run the full
    # protocol. At N>=3 the cluster has natural rqlite quorum even
    # without the witness, and the isolated old master self-demotes
    # via NoQuorum logic — so we can proceed cautiously. At N<=2 the
    # witness is mandatory because rqlite quorum depends on the
    # arbiter we're about to promote.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _witness.is_alive(ws):
            break
        time.sleep(0.2)
    if not _witness.is_alive(ws):
        n = _cluster_size()
        if n >= 3:
            log.warning("arbiter: takeover at N=%d without witness — "
                        "proceeding (rqlite quorum + isolated-master "
                        "self-demote keep this safe; deploy a witness "
                        "for full protocol)", n)
            return True
        log.error("arbiter: takeover REFUSED — taking over from "
                  "node_id=%d at N=%d but no witness reply in 5 s",
                  last_master_id, n)
        return False

    # Step 1+2: inspect M's slot. Per cluster-quorum-spec.md INV-7,
    # tag.lms=1 NEVER times out. A stale slot with lms=1 means the
    # previous master died without clearing its LMS — only the
    # operator can clear it (see docs/operator-overrides.md).
    slot_m = _witness.read_slot(ws, last_master_id)
    if slot_m is None:
        # INV-7 "missing slot = worst case assumed". A missing slot
        # for a still-known cluster member might mean the witness
        # rebooted and lost its map; we cannot rule out that the
        # missing slot previously held tag.lms=1. Operator must
        # decommission the node from the rqlite `nodes` table
        # (which makes us ignore it entirely) or re-key the
        # witness identity.
        log.error("arbiter: takeover REFUSED — last master "
                  "node_id=%d has no slot at witness. Per INV-7 a "
                  "missing slot is treated as worst-case (could have "
                  "held lms=1). Operator must decommission this "
                  "node from the rqlite `nodes` table, or re-key "
                  "the witness identity (see docs/operator-overrides.md).",
                  last_master_id)
        return False
    if not slot_m.is_stale():
        log.info("arbiter: takeover REFUSED — slot[%d] is fresh "
                 "(tag.lms=%s); cluster healthy elsewhere",
                 last_master_id, slot_m.lms)
        return False
    if slot_m.lms:
        log.error("arbiter: takeover REFUSED — slot[%d] is stale "
                  "but tag.lms=1. Previous master died without "
                  "clearing LMS; LMS does not time out. Operator "
                  "must clear via override before takeover can "
                  "proceed (see docs/operator-overrides.md).",
                  last_master_id)
        return False
    log.info("arbiter: slot[%d] stale and tag.lms=0; continuing",
             last_master_id)
    # Step 3: local DRBD UUID must EQUAL slot.marker exactly.
    local_uuid_step3 = _read_local_drbd_uuid()
    slot_marker = slot_m.marker.decode("ascii", errors="replace").strip()
    if not local_uuid_step3:
        log.error("arbiter: takeover REFUSED — drbdadm current-uuid "
                  "tier-critical failed")
        return False
    if local_uuid_step3 != slot_marker:
        log.error("arbiter: takeover REFUSED — DRBD divergence: "
                  "local current-uuid=%s vs slot[%d].marker=%s. "
                  "Operator must reconcile (drbdadm invalidate or "
                  "wait for peer).",
                  local_uuid_step3[:12], last_master_id, slot_marker[:12])
        return False
    log.info("arbiter: DRBD UUID match (%s); proceeding to claim",
             local_uuid_step3[:12])

    # Step 4: write own slot tag=lms. netd's election tick runs at 1
    # Hz and will pick up the new tag on the very next iteration; we
    # set it via shared state and wait for netd to send it.
    local_uuid = _read_local_drbd_uuid()
    marker_bytes = local_uuid.encode("ascii") if local_uuid else b""
    _witness.set_own_slot(ws, marker=marker_bytes, tag=_witness.TAG_LMS)

    # Step 5: read it back. Wait up to 3 attempts × ~1.5 s = ~4.5 s.
    expected_marker = marker_bytes
    for attempt in range(1, 4):
        time.sleep(1.5)  # let netd send + receive at least one round-trip
        own = _witness.own_slot(ws)
        if own is not None and own.lms and own.marker == expected_marker:
            log.info("arbiter: own slot readback OK (attempt %d, tag.lms=1, "
                     "marker=%s)", attempt, local_uuid[:12])
            return True
        log.warning("arbiter: own-slot readback attempt %d not yet "
                    "reflecting lms+marker (have=%r)", attempt,
                    own and (own.lms, own.marker[:12]))
    log.error("arbiter: takeover REFUSED — own-slot readback failed "
              "after 3 attempts; witness unreachable or losing writes")
    return False


def _last_known_master_node_id() -> "int | None":
    """Read the current mgmt_master + its loopback from local rqlite
    (level='none', works without quorum). Return the node_id (last
    octet of loopback_ip), or None if no master is set."""
    try:
        try:
            from . import rqlite_client
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import rqlite_client  # type: ignore
        with rqlite_client.RqliteClient() as _rc:
            row = _rc.query_one(
                "SELECT n.loopback_ip FROM cluster_info ci "
                "LEFT JOIN nodes n ON n.node_name = ci.mgmt_master "
                "WHERE ci.id = 1",
                level="none",
            )
    except Exception:
        return None
    loop = (row or {}).get("loopback_ip") or ""
    if not loop:
        return None
    try:
        return int(loop.rsplit(".", 1)[1])
    except (IndexError, ValueError):
        return None


def _read_local_drbd_uuid() -> str:
    """Read tier-critical's current-UUID. Returns "" if DRBD isn't
    configured (N=1) or no source has it.

    Primary source: DRBD9's debugfs at
    ``/sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id``.
    First line is the current UUID. Works while the resource is UP
    (the takeover-protocol case).

    Fallback: ``drbdadm dump-md`` — only works when the resource is
    detached (N=1 scratch path).

    ``drbdadm current-uuid`` does NOT exist in DRBD 9.34 — removed
    upstream after the 8.x → 9.x split. Don't reach for it."""
    debugfs = (
        f"/sys/kernel/debug/drbd/resources/{TIER_RESOURCE}/volumes/0/"
        "data_gen_id"
    )
    try:
        with open(debugfs, "r") as f:
            first = f.readline().strip()
        if first.startswith("0x"):
            return first[2:].lower()
    except OSError:
        pass
    # Fallback for down/unattached resources.
    try:
        out = subprocess.check_output(
            ["drbdadm", "dump-md", TIER_RESOURCE], timeout=3
        )
        for line in out.decode().splitlines():
            s = line.strip()
            if s.startswith("current-uuid"):
                parts = s.split()
                if len(parts) >= 2:
                    return parts[1].rstrip(";").lower().replace("0x", "")
    except Exception:
        pass
    return ""


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
    # Release the .254 VIP at every cluster size. promote_to_arbiter_host
    # binds it in both the N=1 and the N>=2 paths, so demote must
    # release it in both too — otherwise a follower with a stale .254
    # from a previous role briefly answers on the cluster VIP.
    _ip_del()
    if drbd_present:
        _svc_stop(ARBITER_SVC)
        _umount()
        _drbd_secondary()
    # Per cluster-quorum-spec.md INV-7: tag.lms=1 never times out.
    # After self-demote we MUST clear our lms bit so a survivor's
    # takeover protocol can proceed without operator intervention.
    # The netd tick pushes the new tag via set_own_slot on its next
    # heartbeat. This write only succeeds when the witness is
    # reachable from us; if the witness is unreachable now, netd
    # will keep retrying on every subsequent tick as long as we
    # remain running. If we shut down or die before the write lands,
    # the slot stays lms=1 and operator override is required to
    # unstick the cluster (see docs/operator-overrides.md).
    if SHARED_STATE is not None and SHARED_STATE.netd_ws is not None:
        try:
            try:
                from . import witness as _witness
            except ImportError:
                import sys as _sys
                _sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import witness as _witness  # type: ignore
            ws = SHARED_STATE.netd_ws
            marker = (_read_local_drbd_uuid() or "").encode("ascii")
            _witness.set_own_slot(ws, marker=marker, tag=0)
        except Exception as e:
            log.warning("arbiter: post-demote slot clear failed: %s "
                        "(LMS may stick if we shut down before retry)", e)
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
        if outcome in ("noquorum", "follower"):
            return False
        # outcome == "" / "init" → fall through to legacy path while
        # the daemon is still warming up.
    try:
        s = json.loads(STATE_JSON.read_text())
        return "mgmt" in (s.get("role") or "")
    except Exception:
        return False


def converge() -> dict:
    """Single-shot converge: actuate local hosting state to match
    what the realtime layer (netd's election + witness slot) has
    decided. If I should host singletons and don't, promote; if I
    shouldn't and do, demote. Called from the orchestrator's
    converge_retry tick and from boot_orchestrator after role settles.

    "Hosting" at N>=2 means: arbiter rqlite running on .254 + DRBD
    primary + mount + filer + s3. At N=1 it means: filer + s3 + .254.

    Layering rule (load-bearing — see docs/cluster-quorum-spec.md):
    converge() NEVER writes to rqlite. Master selection lives in the
    realtime layer (election → witness slot → cluster.json via
    netd.set_mgmt_master). cluster_arbiter just actuates whatever the
    realtime layer has put in cluster.json. The rqlite ``cluster_info``
    row is a follower of the realtime decision, not the source of it.

    Two flavours of "am I hosting":
      * ``am_host_complete`` — every singleton is up. Skip promote.
      * ``am_host_partial`` — any singleton state present. Demote
        when cluster.json names someone else, even if only .254 is
        bound (so a follower with leftover state from a prior role
        is reliably cleaned up).
    """
    should_host = i_should_host_arbiter()
    status = arbiter_status()
    drbd_present = _drbd_resource_exists()
    if drbd_present:
        # Promote-direction: skip the promote ONLY if every singleton
        # is already up. Any missing piece → re-promote (idempotent).
        am_host_complete = (status["service_active"]
                            and status["ip_present"])
        # Demote-direction: ANY singleton state present → demote needs
        # to run to clean it up. A stale .254 on a follower (after a
        # failed promote, or a leftover from a prior role) must be
        # released even if no other state is up.
        am_host_partial = (status["service_active"]
                           or status["ip_present"]
                           or status["mounted"])
    else:
        try:
            try:
                from . import seaweedfs
            except ImportError:
                import sys
                sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import seaweedfs  # type: ignore
            filer_active = seaweedfs.is_filer_active()
        except Exception:
            filer_active = False
        am_host_complete = filer_active and status["ip_present"]
        am_host_partial  = filer_active or status["ip_present"]
    # Two-sided am_host: ``complete`` is the "everything's up" check
    # used to skip promote; ``partial`` is the "anything's up" check
    # used to fire demote when our role disagrees with cluster.json.
    am_host = am_host_complete

    # Note: cluster_arbiter intentionally does NOT write to rqlite.
    # Master selection is owned by the realtime layer (netd's
    # election + the witness slot). The rqlite ``cluster_info`` row
    # is a follower of that decision, written exclusively by netd's
    # ``set_mgmt_master`` path once it sees a stable LEADER outcome.
    # converge()'s job is to actuate hosting state (filer, .254,
    # DRBD primary) based on what the realtime layer has decided
    # via cluster.json — never the reverse.

    if should_host and not am_host_complete:
        return promote_to_arbiter_host()
    if not should_host and am_host_partial:
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
