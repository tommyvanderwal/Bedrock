"""Arbiter rqlite mobility — DRBD-promote + IP-add + service-start.

  * The arbiter is a SECOND rqlite daemon co-resident with the
    elected mgmt master, providing the 3rd Raft voter that
    rqlite (which doesn't natively understand Bedrock's
    witness-weighted election) needs at N=2 physical.
  * The arbiter's data + WAL live on a shared DRBD volume named
    `cluster`, mounted at `/var/lib/bedrock/cluster/`. The
    SeaweedFS filer's leveldb3 metadata + S3 IAM also live here,
    so the same DRBD-promote + mount sequence moves all
    cluster-wide singletons together.
  * The arbiter's network identity is a SECONDARY `100.X.Y.254/32`
    added to `lo` on the hosting node (NOT a real interface).
    From rqlite's perspective the address is constant across
    master moves — the IP simply changes which `lo` it lives on.
    A constant address on lo dissolves rqlite's catch-22: it never
    has to be told that its peer moved.

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

# `cluster`: the shared DRBD volume hosting all cluster-singleton
# services (rqlite-arbiter, SeaweedFS filer metadata, S3 IAM, future
# singletons). Must align with tier_storage.CLUSTER_RESOURCE.
TIER_RESOURCE   = "cluster"
MOUNT_POINT     = Path("/var/lib/bedrock/cluster")
ARBITER_DATA    = MOUNT_POINT / "rqlite"
ARBITER_SVC     = "bedrock-rqlited-arbiter.service"

# Local marker tier_storage writes ONCE the N=1->N=2 cluster-singleton DRBD
# transition is complete (resource up + N=1 data restored). The election-
# driven promote gates on this so it defers to tier_storage during the
# transition instead of racing its create-md/up/restore. Local + persistent
# => readable during failover (INV-6: no rqlite on the takeover path).
# Path MUST agree with tier_storage.CLUSTER_DRBD_MARKER.
CLUSTER_DRBD_MARKER = Path("/etc/bedrock/cluster-drbd-ready")

# The reserved arbiter octet + the full VIP derivation now live in
# cluster_addr (cluster_addr.ARBITER_VIP_OCTET / cluster_addr.cluster_vip)
# so bedrock-net and the arbiter share one source of truth.

# Peer-heartbeat freshness for the steal-back guard (C2/M12) and the
# last-standing check (H6). netd sends an election heartbeat ~1 Hz, so a
# peer seen within ~2 s is live. Beyond that it has gone silent and is
# treated as not-reachable / not-claiming-master.
PEER_HB_FRESH_S = 2.0

STATE_JSON      = Path("/etc/bedrock/state.json")

# Cold-boot patience: at 2+ nodes, a node that comes up believing it is
# master waits this long before the FIRST promote so a slower peer can
# catch up and a cleaner convergence wins (EXECUTION-PLAN BAD-1 timing
# table). Single-node clusters promote immediately. Settable low in
# tests. Tracked from process start via _COLD_BOOT_AT.
COLD_BOOT_PATIENCE_S = 30.0
_COLD_BOOT_AT = time.monotonic()

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


# Process-lifetime memo of the derived arbiter .254 IP. cluster_uuid is
# IMMUTABLE for the life of a cluster: the ONLY writer is cluster_init()'s
# one-time INSERT (see bedrock_state.py; set_cluster_name() — the rename saga
# — touches cluster_name only, never cluster_uuid). A different uuid can only
# arise from a reset→reinit, which restarts bedrock-d and drops this memo with
# the process. So no in-process invalidation is needed or possible.
# Why this matters: arbiter_loopback_ip() is the single funnel for
# _arbiter_ip_present / _ip_add / _ip_del / arbiter_status, so a steady Leader
# tick was issuing 5-8 identical "SELECT cluster_uuid" rqlite round-trips/sec
# for a constant (py-spy: ~16% of bedrock-d). The memo collapses that to one
# attribute read after the first successful resolve.
_ARBITER_IP_MEMO: str = ""


def arbiter_loopback_ip() -> str:
    """The arbiter's `100.X.Y.254/32` IP for this cluster. Reads
    cluster_uuid from local rqlite (level='none', works without
    quorum) and derives the deterministic cluster prefix (same
    algorithm as for node loopback IPs). Memoized: cluster_uuid is
    immutable, so after the first resolve this is a constant."""
    global _ARBITER_IP_MEMO
    if _ARBITER_IP_MEMO:
        return _ARBITER_IP_MEMO
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
    # Do NOT memoize the empty/error result: pre-init or a transient rqlite
    # blip must self-heal on the next tick (and _ip_add still raises loud on
    # an unknown IP rather than acting on a guess).
    if not uuid:
        return ""
    try:
        from . import cluster_addr
    except ImportError:
        import sys
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import cluster_addr  # type: ignore
    _ARBITER_IP_MEMO = cluster_addr.cluster_vip(uuid)
    return _ARBITER_IP_MEMO


# ─────────────────────────────────────────────────────────────────────
# DRBD steps
# ─────────────────────────────────────────────────────────────────────


def _drbd_role() -> str:
    """Returns 'Primary' / 'Secondary' / 'Unknown' for the cluster resource.
    'Unknown' covers both "drbdadm errored" and "resource not
    configured" (N=1 case, where no DRBD resource exists at all)."""
    rc, out, _ = _run(["drbdadm", "role", TIER_RESOURCE])
    if rc != 0:
        return "Unknown"
    head = (out or "").strip().split("/")[0]
    return head or "Unknown"


def _drbd_resource_exists() -> bool:
    """True once tier_storage has finished setting up + handing off the
    cluster-singleton DRBD tier (the `cluster-drbd-ready` marker exists).

    Deliberately NOT `drbdadm dump` (config-file parse): that is true the
    instant tier_storage writes the .res — mid-transition, before
    `drbdadm up` — so the election-driven promote would fire a premature
    `drbdadm primary` ("Unknown resource", spammed every tick) and could
    even mount the empty DRBD volume before tier_storage restores the N=1
    snapshot (data-loss race). Gating on the marker makes tier_storage the
    sole owner of the N=1->N=2 transition; cluster_arbiter only takes over
    (.254 + arbiter rqlite) once the data is in place. False at N=1 and
    during the transition. Local marker => failover-safe (no rqlite)."""
    return CLUSTER_DRBD_MARKER.exists()


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
    # A stale arbiter mount blocks promotion ("-12 Device is held open by mount"): a prior
    # master demoted but its frozen/EIO mount couldn't be umounted at the time. A node that is
    # PROMOTING is (about to be) the host but is currently Secondary, and a Secondary must NOT
    # be mounted — so a held-open-by-mount here is ALWAYS a stale mount. The demote branch of
    # converge (which umounts) never runs for a should-host node, so the promote path must
    # self-heal it: lazy-detach + retry. (See _umount / the EIO-zombie-mount lesson.) Without
    # this, a node that was master a few failovers ago can never re-promote -> winner-promote
    # stalls -> the cluster ends a failover with NO master.
    if "held open" in err.lower() or "open_cnt" in err.lower():
        log.warning("arbiter: promote blocked by stale arbiter mount; clearing + retrying")
        _umount()
        rc, _, err = _run(["drbdadm", "primary", TIER_RESOURCE], timeout=30)
        if rc == 0:
            return
    # Two cases need --force, and in BOTH the cluster election + witness are
    # the single source of authority for who owns the data — if they say we are
    # master (we only get here from a Leader outcome), we are:
    #   * "Need access to UpToDate data" — the previous primary is unreachable.
    #   * "No quorum" — with `quorum all`, DRBD refuses `primary` on a side that
    #     lacks FULL replica quorum. But a witness-pivotal / minority takeover
    #     is exactly that: bedrock-d's election (all nodes + witness) blessed us
    #     even though DRBD's local replica count is short. --force is the
    #     "bedrock-d says keep going" promote signal; the losing side is frozen
    #     (quorum all) so it cannot be writing, so forcing primary here is safe.
    el = err.lower()
    if "uptodate" in el or "need access" in el or "quorum" in el:
        log.warning("arbiter: drbdadm primary refused (%s); retrying with "
                    "--force per election authority",
                    "no quorum" if "quorum" in el else "peer unreachable")
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


# One-shot guard: pin the DRBD safety resource-options once per daemon lifetime
# (the kernel keeps them until the resource goes down).
_DRBD_SAFETY_DONE = False


def _enforce_drbd_safety_options() -> None:
    """Pin the load-bearing DRBD resource-options on the live arbiter resource.

    DRBD ships dangerous defaults for a quorum-critical singleton:
      * ``auto-promote yes`` — self-promotes to Primary the moment the device
        is opened. We forbid it (``auto-promote no``): the election + witness
        are the SOLE promote authority, and only bedrock-d's ``drbdadm primary``
        ever promotes. With self-promote off, two nodes can never both be
        Primary → 2-primary split-brain impossible by construction.
      * ``quorum off`` — a Primary keeps writing (and rotates its current-UUID)
        even in a minority. We set ``quorum all`` + ``on-no-quorum suspend-io``
        so a Primary that loses ANY peer FREEZES instantly (no writes, no UUID
        rotation) — DRBD must stop the moment it loses a node, not wait for
        bedrock-d to notice (by then the UUID has advanced and IOPs crossed the
        line = split brain). On peer loss DRBD calls the fence-peer handler
        (``bedrock-fence-peer``), which returns bedrock-d's fresh+stable election
        verdict — outdate the peer (continue) or outdate self (yield). This
        function also deploys that handler + applies ``fencing resource-only``.
      * ``on-suspended-primary-outdated force-secondary`` — a returning frozen
        Primary that reconnects to a newer-generation Primary auto-demotes
        (backstop; bedrock-d's self-demote is the primary clean path).

    The rendered .res declares all of these (fresh installs); this live-sets
    them on clusters that predate the change. Idempotent, forks at most once per
    daemon start. The core trio is set together; the (newer) outdated-handling
    is best-effort so an older drbd-utils that rejects the flag still gets the
    rest."""
    global _DRBD_SAFETY_DONE
    if _DRBD_SAFETY_DONE:
        return
    # Deploy the fence-peer handler FIRST — DRBD must be able to run it before
    # fencing is active (a missing handler -> drbdadm error -> DRBD leaves IO
    # frozen: safe, but not what we want). Idempotent.
    try:
        from . import fence_verdict as _fv
    except ImportError:
        from lib import fence_verdict as _fv  # type: ignore
    try:
        _fv.deploy_handler()
    except OSError as e:
        log.warning("arbiter: fence-peer handler deploy failed: %s", e)
    rc, _, _ = _run(["drbdsetup", "resource-options", TIER_RESOURCE,
                     "--auto-promote=no", "--quorum=all",
                     "--on-no-quorum=suspend-io"])
    if rc != 0:
        return  # retry next tick; don't latch a partial apply
    # Best-effort (drbd-utils >= 9.1.7); harmless if unsupported.
    _run(["drbdsetup", "resource-options", TIER_RESOURCE,
          "--on-suspended-primary-outdated=force-secondary"])
    # Apply `fencing resource-only` + the fence-peer handler from the (updated)
    # .res live — for clusters that predate the change; a no-op on fresh installs
    # already up from the new .res. THIS is what makes DRBD call bedrock-fence-peer
    # on peer loss instead of the removed resume-io path.
    _run(["drbdadm", "adjust", TIER_RESOURCE])
    _DRBD_SAFETY_DONE = True


def _drbd_suspended_quorum() -> bool:
    """True iff the arbiter resource currently has IO suspended because it lost
    DRBD quorum (``suspended:quorum`` in drbdsetup status)."""
    rc, out, _ = _run(["drbdsetup", "status", TIER_RESOURCE])
    return rc == 0 and "suspended:quorum" in (out or "")


# (REMOVED) _drbd_resume_io / ensure_drbd_write_permission — the resume-io path
# was the spurious-UUID ROOT CAUSE: it fired every converge tick on a STALE,
# unconfirmed cached election outcome, and `drbdadm resume-io` on a quorum-lost
# armed Primary made DRBD mint a new current-UUID (zero writes) -> false
# split-brain on heal. Replaced by the native fence-peer arbiter callout: on
# losing a peer DRBD calls `bedrock-fence-peer`, which returns bedrock-d's
# FRESH + STABLE election verdict (fence_verdict.py) as exit 4 (outdate peer ->
# regain quorum -> continue) / 6 (outdate self -> yield) / 1 (leave IO frozen).
# See docs/drbd-fence-peer-arbiter-design.md + bug-reports-upstream/.


def _drbd_up() -> bool:
    """``drbdadm up`` the arbiter resource (attach + connect as Secondary).

    The arbiter DRBD is deliberately NOT systemd-auto-started (install.sh
    disables drbd.service) so a promote never races a half-configured resource
    at boot — bedrock-d owns the decision and calls this once it holds the tier.
    Bringing it up needs no quorum and no Primary: peers connect and resync on
    UUID lineage as Secondary. Idempotent: returns True if already up."""
    rc, _, err = _run(["drbdadm", "up", TIER_RESOURCE], timeout=30)
    e = (err or "").lower()
    if rc == 0 or "already" in e or "exists" in e:
        return True
    log.warning("arbiter: drbdadm up %s failed: %s", TIER_RESOURCE, err.strip())
    return False


def ensure_arbiter_drbd_up() -> None:
    """Quorum-aware boot / self-heal for the arbiter (cluster-tier) DRBD.

    Runs on EVERY node that holds the arbiter tier (the cluster-drbd-ready
    marker) — followers included — so the master's writes replicate and a
    failover target always holds an UpToDate copy. Brings the resource up as
    Secondary if it's down (cold boot, crash) and pins auto-promote=no. The
    elected master separately promotes to Primary in promote_to_arbiter_host().
    Idempotent + cheap once up.

    R5 / degraded recovery: with auto-promote=no the only StandAlone we could
    ever see is a genuine 2-primary divergence — which the election/witness make
    impossible by construction. So if one is ever observed we FAIL LOUD and
    leave the data untouched for an operator (never silently discard); the
    normal path is just up→Secondary→resync."""
    if not _drbd_resource_exists():
        return  # N=1 / not in the arbiter tier yet — nothing to up
    if _drbd_role() == "Unknown":
        log.info("arbiter: cluster DRBD is down — bringing it up as Secondary "
                 "(quorum-aware boot/self-heal)")
        if not _drbd_up():
            return
    _enforce_drbd_safety_options()
    # Surface a StandAlone connection loudly. It should be unreachable under
    # auto-promote=no + the election authority; if it happens, data has
    # genuinely diverged and only an operator (or a future fenced resolver)
    # should choose a survivor — we do NOT auto-discard.
    rc, out, _ = _run(["drbdadm", "cstate", TIER_RESOURCE])
    if rc == 0 and "StandAlone" in (out or ""):
        log.error("arbiter: cluster DRBD is StandAlone (split-brain) — NOT "
                  "auto-resolving; manual recovery required. cstate=%r",
                  (out or "").strip())


# ─────────────────────────────────────────────────────────────────────
# Mount steps
# ─────────────────────────────────────────────────────────────────────


def _is_mounted(path: Path) -> bool:
    """True iff `path` itself is a mount point (not just on a mounted FS).

    Reads /proc/self/mounts directly and matches the EXACT mountpoint — NO stat() of the
    path. Two requirements this satisfies:
      * Don't false-positive on the root FS: `findmnt -T` returns the containing filesystem
        ("/" for an unmounted /var/lib/bedrock/cluster), which once made promote skip the
        mount and the filer wrote leveldb to the root FS (invisible to peers). Matching the
        exact mountpoint string avoids that.
      * SEE an EIO "zombie" mount: when DRBD demotes the device to Secondary (e.g. the
        outdated loser becomes SyncTarget on heal) while the XFS is still mounted, every I/O
        to that mount returns EIO. `mountpoint -q` stat()s the path → EIO → it falsely reports
        "not mounted", so converge never cleans the zombie and the held-open device blocks the
        next promote (`-12 Device is held open`). The mount TABLE needs no I/O to the FS."""
    target = str(path)
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == target:
                    return True
    except OSError:
        pass
    return False


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
    rc, _, err = _run(["umount", str(MOUNT_POINT)], timeout=20)
    if rc != 0 and _is_mounted(MOUNT_POINT):
        # A DRBD device demoted to Secondary under a live mount leaves an EIO "zombie": a
        # normal umount can't sync/stat it. Lazy-detach frees the device (the next promote
        # needs it) and the FS refs drain asynchronously. This is the cleanup path for the
        # outdated loser after a failover heal (the force-secondary couldn't umount frozen).
        log.warning("arbiter: umount %s failed (%s); lazy-detaching (-l)",
                    MOUNT_POINT, err.strip())
        _run(["umount", "-l", str(MOUNT_POINT)], timeout=20)


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
        raise RuntimeError("arbiter loopback IP unknown (cluster_uuid not in rqlite?)")
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

    N=1 mode (no cluster DRBD resource): bind .254 + start
    singletons on local FS. .254 is bound at every N including N=1
    per docs/storage-architecture.md so client config (filer URL,
    mgmt URL) is identical from day 1.

    Idempotent: safe to call on every role tick. Witness checks are
    skipped when we are already hosting (DRBD primary + mounted +
    .254 bound).
    """
    n = _cluster_size()
    drbd_present = _drbd_resource_exists()
    ip = ""

    if not drbd_present:
        # N=1: no DRBD, no witness consult. Simple path.
        log.info("arbiter: promoting (N=%d, no cluster DRBD — "
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
            # Only (re)start the filer when it's actually down — was an
            # unconditional 3-fork systemctl burst on every converge tick.
            if not seaweedfs.is_filer_active():
                seaweedfs.promote_to_filer_host()
        except Exception as e:
            log.warning("arbiter: SeaweedFS filer promote failed: %s", e)
        # H5/INV-6: mgmt_master is a RESULT of a confirmed promote, never
        # the trigger. At N=1 there's no arbiter rqlite, so write it as
        # soon as hosting (filer + .254) is confirmed. One status read,
        # reused for the mgmt_master gate and the return value.
        status = arbiter_status()
        _set_mgmt_master_after_promote(drbd_present=False, status=status)
        return status

    # N>=2 with cluster DRBD. converge_retry calls this every 1Hz tick, so
    # only log at INFO when we actually actuate (the cold path below) — the
    # steady-state self-renew must stay silent (it was spamming "promoting"
    # + "mgmt_master written" every second into journald + the obs pipeline).
    log.debug("arbiter: promote tick (N=%d, cluster DRBD present)", n)

    # Idempotent fast-path: if I am already hosting (DRBD primary +
    # mounted + .254 bound), skip the witness protocol. This handles
    # every tick after the initial promotion (converge_retry calls
    # promote repeatedly).
    am_already_host = (
        _drbd_role() == "Primary"
        and _is_mounted(MOUNT_POINT)
        and _arbiter_ip_present()
    )

    try:
        from . import rqlite_setup
        from . import seaweedfs
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import rqlite_setup  # type: ignore
        from lib import seaweedfs  # type: ignore

    if not am_already_host:
        # Steps 1-5: takeover protocol. Refuse to promote if it fails.
        if not _run_takeover_protocol():
            log.error("arbiter: takeover protocol REFUSED — not promoting")
            return arbiter_status()
        # Steps 6: hardware/software state changes — assert the
        # DRBD-primary + mount + .254 trio.
        log.info("arbiter: promoting (N=%d, cluster DRBD present)", n)
        # On a cold boot / post-crash the resource is down (systemd does not
        # auto-start it); `drbdadm primary` on a down resource fails "Unknown
        # resource". Bring it up (Secondary) + pin auto-promote=no first.
        # Idempotent no-op when already up.
        _drbd_up()
        _enforce_drbd_safety_options()
        _drbd_promote()
        _mount()
        ARBITER_DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
        ip = _ip_add()
        log.info("arbiter: promotion complete (ip=%s mount=%s)",
                 ip, MOUNT_POINT)
    # else: steady-state self-renew. converge_retry calls promote on EVERY
    # 1Hz tick; the DRBD-primary + mount + .254 trio was JUST confirmed live
    # by the am_already_host probe above. Re-running _drbd_promote/_mount/
    # _ip_add would only re-fork drbdadm/mount/ip to re-discover what we
    # already know — a ~10+ fork/s no-op on the steady master (py-spy RCA
    # 2026-06-01). Any drift (a peer steals primary, an unmount, a flushed
    # .254) flips am_already_host False next tick and the full actuation
    # above fires. So skip the trio re-assertion when already hosting.

    # Heal the targets the trio-probe does NOT cover — every tick, but
    # cheaply (only fork to FIX when actually down). The arbiter rqlited
    # service, the env file, and the SeaweedFS filer can each die
    # independently of the DRBD/mount/.254 trio, so they must be re-checked
    # even on the already-hosting path.
    rqlite_setup.render_arbiter_env_file()
    _svc_start(ARBITER_SVC)
    if not seaweedfs.is_filer_active():
        # Was an unconditional reset-failed + 2× systemctl start every tick;
        # now only when the filer is actually down.
        try:
            seaweedfs.promote_to_filer_host()
        except Exception as e:
            log.warning("arbiter: SeaweedFS filer promote failed: %s", e)
    # H5/INV-6: write mgmt_master only AFTER the arbiter rqlite is back, as a
    # RESULT of a confirmed promote — never the trigger. ONE arbiter_status()
    # read (was two: here + inside _set_mgmt_master_after_promote). It only
    # writes when DRBD primary + .254 + arbiter service are all up;
    # set_mgmt_master is write-if-changed (L57), so the steady tick is a cheap
    # local read, not a Raft write.
    status = arbiter_status()
    _set_mgmt_master_after_promote(drbd_present=True, status=status)
    return status


def _set_mgmt_master_after_promote(*, drbd_present: bool,
                                   status: Optional[dict] = None) -> None:
    """Write this node as the rqlite ``mgmt_master`` — but ONLY once a
    promote has actually taken hold (H5 / INV-6 two-tier ordering).

    The base layer (netd's election) DRIVES the promote; mgmt_master is
    written here as the RESULT, after arbiter_status() confirms this node
    is hosting. mgmt_master is never the promote trigger — the promote
    drives the write, not the other way round.

    Hosting confirmation:
      * N>=2 (drbd_present): service_active AND ip_present AND DRBD
        Primary — the arbiter rqlite is up, so the write commits.
      * N=1: filer + .254 — there's no arbiter rqlite to wait on; the
        local rqlite is the only voter and accepts the write.

    No deadlock: this never gates the promote on mgmt_master already
    being set; it runs strictly after the promote. If the write fails
    (rqlite still electing), the next converge tick re-promotes
    (idempotent, a no-op) and retries the write.

    `status` lets the caller pass the arbiter_status() it just read so we
    don't re-fork drbdadm/mountpoint/ip a second time within one promote
    (the caller reads it once and reuses it for the return value too)."""
    status = arbiter_status() if status is None else status
    if drbd_present:
        hosting = (status.get("service_active")
                   and status.get("ip_present")
                   and status.get("drbd_role") == "Primary")
    else:
        try:
            try:
                from . import seaweedfs
            except ImportError:
                import sys as _sys
                _sys.path.insert(0, "/usr/local/lib/bedrock")
                from lib import seaweedfs  # type: ignore
            filer_active = seaweedfs.is_filer_active()
        except Exception:
            filer_active = False
        hosting = filer_active and status.get("ip_present")
    if not hosting:
        log.info("arbiter: promote not yet fully hosting "
                 "(%s) — deferring mgmt_master write to next tick",
                 {k: status.get(k) for k in
                  ("service_active", "ip_present", "drbd_role")})
        return
    self_name = _self_node_name()
    if not self_name:
        log.warning("arbiter: cannot write mgmt_master — self node name "
                    "unknown")
        return
    try:
        try:
            from . import bedrock_state as _bs
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import bedrock_state as _bs  # type: ignore
        rev = _bs.set_mgmt_master(self_name)
        # DEBUG not INFO: this runs every 1Hz converge tick, but
        # set_mgmt_master is write-if-changed (L57) so it's a no-op read on
        # the steady master — an INFO line/sec of "written" was both spammy
        # and misleading (nothing was written). A real role change still
        # shows up via the netd election + the rev bump.
        log.debug("arbiter: mgmt_master=%s confirmed after promote "
                  "(rqlite rev=%s)", self_name, rev)
    except Exception as e:
        log.warning("arbiter: set_mgmt_master deferred — %s "
                    "(will retry next converge tick)", e)


def _self_node_name() -> str:
    """This node's name, read from state.json (per-node truth, no
    rqlite quorum needed)."""
    try:
        s = json.loads(STATE_JSON.read_text())
        return s.get("node_name") or ""
    except Exception:
        return ""


def _run_takeover_protocol() -> bool:
    """Steps 1-5 of the arbiter takeover protocol. Returns True if it
    is safe to proceed with drbdadm primary + service starts.

    NO rqlite calls on this path — the cluster's rqlite is the very
    service we're about to recover, so it cannot be a precondition.

    Fast-path: if rqlite cluster_info says I'm already the mgmt_master
    (or no master is recorded yet), there's nothing to take over from.
    The protocol's job is to gate failover from another node; for the
    first promotion at storage-promote or the periodic self-renew
    after I've already been confirmed master, we skip witness checks
    entirely. netd publishes our slot every tick regardless.
    """
    if SHARED_STATE is None or SHARED_STATE.netd_ws is None:
        # netd not wired / not running: fall back to "always allow" so a
        # standalone invocation (CLI, boot before netd attaches) can still
        # promote.
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

    # C2/M12 STEAL-BACK GUARD: a returning old master would otherwise
    # hit the fast path below (last_master_id == my_id) and promote,
    # stealing the role from the live survivor that already took over
    # while we were gone. If a peer's FRESH heartbeat advertises ITSELF
    # as master (a live/forming master that is not us), defer — never
    # steal the role back. This also covers the normal path. A legitimate
    # first-takeover (no peer claims master) proceeds.
    claimer = _peer_claims_master_now(ws)
    if claimer:
        log.info("arbiter: takeover DEFERRED — peer %r is currently "
                 "claiming master (fresh heartbeat); not stealing the "
                 "role back", claimer)
        return False

    # FAST PATH: no prior master OR self is the recorded master.
    # Nothing to take over FROM another node; proceed with the
    # promotion. The netd tick is already publishing our slot every
    # second, so when a witness IS present it will see us. No witness
    # reachability gate needed at this path (covers first-ever promote
    # where the operator hasn't deployed an Echo yet).
    last_master_id = _last_known_master_node_id()
    if last_master_id is None or last_master_id == my_id:
        # Cold-boot guard (cluster-quorum-spec cold-boot protocol): if
        # the witness holds OUR OWN slot from a previous life and that
        # marker is a generation we no longer have locally, the cluster
        # advanced without us — refuse to promote a stale copy. This is
        # the one UUID check that applies even with no other master,
        # and it is rqlite-free.
        if not _cold_boot_uuid_ok(ws, _witness):
            log.error("arbiter: takeover REFUSED — cold-boot UUID check: "
                      "local DRBD generation is older than our own last "
                      "published slot marker. The cluster advanced without "
                      "us; refusing to promote a stale copy (operator must "
                      "reconcile / `seize`).")
            return False
        # 2+ node patience: give a slower peer time to come up and beat
        # us cleanly before we self-promote. Single-node skips this.
        if _cluster_size() >= 2:
            waited = time.monotonic() - _COLD_BOOT_AT
            if waited < COLD_BOOT_PATIENCE_S:
                log.info("arbiter: cold-boot patience — %.0fs of %.0fs "
                         "elapsed at N>=2; deferring first promote",
                         waited, COLD_BOOT_PATIENCE_S)
                return False
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

    # Step 1+2: inspect M's slot. Per cluster-quorum-spec.md INV-7, a
    # witness claim NEVER times out. A stale slot with claim=1 means the
    # previous master died WHILE the witness was still pivotal for its
    # quorum and could not release its own claim — only an operator
    # decommission can clear it (see docs/operator-overrides.md). A master
    # that had a node-majority releases its claim itself, so its slot reads
    # claim=0 here and takeover proceeds without operator action.
    slot_m = _witness.read_slot(ws, last_master_id)
    if slot_m is None:
        # INV-7 "missing slot = worst case assumed". A missing slot
        # for a still-known cluster member might mean the witness
        # rebooted and lost its map; we cannot rule out that the
        # missing slot previously held a claim. Operator must
        # decommission the node from the rqlite `nodes` table
        # (which makes us ignore it entirely) or re-key the
        # witness identity.
        log.error("arbiter: takeover REFUSED — last master "
                  "node_id=%d has no slot at witness. Per INV-7 a "
                  "missing slot is treated as worst-case (could have "
                  "held a witness claim). Operator must decommission this "
                  "node from the rqlite `nodes` table, or re-key "
                  "the witness identity (see docs/operator-overrides.md).",
                  last_master_id)
        return False
    if not slot_m.is_stale():
        # Master's slot is FRESH. Death-oracle: refuse ONLY if it also says
        # HOSTING — i.e. the master is alive AND actually hosting (docs/
        # witness-death-oracle.md). A fresh-but-NOT-hosting slot means the
        # master relinquished/demoted while still alive → safe to take over.
        if slot_m.hosting:
            log.info("arbiter: takeover REFUSED — slot[%d] is fresh + HOSTING; "
                     "master is alive and hosting, cluster healthy elsewhere",
                     last_master_id)
            return False
        log.info("arbiter: slot[%d] fresh but NOT hosting (master relinquished); "
                 "continuing", last_master_id)
    elif slot_m.claim:
        log.error("arbiter: takeover REFUSED — slot[%d] is stale "
                  "but holds a witness claim. Previous master died while "
                  "the witness was pivotal for its quorum and never "
                  "released the claim; claims do not time out. Operator "
                  "must decommission the dead node (`bedrock node leave`) "
                  "before takeover can proceed (see docs/operator-overrides.md).",
                  last_master_id)
        return False
    else:
        log.info("arbiter: slot[%d] stale and holds no witness claim; continuing",
                 last_master_id)
    # Step 3: our local DRBD current-UUID must equal the master's published
    # marker for the SAME data generation. Both are read role-bit-masked
    # (`_read_local_drbd_uuid` here; netd publishes the marker via the same
    # masked reader), and we re-mask the marker defensively so a marker from
    # any other source still compares on the GENERATION, never DRBD's
    # primary-role flag (bit 0). DRBD itself masks `& ~((u64)1)` on every
    # current-UUID compare — without this an in-sync Secondary (UpToDate,
    # oos:0) is wrongly refused as 'diverged' from the ex-Primary's marker.
    # See docs/cluster-quorum-spec.md INV-5.
    local_uuid_step3 = _read_local_drbd_uuid()
    slot_marker = _mask_drbd_role_bit(
        slot_m.marker.decode("ascii", errors="replace"))
    if not local_uuid_step3:
        log.error("arbiter: takeover REFUSED — local DRBD current-UUID "
                  "unreadable (resource attaching?); deferring")
        return False
    if local_uuid_step3 != slot_marker:
        log.error("arbiter: takeover REFUSED — DRBD divergence: "
                  "local gen-uuid=%s vs slot[%d].marker=%s (role-masked). "
                  "Operator must reconcile (drbdadm invalidate or "
                  "wait for peer).",
                  local_uuid_step3[:12], last_master_id, slot_marker[:12])
        return False
    log.info("arbiter: DRBD generation match (%s); proceeding to claim",
             local_uuid_step3[:12])

    # Step 4: claim the witness as part of the takeover handshake — set our
    # own slot tag=claim. The arbiter OWNS the claim bit: netd does not
    # recompute own_tag, so this explicit set is authoritative and the
    # step-5 readback can't be raced back to 0. netd's election tick (1 Hz)
    # ships whatever tag we set here on its next heartbeat, and only
    # refreshes own_marker — never the tag. If this takeover lands us with a
    # node-majority (the witness wasn't actually pivotal), the next Leader
    # tick's ensure_witness_claim() releases this claim again — it sticks
    # only while the witness remains pivotal for our quorum.
    local_uuid = _read_local_drbd_uuid()
    marker_bytes = local_uuid.encode("ascii") if local_uuid else b""
    _witness.set_own_slot(ws, marker=marker_bytes, tag=_witness.TAG_CLAIM)

    # Step 5: read it back. Wait up to 3 attempts × ~1.5 s = ~4.5 s.
    expected_marker = marker_bytes
    for attempt in range(1, 4):
        time.sleep(1.5)  # let netd send + receive at least one round-trip
        own = _witness.own_slot(ws)
        if own is not None and own.claim and own.marker == expected_marker:
            log.info("arbiter: own slot readback OK (attempt %d, claim=1, "
                     "marker=%s)", attempt, local_uuid[:12])
            return True
        log.warning("arbiter: own-slot readback attempt %d not yet "
                    "reflecting claim+marker (have=%r)", attempt,
                    own and (own.claim, own.marker[:12]))
    log.error("arbiter: takeover REFUSED — own-slot readback failed "
              "after 3 attempts; witness unreachable or losing writes")
    return False


def _fresh_peer_hbs() -> dict:
    """Return {peer_name: hb} for every peer whose election heartbeat is
    FRESH (seen within PEER_HB_FRESH_S). Reads SHARED_STATE.netd.peer_hb,
    the live per-peer heartbeat record netd maintains. Empty if netd
    isn't wired or no peer is fresh."""
    out: dict = {}
    st = SHARED_STATE
    d = getattr(st, "netd", None) if st is not None else None
    peer_hb = getattr(d, "peer_hb", None) if d is not None else None
    if not peer_hb:
        return out
    now = time.monotonic()
    for peer, hb in peer_hb.items():
        if not isinstance(hb, dict):
            continue
        if (now - hb.get("seen_at_monotonic", 0.0)) <= PEER_HB_FRESH_S:
            out[peer] = hb
    return out


def _peer_claims_master_now(ws) -> "str | None":
    """C2/M12 steal-back cross-check. Return a peer name iff some peer has
    a FRESH heartbeat advertising ITSELF as master (believed_master ==
    that peer), i.e. a live/forming master that is NOT us. Returns None
    when no peer is currently claiming the role (the legitimate
    first-takeover case).

    rqlite-free: reads only netd's in-memory peer heartbeat records."""
    my_name = getattr(ws, "my_node_name", "") or _self_node_name()
    for peer, hb in _fresh_peer_hbs().items():
        if peer == my_name:
            continue
        if hb.get("believed_master") == peer:
            return peer
    return None


def ensure_witness_claim(ws, *, node_has_majority: bool) -> bool:
    """Maintain THIS node's witness claim each Leader tick (H6 / INV-3,
    INV-7). The claim is an exclusive reservation (SCSI-3-PR style) of the
    witness's tie-breaking vote.

    Set/release rule — owned solely by the claiming node, never auto-expires,
    never copied from another slot:
      * `node_has_majority` is False  → the witness is PIVOTAL (our node-votes
        alone fall short of quorum; we only reach this branch as elected
        LEADER, so node-votes + witness already crosses the line). CLAIM it.
      * `node_has_majority` is True   → node-majority is (re)established; the
        witness is no longer needed. RELEASE any claim we hold.

    This is the auto-release the old `ensure_lms_if_last_standing` lacked: a
    healthy master that grew back to a node-majority drops its claim itself,
    so its slot reads claim=0 and a survivor's takeover proceeds WITHOUT an
    operator override. Operator action (`bedrock node leave`) is needed ONLY
    when the claiming node dies permanently and can't release its own claim.

    `node_has_majority` is computed by the caller (netd) from the same
    election result that put us in the Leader branch:
        100 * reachable_active_nodes(incl self) >= majority.

    This also publishes the HOSTING bit (actuation-truth) every tick — it is the
    death-oracle signal a far node reads to decide the master is alive
    (docs/witness-death-oracle.md). HOSTING and CLAIM are INDEPENDENT: a master
    with a node-majority publishes HOSTING without CLAIM; a pivotal master
    publishes HOSTING|CLAIM; a node that isn't actually hosting publishes neither.

    Idempotent. Returns True iff we changed the tag this call.
    """
    if ws is None:
        return False
    try:
        try:
            from . import witness as _witness
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import witness as _witness  # type: ignore
    except Exception:
        return False

    # The witness must be valid + confirmed for us to read our own slot back.
    # If it isn't, leave the tag untouched and retry next tick (we can't safely
    # flip a bit we can't read).
    if not (_witness.is_valid(ws) and _witness.is_confirmed(ws)):
        return False
    own = _witness.own_slot(ws)
    cur_tag = own.tag if own is not None else 0

    # HOSTING is actuation-truth: are we genuinely the arbiter host RIGHT NOW?
    status = arbiter_status()
    drbd_present = _drbd_resource_exists()
    if drbd_present:
        hosting = bool(status.get("service_active")
                       and status.get("ip_present")
                       and status.get("drbd_role") == "Primary")
    else:
        hosting = bool(status.get("ip_present"))

    if not hosting:
        # We're in the Leader branch but not actually hosting yet (mid-promote)
        # or we lost actuation — publish NO HOSTING/CLAIM so a far node never
        # treats us as alive-and-hosting on stale truth. (demote clears it too.)
        if cur_tag == 0:
            return False
        _witness.set_own_slot(
            ws, marker=(_read_local_drbd_uuid() or "").encode("ascii"), tag=0)
        return True

    # We host. CLAIM only when the witness is pivotal (node-votes short).
    want_claim = not node_has_majority
    desired = _witness.TAG_HOSTING | (_witness.TAG_CLAIM if want_claim else 0)
    if cur_tag == desired:
        return False

    local_uuid = _read_local_drbd_uuid()
    marker = local_uuid.encode("ascii") if local_uuid else b""
    adding_claim = want_claim and not (own is not None and own.claim)
    log.info("arbiter: publishing witness tag hosting=1 claim=%d (marker=%s)",
             1 if want_claim else 0, local_uuid[:12] if local_uuid else "")
    _witness.set_own_slot(ws, marker=marker, tag=desired)
    if not adding_claim:
        # HOSTING-only or claim-release write — no readback barrier needed;
        # netd republishes own_tag each heartbeat.
        return True
    # We just ADDED the claim — readback-confirm so a peer's takeover sees
    # fresh+claim and defers. Best-effort; netd keeps publishing.
    for _ in range(3):
        time.sleep(1.5)
        back = _witness.own_slot(ws)
        if back is not None and back.claim and back.marker == marker:
            log.info("arbiter: witness claim readback confirmed")
            return True
    log.warning("arbiter: witness claim set but readback not yet confirmed "
                "(witness slow/unreachable); netd will keep publishing")
    return True


def _cold_boot_uuid_ok(ws, _witness) -> bool:
    """Cold-boot DRBD-UUID-vs-own-slot check (cluster-quorum-spec
    cold-boot protocol). Returns True (safe to promote) unless we can
    PROVE our local generation is stale.

    The witness may still hold OUR OWN slot from a previous life. If its
    marker differs from our current local DRBD UUID *and* our local UUID
    is classified SUPERSEDED in our own 7-day history (a newer generation
    once existed locally and we've since regressed), the cluster advanced
    without us — refuse. If the witness has no slot for us, or the marker
    matches, or we have no contradicting history, allow: a cold node with
    nothing to compare against is the legitimate first-promote case.

    rqlite-free: only the witness slot cache + local state.json history.
    """
    try:
        own = _witness.own_slot(ws)
    except Exception:
        own = None
    if own is None or not own.marker:
        return True
    # Role-bit-mask the slot marker, symmetric with takeover step 3: compare
    # data GENERATION, never DRBD's primary-role bit 0 (DRBD masks & ~((u64)1)).
    # local_uuid is already masked by _read_local_drbd_uuid. Markers we publish
    # are masked-at-source, so this is a no-op today, but it must not trust that
    # invariant any more than step 3 does.
    slot_marker = _mask_drbd_role_bit(own.marker.decode("ascii", errors="replace"))
    local_uuid = _read_local_drbd_uuid()
    if not local_uuid or local_uuid == slot_marker:
        return True
    # Markers differ. Consult our own history: is the local generation
    # superseded (older than something we recorded)?
    try:
        try:
            from . import state as _lstate
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import state as _lstate  # type: ignore
        cls = _lstate.classify_arbiter_uuid(local_uuid)
        return cls != _lstate.UUID_SUPERSEDED
    except Exception:
        # Can't classify — don't block the legitimate first promote.
        return True


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


def _mask_drbd_role_bit(uuid_hex: str) -> str:
    """Clear DRBD's primary-role flag (bit 0 of the current UUID) and return
    bare lower-case hex; "" on junk.

    DRBD overloads the least-significant bit of the current UUID as the
    Primary/Secondary role flag, set/cleared on role change WITHOUT minting
    a new UUID — so an ex-Primary and an in-sync Secondary that share a data
    generation differ ONLY in bit 0. DRBD's own code masks it on every
    current-UUID comparison (`drbd_uuid_compare`/`receive_uuids`:
    `x & ~((u64)1)`). The takeover step-3 check must do the same, else a
    healthy `UpToDate`/`oos:0` Secondary is wrongly refused as 'diverged'
    from the master's published marker."""
    u = (uuid_hex or "").strip().lower()
    if u.startswith("0x"):
        u = u[2:]
    u = u.rstrip(";")
    if not u:
        return ""
    try:
        return f"{int(u, 16) & ~1:x}"
    except ValueError:
        return ""


def _read_local_drbd_uuid(resource: str = TIER_RESOURCE) -> str:
    """Read `resource`'s **live current UUID, role-bit masked** (defaults to
    the cluster singleton TIER_RESOURCE; the VM-failover path passes a pet
    resource). Returns "" if DRBD isn't configured or the live value can't be
    read — the caller then defers (never acts on a guess).

    Source of truth: DRBD9 debugfs
    ``/sys/kernel/debug/drbd/resources/<r>/volumes/0/data_gen_id``. The
    FIRST whitespace token of the first line is the current UUID; later
    tokens are per-peer BITMAP UUIDs and later lines are HISTORY UUIDs — we
    must NOT read those (an old current that has rolled into the bitmap is a
    different generation). Bit 0 is the role flag, masked via
    `_mask_drbd_role_bit` so the marker and a peer's read compare equal.

    `drbdadm dump-md` is used ONLY when the resource is genuinely DETACHED
    (debugfs file absent — the N=1 scratch path), where on-disk current-uuid
    IS the live one. On an ATTACHED resource dump-md is unreliable (it can
    report a value that has since become a bitmap UUID), so a transient
    debugfs read error there returns "" (defer) rather than a stale guess.
    `drbdadm current-uuid` does NOT exist in DRBD 9.x — don't reach for it."""
    debugfs = (
        f"/sys/kernel/debug/drbd/resources/{resource}/volumes/0/"
        "data_gen_id"
    )
    try:
        with open(debugfs, "r") as f:
            first = f.readline().strip()
        tok = first.split()[0] if first else ""   # current UUID = token 0
        if tok.startswith("0x"):
            return _mask_drbd_role_bit(tok)
        # debugfs present but unparseable → defer; do NOT use stale dump-md.
        return ""
    except FileNotFoundError:
        # debugfs absent. If this IS the arbiter tier resource and it is
        # configured-but-down (marker present, cold boot before
        # ensure_arbiter_drbd_up has attached it), dump-md only spams
        # `drbdmeta ... dump-md exit-255` on every election tick and tells us
        # nothing live → defer; the up reconcile attaches it within seconds.
        if resource == TIER_RESOURCE and _drbd_resource_exists():
            return ""
        # else: genuinely detached / scratch — on-disk current-uuid IS live.
    except OSError:
        return ""  # transient read error on an attached resource → defer
    try:
        out = subprocess.check_output(
            ["drbdadm", "dump-md", resource], timeout=3,
            stderr=subprocess.DEVNULL,   # don't leak drbdmeta errors to journal
        )
        for line in out.decode().splitlines():
            s = line.strip()
            if s.startswith("current-uuid"):
                parts = s.split()
                if len(parts) >= 2:
                    return _mask_drbd_role_bit(parts[1])
    except Exception:
        pass
    return ""


def _force_release_drbd() -> None:
    """HARD-release a FROZEN arbiter device (suspended:quorum). No clean shutdown is possible.

    The device has IO suspended (it lost DRBD quorum), so a graceful `systemctl stop` of the
    arbiter rqlite flushes-and-blocks, and a normal `umount` fsyncs-and-blocks — both hang
    forever on the suspended device. And we must NEVER `resume-io` to unstick them: resuming
    would let the minority write, the exact spurious-UUID bug the freeze prevents. So we KILL:

      1. SIGKILL the arbiter rqlite + every other process holding the mount. Their dirty pages
         are DROPPED at the kernel level when the process dies — never flushed — so the minority
         STILL never writes. (Tommy's call: the arbiter must be killed, no clean shutdown.)
      2. `umount -l` (lazy): detaches the mount from the namespace without touching the frozen
         FS, releasing the device's open count.
      3. `drbdadm secondary`: with open_cnt==0 the device demotes + discards the frozen writes.
         The outdated loser then reconnects as a 0-pri Secondary so after-sb-0pri
         discard-zero-changes auto-resolves the heal (no StandAlone tangle). DRBD's
         on-suspended-primary-outdated=force-secondary would also fire once the mount is freed;
         the explicit call is a deterministic backstop.
    """
    log.warning("arbiter: DRBD frozen (quorum lost) — HARD-releasing (force Secondary, then "
                "kill holders). No clean shutdown; never resume-io.")
    # DEMOTE FIRST — this is the one operation that must be fast. `drbdsetup secondary
    # --force=yes` demotes a frozen (suspended:quorum) Primary INSTANTLY (~0 s, verified on the
    # testbed) even with the mount + arbiter-rqlite still attached: it EIOs any held/pending IO
    # and forces the role to Secondary. The pending writes are discarded — the loser is the
    # outdated minority, they'd be lost anyway, and crucially this is NOT resume-io (nothing is
    # flushed to the minority). Doing this FIRST is the whole fix: the old order did fuser +
    # umount first, which BLOCK ~tens of seconds on the frozen FS, so the demote landed after
    # the heal and the loser reconnected as a Primary -> 1pri/2pri -> StandAlone/resync-stall.
    rc, _, err = _run(["drbdsetup", "secondary", TIER_RESOURCE, "--force=yes"], timeout=20)
    if rc != 0:                              # older drbdsetup without --force, or transient
        rc, _, err = _run(["drbdadm", "secondary", TIER_RESOURCE], timeout=15)
    if rc == 0:
        log.info("arbiter: force-release demoted to Secondary — heal will resync clean")
    else:
        log.error("arbiter: force-release could not demote to Secondary: %s", (err or "").strip())
    # Now the device is a (read-only) Secondary, so these no longer block on a frozen Primary.
    # Kill EVERY holder of the arbiter mount BY SERVICE NAME — the arbiter rqlite AND the
    # SeaweedFS filer (singleton) + s3 both keep data on this mount, and if the filer survives
    # it holds the device open ("-12 held open") and blocks this node's NEXT promote.
    # ☠️ NEVER `fuser -k -m <mount>` here: on an EIO zombie mount fuser stats the path, gets
    # EIO, falls back to the containing mount '/', and -k SIGKILLs the whole box (sshd + init).
    # SIGKILL (not stop) so a flush can't hang; lazy umount (no fsync) after.
    for _svc in (ARBITER_SVC, "bedrock-weed-filer.service", "bedrock-weed-s3.service"):
        _run(["systemctl", "kill", "--signal=SIGKILL", _svc], timeout=8)
    _run(["umount", "-l", str(MOUNT_POINT)], timeout=10)


def demote_arbiter_host() -> dict:
    """Stop hosting the singleton services. Reverse of promote.

    Two modes mirroring promote_to_arbiter_host():

      * If cluster DRBD is present: stop filer + s3, stop
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
    frozen = drbd_present and _drbd_suspended_quorum()
    # Release the .254 VIP at every cluster size + state (cheap, no IO — safe even when frozen).
    # promote binds it in both N=1 and N>=2, so demote must release it in both — otherwise a
    # follower with a stale .254 from a prior role briefly answers on the cluster VIP.
    _ip_del()
    if frozen:
        # DRBD froze this node on quorum loss (suspended:quorum). A clean shutdown is IMPOSSIBLE
        # — a graceful filer/rqlite stop flushes and a normal umount fsyncs, both blocking
        # forever on the suspended device — and we must never resume-io (minority-write = the
        # spurious-UUID bug). So HARD-KILL the holders + force the device Secondary, discarding
        # the frozen writes at the kernel level. The outdated loser then reconnects as a 0-pri
        # Secondary and after-sb-0pri discard-zero-changes auto-resolves the heal. This is the
        # fix for the StandAlone/resync-stall tangle (the old code left the device frozen here).
        _force_release_drbd()
    else:
        # Not frozen (graceful role handover, or N=1): clean stop is safe. SeaweedFS S3 + filer
        # first — they use the mount, must stop before umount.
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
            _umount()
            _drbd_secondary()
    # Per cluster-quorum-spec.md INV-7: a witness claim never times out.
    # After self-demote we MUST clear our claim bit so a survivor's
    # takeover protocol can proceed without operator intervention. (This is
    # the demote-path release; ensure_witness_claim() is the steady-state
    # release when node-majority returns without a demote.)
    # The netd tick pushes the new tag via set_own_slot on its next
    # heartbeat. This write only succeeds when the witness is
    # reachable from us; if the witness is unreachable now, netd
    # will keep retrying on every subsequent tick as long as we
    # remain running. If we shut down or die before the write lands,
    # the slot stays claim=1 and operator decommission is required to
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
            log.warning("arbiter: post-demote claim clear failed: %s "
                        "(claim may stick if we shut down before retry)", e)
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
            "leader"   → True  (host)
            "noquorum" → False (we lost quorum; demote in progress)
            "follower" → False (peer is master)
      2. Fallback to state.json["role"] for standalone invocations with
         no shared state. Returns False if missing.

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
        # outcome == "" / "init" → fall through to the state.json path
        # while the daemon is still warming up.
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

    Layering rule (load-bearing — H5/INV-6, see docs/cluster-quorum-spec.md):
    master selection lives in the realtime layer (netd's election). netd
    DRIVES the promote on a Leader outcome; converge() is the idempotent
    reconcile safety net that re-actuates hosting state if it drifts.
    mgmt_master in rqlite is a RESULT, written at the END of
    promote_to_arbiter_host() only after the arbiter rqlite is back —
    never the promote trigger. So converge() reaches rqlite only
    transitively (via promote's post-hosting mgmt_master write), and only
    once hosting is confirmed; the rqlite ``cluster_info`` row is a
    follower of the realtime decision, not the source of it.

    Two flavours of "am I hosting":
      * ``am_host_complete`` — every singleton is up. Skip promote.
      * ``am_host_partial`` — any singleton state present. Demote
        when the realtime layer says a peer is master, even if only
        .254 is bound (so a follower with leftover state from a prior
        role is reliably cleaned up).
    """
    # Quorum-aware boot / self-heal: ensure the arbiter DRBD is up (Secondary)
    # on every node that holds the tier — followers included — so the master's
    # writes replicate and a failover target stays UpToDate. Runs before the
    # hosting decision so a would-be master finds the resource already attached
    # when it promotes. Idempotent + cheap once up.
    ensure_arbiter_drbd_up()

    # NOTE: write-permission is NO LONGER driven from converge. On peer loss DRBD
    # itself calls the fence-peer handler (bedrock-fence-peer), which asks
    # bedrock-d the fresh+stable election verdict and outdates the peer (continue)
    # or itself (yield). The removed ensure_drbd_write_permission/resume-io was
    # the spurious-UUID root cause. _enforce_drbd_safety_options() deploys the
    # handler + applies `fencing resource-only`.

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
    # used to fire demote when our role disagrees with the realtime
    # election outcome.
    am_host = am_host_complete

    # Note: master selection is owned by the realtime layer (netd's
    # election + the witness slot). converge() actuates hosting state
    # (filer, .254, DRBD primary) to match that decision; the rqlite
    # ``cluster_info.mgmt_master`` row is written only as a RESULT, at
    # the end of promote_to_arbiter_host() once hosting is confirmed
    # (H5/INV-6) — never as the promote trigger.

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
