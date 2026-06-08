"""Bedrock management-plane orchestrator (rqlite edition).

Cluster state lives in rqlite. This orchestrator runs as a set of
asyncio tasks inside the bedrock-mgmt FastAPI process — one Python
process per node, hosting:

  ① rqlite_subscriber    poll bedrock_meta.revision; on advance,
                          rebuild snapshot from rqlite, project this
                          node's role/URL to state.json, run the
                          snapshot-diff reactor.
  ② boot_orchestrator    on startup: wait for clear cluster role
                          (quorum established by netd's election),
                          then bring up the per-VM DRBD resources this
                          node hosts, start libvirtd, and start the VMs
                          that belong here. Nothing acts before quorum.
                          The `cluster` singleton's DRBD primary/mount/
                          .254 VIP/arbiter rqlite/filer are owned by
                          cluster_arbiter.converge().
  ③ no_quorum_responder  on no-quorum marker (dropped by netd's
                          election when this node loses quorum):
                          pause running VMs, then clear the marker
                          when election regains quorum + reconcile
                          paused VMs against the now-current cluster
                          state:
                              moved/destroyed → virsh destroy +
                                                drbdadm secondary
                              still ours      → virsh resume
                          re-run start_local_services for
                          re-promotion.
  ④ reactor              snapshot-diff-driven side-effects —
                          vm-host changed, backup_target appeared,
                          etc., once services are up.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/bedrock")

from lib import view_builder, rqlite_client

log = logging.getLogger("bedrock.orchestrator")

CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")
NO_QUORUM_MARKER = Path("/run/bedrock-no-quorum")

# Cleanup itself is fast — virsh suspend on local VMs is seconds.
# This is the cap on the cleanup procedure only; a stuck cleanup is
# left for the operator to troubleshoot (no auto-reboot).
NO_QUORUM_CLEANUP_TIMEOUT_S = 30.0

# Live in-memory snapshot, updated by rqlite_subscriber and read by
# other tasks (and by the FastAPI handlers that want fresh state).
#
# Under bedrock-d (the unified daemon) these globals are bound to
# fields on the shared state_shared.BedrockState object by
# orchestrator.attach_state(); standalone bedrock-mgmt uses the module
# globals directly. The accessor helpers below abstract over both.
_SNAPSHOT: dict = view_builder.empty_snapshot()
# bedrock_meta.revision of the last snapshot we observed. Named
# _LAST_LOG_IDX because external readers reference that name; it holds
# the rqlite revision.
_LAST_LOG_IDX: int = 0
# Previous snapshot — kept so the reactor can diff prev→cur to drive
# transition handling (vm_destroyed, vm_migrated, etc.).
_PREV_SNAPSHOT: dict = view_builder.empty_snapshot()
_SERVICES_STARTED: bool = False
# The main asyncio event loop, captured in start_all() (which runs IN the
# loop thread). _apply_revision runs in a thread executor, where
# asyncio.get_event_loop() raises "no current event loop in thread" — so
# cross-thread task spawns (the reactor_diff) MUST use this captured ref
# via call_soon_threadsafe. (Bug pre-2026-05-30: get_event_loop() in the
# worker thread raised, was swallowed, and reactor_diff never ran.)
_MAIN_LOOP = None
# The central cluster-state loop (ClusterStateSource), held so its supervised
# task is not garbage-collected.
_CLUSTER_SRC = None

# Wake event for operations_drain. The central loop sets it on every detected
# change so a freshly-queued node-dispatched saga (vm_backup / vm_restore) runs
# near-instantly instead of waiting out the drain's poll floor. Created in
# start_all (bound to the running loop); the drain keeps its own poll floor as
# the correctness backstop, so a missed wake just means "3s later", never lost.
_OPS_WAKE = None

# When non-None, all snapshot/last_log_idx/services_started reads + writes
# go through this object instead of the module globals above. Set by
# `attach_state(state)` from the unified bedrock-d entrypoint.
_STATE = None


# ── helpers ──────────────────────────────────────────────────────────────

def attach_state(state) -> None:
    """Hook the unified daemon's shared state object so subscriber +
    no_quorum_responder + boot + converge_retry + backup all read/write
    through it. Called once at bedrock-d startup."""
    global _STATE
    _STATE = state


def _self_node_name() -> str:
    if not STATE_JSON.exists():
        return ""
    try:
        return json.loads(STATE_JSON.read_text()).get("node_name", "") or ""
    except Exception:
        return ""


def _running_vm_names() -> list[str]:
    """Names of VMs currently in `running` state. Empty if libvirtd is
    down — that's fine, nothing to pause anyway."""
    r = subprocess.run(
        ["virsh", "list", "--state-running", "--name"],
        capture_output=True, text=True
    )
    return [n.strip() for n in r.stdout.splitlines() if n.strip()]


def _paused_vm_names() -> list[str]:
    r = subprocess.run(
        ["virsh", "list", "--state-paused", "--name"],
        capture_output=True, text=True
    )
    return [n.strip() for n in r.stdout.splitlines() if n.strip()]


def _vm_drbd_resource(vm_name: str) -> str | None:
    """Find the DRBD resource backing this VM's primary disk, by parsing
    `virsh dumpxml` for `/dev/drbdN`. Returns None for cattle (local LV)."""
    r = subprocess.run(
        ["virsh", "dumpxml", vm_name],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    minor_match = re.search(r"<source dev='/dev/drbd(\d+)'", r.stdout)
    if not minor_match:
        return None
    minor = minor_match.group(1)
    for cfg in Path("/etc/drbd.d").glob("*.res"):
        try:
            text = cfg.read_text()
        except Exception:
            continue
        # Match the minor to a resource name. We accept either an
        # explicit `minor N;` line or the `/dev/drbdN` device path.
        if re.search(rf"minor\s+{minor}\b", text) or f"/dev/drbd{minor}" in text:
            res_match = re.search(r"resource\s+(\S+)\s*\{", text)
            if res_match:
                return res_match.group(1)
    return None


def _drbd_role(resource: str) -> str:
    """Return one of: 'Primary', 'Secondary', 'Unknown'."""
    r = subprocess.run(
        ["drbdadm", "role", resource],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return "Unknown"
    return (r.stdout or "").strip().split("/")[0] or "Unknown"


def _vm_drbd_resources(vm_name: str) -> list[str]:
    """Configured DRBD resources backing this VM, from /etc/drbd.d.

    A VM's disks are written as `vm-<name>-disk<N>.res` (see the
    vm_create saga's write_drbd_config step + bedrock_d/vm/drbd_config
    res_file_path). Cattle VMs sit on a local LV with no `.res` file →
    empty list, and nothing to bring up. Returns every disk so
    multi-disk VMs are handled, not just disk0."""
    prefix = f"vm-{vm_name}-disk"
    resources = []
    for cfg in sorted(Path("/etc/drbd.d").glob(f"{prefix}*.res")):
        m = re.search(r"resource\s+(\S+)\s*\{", cfg.read_text() or "")
        if m:
            resources.append(m.group(1))
    return resources


def _bring_up_vm_drbd(vm_name: str) -> None:
    """`drbdadm up` every DRBD resource backing this VM so /dev/drbdN
    exists before libvirtd opens it. Idempotent — `drbdadm up` on an
    already-up resource is a harmless no-op. Promote is NOT done here:
    per-VM primary/secondary is decided by the vm_failover / create
    paths, not by the cold-boot reconcile (which would risk dual-primary
    if a peer already holds the VM)."""
    for res in _vm_drbd_resources(vm_name):
        log.info("services: drbdadm up %s (for VM %s)", res, vm_name)
        subprocess.run(["drbdadm", "up", res], check=False)


def get_snapshot() -> dict:
    """Read-only access to the live snapshot for FastAPI handlers."""
    if _STATE is not None:
        with _STATE.snapshot_lock:
            return _STATE.snapshot
    return _SNAPSHOT


# ── ① central cluster-state loop (ClusterStateSource) ────────────────────
#
# The single per-node detector of cluster-state changes lives in
# bedrock_d/orchestrator/cluster_loop.py. It polls the revision (master-first
# read with a really-handled local fallback) — and exposes check_now() so
# rqlite CDC on the leader can wake it near-instantly (poll is the floor, CDC
# the speed-up). On each detected advance it calls _cluster_change below, which
# is the ONE fan-out: build the snapshot at the detected read level and run the
# reactors. This replaces the old rqlite_subscriber / _subscriber_pass.

async def _cluster_change(revision: int, level: str) -> None:
    """Dispatcher invoked by ClusterStateSource on a detected revision
    advance. Runs the (blocking) reactor set in a worker thread so the
    event loop stays responsive, preserving the prior all-in-a-thread
    behavior. Re-resolves self_name each call (recovers the joiner race
    where bedrock-d started before state.json had node_name). NOT
    blanket-caught — a crash escalates to supervise(), never swallowed."""
    await asyncio.to_thread(_apply_at_level, revision, level)
    # The state just changed — nudge the node-dispatched saga drain so a
    # vm_backup/vm_restore op the leader just queued for this node runs now
    # rather than waiting out its 3s poll floor. We are on the main loop here,
    # so set the event directly (no cross-thread hop needed). The drain's poll
    # floor still backstops a missed wake.
    if _OPS_WAKE is not None:
        _OPS_WAKE.set()


def _apply_at_level(revision: int, level: str) -> None:
    """Build the snapshot at `level` (with a FRESH client) and run the
    reactors. A fresh client per change means a client created before
    rqlite was listening can never wedge the loop."""
    with rqlite_client.RqliteClient() as rc:
        _apply_revision(rc, revision, _self_node_name(), level=level)


def signal_check_now() -> bool:
    """Wake the central cluster-state loop IMMEDIATELY — the CDC fast path:
    'cluster state changed, converge now' instead of waiting for the poll
    floor. Thread-safe: schedules check_now on the main loop, so it is safe
    to call from a request handler thread. Returns True if the loop was
    signalled, False if it isn't up yet (caller can fall back to the poll)."""
    if _MAIN_LOOP is not None and _CLUSTER_SRC is not None:
        _MAIN_LOOP.call_soon_threadsafe(_CLUSTER_SRC.check_now)
        return True
    return False


def fanout_check_now_blocking() -> None:
    """Leader-side CDC fan-out: tell every OTHER node to converge NOW.

    Called (in a worker thread — it does blocking HTTP) when the local
    rqlited, which is the Raft leader, delivers a CDC event. Each peer is
    nudged via a signed POST to its /api/internal/check-now over the mesh
    loopback address.

    Best-effort BY DESIGN, and this is the FULL handling of a failed POST:
    a peer we can't reach simply converges via its own poll floor (the
    correctness backstop that always runs), so the cluster stays correct —
    CDC is only the speed-up. A per-peer failure is therefore logged loud
    and skipped, never escalated. We do NOT swallow a failure to even
    enumerate peers: that is logged at ERROR (the fan-out did nothing this
    commit; every peer falls back to its poll floor)."""
    self_name = _self_node_name()
    from lib import cluster_state as _cs
    from lib import peer_auth as _pa
    try:
        nodes = (_cs.load_cluster(level="none").get("nodes") or {})
    except Exception:
        log.error("cdc fanout: could not enumerate peers — every node will "
                  "converge via its poll floor this commit", exc_info=True)
        return
    ok = total = 0
    for name, n in nodes.items():
        if name == self_name:
            continue
        lip = (n.get("loopback_ip") or "").strip()
        if not lip:
            log.warning("cdc fanout: node %s has no loopback_ip — skipping "
                        "(it converges via its poll floor)", name)
            continue
        total += 1
        try:
            _pa.request("POST", f"https://{lip}:8443/api/internal/check-now",
                        {}, self_name, timeout=3.0)
            ok += 1
        except Exception as e:
            log.warning("cdc fanout to %s (%s) failed: %s — that node "
                        "converges via its poll floor", name, lip, e)
    log.info("cdc fanout: nudged %d/%d peers to converge now", ok, total)


def _apply_revision(rc: rqlite_client.RqliteClient, revision: int,
                    self_name: str, level: str = "weak") -> None:
    """Refresh the in-memory snapshot from rqlite, project to disk,
    queue a reactor task. `level` is the rqlite read consistency the
    central ClusterStateSource detected at ('strong' = via the leader,
    'weak' = local replica fallback) — the snapshot BUILD uses the same
    level so we never detect a strong revision then build a stale-local
    snapshot."""
    global _LAST_LOG_IDX, _PREV_SNAPSHOT, _SNAPSHOT
    # Re-resolve node identity each fold. On a joiner, bedrock-d (and
    # this subscriber) can start BEFORE the join saga writes node_name
    # into state.json, so rqlite_subscriber captured self_name=''. Re-
    # reading here lets the subscriber recover the instant state.json is
    # populated — without it, self_name stays '' for the process
    # lifetime and backend-on-self convergence (this node becoming a
    # vm/vl backend) + the state.json role projection never fire.
    if not self_name:
        self_name = _self_node_name()
    if _STATE is not None:
        with _STATE.snapshot_lock:
            prev = copy.deepcopy(_STATE.snapshot)
            new = view_builder.build_snapshot(client=rc, level=level)
            _STATE.snapshot = new
            _STATE.prev_snapshot = prev
            _STATE.last_log_idx = int(new.get("log_index", revision))
        # Keep module globals in lockstep so callers doing
        # `from orchestrator import _SNAPSHOT` see the same data.
        # Cheap, ~few μs per tick.
        _SNAPSHOT = new
        _PREV_SNAPSHOT = prev
        _LAST_LOG_IDX = _STATE.last_log_idx
    else:
        prev = copy.deepcopy(_SNAPSHOT)
        new = view_builder.build_snapshot(client=rc)
        _SNAPSHOT.clear()
        _SNAPSHOT.update(new)
        _LAST_LOG_IDX = int(new.get("log_index", revision))

    # state.json projection of this node's role + mgmt_url. Consumers
    # query cluster state from rqlite directly via
    # cluster_state.load_cluster() (level='none'); state.json holds the
    # per-node fields that need to survive cold-boot without rqlite:
    # node identity + the derived role + master URL for that node.
    try:
        if self_name and self_name in (_SNAPSHOT.get("nodes") or {}):
            existing = {}
            if STATE_JSON.exists():
                try:
                    existing = json.loads(STATE_JSON.read_text())
                except Exception as e:
                    # A corrupt / 0-byte state.json is a real node-bricking
                    # failure mode (see lessons-log: state.json self-heal). We
                    # DO self-heal — rebuild from the rqlite projection below
                    # (existing stays {}) — but make it LOUD: a silent rebuild
                    # would hide the underlying cause (disk fault, prior crash
                    # mid-write) until it bites again.
                    log.warning("rqlite_subscriber: existing state.json "
                                "unreadable (%s) — rebuilding from rqlite "
                                "projection at rev %d", e, revision)
            existing.update(view_builder._state_view(_SNAPSHOT, self_name))
            view_builder._atomic_write_json(STATE_JSON, existing)
    except Exception as e:
        log.warning("rqlite_subscriber: state.json projection at rev %d: %s",
                    revision, e)

    # Observability reconciler — converge local vmagent/vlagent and
    # (conditionally) bedrock-vm / bedrock-vl to whatever the snapshot's
    # obs_backends list says. Idempotent.
    try:
        from lib import observability as _obs
        _obs.reconcile(_SNAPSHOT, self_name)
    except Exception as e:
        log.warning("rqlite_subscriber: obs.reconcile at rev %d: %s",
                    revision, e)

    # DRBD multi-path config regen on mesh path-table change.
    try:
        from installer.lib import tier_storage as _ts  # type: ignore
    except ImportError:
        try:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import tier_storage as _ts  # type: ignore
        except Exception:
            _ts = None
    if _ts is not None:
        try:
            _ts.regen_drbd_configs_from_snapshot(_SNAPSHOT)
        except Exception as e:
            log.warning("rqlite_subscriber: drbd config regen at rev %d: %s",
                        revision, e)

    # Arbiter mobility: converge based on whether this node currently
    # holds the mgmt master role. promote/demote is idempotent so this
    # is safe on every revision tick.
    try:
        try:
            from installer.lib import cluster_arbiter as _ca  # type: ignore
        except ImportError:
            import sys as _sys2
            _sys2.path.insert(0, "/usr/local/lib/bedrock")
            from lib import cluster_arbiter as _ca  # type: ignore
        _ca.converge()
    except Exception as e:
        log.warning("rqlite_subscriber: cluster_arbiter converge at rev %d: %s",
                    revision, e)

    # ISO library FUSE mount targets the current mgmt-master's
    # loopback /32. Re-render the unit on every revision tick so the
    # mount target follows the master across role transitions.
    # Idempotent: ensure_iso_library_mount only rewrites the unit +
    # daemon-reloads + restarts when the rendered content actually
    # changed.
    try:
        try:
            from installer.lib import seaweedfs as _sw  # type: ignore
        except ImportError:
            from lib import seaweedfs as _sw  # type: ignore
        _sw.ensure_iso_library_mount()
    except Exception as e:
        log.warning("rqlite_subscriber: iso_library_mount at rev %d: %s",
                    revision, e)

    # Snapshot-diff reactor — on each revision advance, derive
    # transitions from prev→cur and run side effects (vm destroyed,
    # vm host changed, tier master changed, backup target added).
    # Spawn the prev->cur transition reactor on the MAIN loop. We are in a
    # worker thread (run_in_executor), where asyncio.get_event_loop()
    # raises "no current event loop in thread" — so use the captured
    # _MAIN_LOOP ref and call_soon_threadsafe to cross threads. (The old
    # get_event_loop() call raised every tick and was swallowed, so this
    # reactor never ran; dropping a diff means a revision's transitions —
    # vm destroyed/migrated, backup target appeared — skip their side
    # effects, so a missing loop is logged LOUD, not silently passed.)
    if _MAIN_LOOP is not None:
        _MAIN_LOOP.call_soon_threadsafe(
            lambda: asyncio.create_task(_reactor_diff(prev, _SNAPSHOT, self_name))
        )
    else:
        log.error("rqlite_subscriber: main loop not captured — reactor_diff "
                  "transitions skipped (start_all not run yet?)")
    _PREV_SNAPSHOT = prev


# ── ② boot_orchestrator ──────────────────────────────────────────────────

async def boot_orchestrator():
    """One-shot at mgmt startup: wait for cluster contact, then start
    DRBD/libvirtd/VMs that should run on this node."""
    global _SERVICES_STARTED
    role = await _wait_for_role(timeout_s=120.0)
    if role in ("noquorum", "", "unknown"):
        log.error("boot: role=%r — not starting local services; "
                  "no_quorum_responder or future state changes will trigger start",
                  role)
        return
    log.info("boot: role=%s; starting local services", role)
    await _start_local_services()
    _SERVICES_STARTED = True


async def _wait_for_role(timeout_s: float,
                         ignore_marker: bool = False) -> str:
    """Poll rqlite (via cluster_state.load_cluster) until we have a
    quorum-recorded mgmt_master. Returns 'leader' if we are master,
    'follower' if a peer is, or 'noquorum' / 'unknown' on timeout. The
    cluster state is read straight from the local rqlite replica, so
    this becomes 'follower' or 'leader' as soon as quorum is back.

    `ignore_marker=True`: skip the NO_QUORUM_MARKER early-return. Used
    by no_quorum_responder — the caller will clear the marker once
    quorum is back.

    Quorum-is-back signal: at least one peer is mesh-reachable AND
    rqlite has a recorded mgmt_master (someone, possibly us). We do
    NOT gate on last_election_outcome — the election returns NO_QUORUM
    as long as the marker is present, which is circular (we're the
    ones holding the marker waiting for quorum to be back so we can
    clear it). Mesh peer-liveness is the only live, marker-
    independent quorum signal we have."""
    deadline = time.monotonic() + timeout_s
    self_name = _self_node_name()
    while time.monotonic() < deadline:
        if not ignore_marker and NO_QUORUM_MARKER.exists():
            return "noquorum"
        # Live mesh check (skips circular marker dependency).
        if ignore_marker and _STATE is not None and _STATE.netd is not None:
            try:
                with _STATE.netd_lock:
                    d = _STATE.netd
                    any_peer_up = any(
                        getattr(n, "logged_up", False)
                        for n in getattr(d, "neighbours", {}).values()
                    )
            except Exception:
                any_peer_up = False
            if not any_peer_up:
                # No peer reachable; can't be quorate at any N. Wait.
                await asyncio.sleep(1)
                continue
        try:
            from lib import cluster_state as _cs
            # ignore_marker=True is used by no_quorum_responder after
            # this node has been partitioned. The local rqlite replica
            # may still show the pre-partition mgmt_master (stale by
            # Raft replication lag) — force a Raft round-trip so we
            # see who actually owns the cluster now. Without 'strong',
            # _reconcile_paused_vms would virsh-resume the local VM
            # against stale vms.host and we'd split-brain.
            level = "strong" if ignore_marker else "none"
            cluster = _cs.load_cluster(level=level)
        except Exception:
            cluster = {}
        master = cluster.get("mgmt_master") or ""
        if master:
            return "leader" if master == self_name else "follower"
        await asyncio.sleep(1)
    return "unknown"


async def _start_local_services():
    """Bring this node's local services up to the state rqlite says
    they should be in. Idempotent — safe at boot, after a no-quorum
    cycle, or when re-running because the log changed.

    Reads at level='strong'. We pay a Raft round-trip on every call
    but the alternative (level='none' against the local replica) is
    a real race: post-no-quorum we run RIGHT AFTER
    _reconcile_paused_vms destroyed the local copy of a VM that the
    peer has taken over, but the stale local replica still says
    vms.host = self / state = running → virsh start →
    split-brain."""
    try:
        from lib import cluster_state as _cs
        cluster = _cs.load_cluster(level="strong")
    except Exception:
        cluster = {}
    vms = cluster.get("vms", {}) or {}
    self_name = _self_node_name()

    # The `cluster` singleton (arbiter rqlite + filer/s3 + .254 VIP + its
    # DRBD primary/mount) is owned by cluster_arbiter; the elected master
    # promotes it to Primary in converge(). But the arbiter DRBD must first be
    # brought UP (as Secondary) on EVERY node that holds the tier — it is
    # deliberately NOT systemd-auto-started (quorum-aware boot), so nothing else
    # attaches it after a reboot. Without this the master would run Primary with
    # no connected secondaries (writes unprotected) and a follower would have no
    # UpToDate copy to fail over to. Eager here (don't wait for the 5s converge
    # tick, and close the cold-boot drbdmeta-spam window); idempotent.
    try:
        try:
            from lib import cluster_arbiter as _ca
        except ImportError:                       # source-tree layout
            from installer.lib import cluster_arbiter as _ca  # type: ignore
        await asyncio.to_thread(_ca.ensure_arbiter_drbd_up)
    except Exception as e:
        log.warning("services: arbiter DRBD up failed "
                    "(converge_retry will retry): %s", e)

    # Per-VM DRBD resources are ours to bring up: at boot nothing has run
    # `drbdadm up` yet (the units are disabled — quorum-aware boot), so
    # /dev/drbdN won't exist until we do it here, and libvirtd would
    # fail to open the VM's backing device.
    ours = [n for n, vm in vms.items()
            if vm.get("host") == self_name and vm.get("state") == "running"]
    for vm_name in ours:
        _bring_up_vm_drbd(vm_name)

    # Per-node SeaweedFS: weed-volume + weed-s3 run on EVERY node, and
    # weed-master on the Raft-3 set. Those units have `WantedBy=` empty
    # by design (role-aware, not blanket boot-enabled), so nothing else
    # restarts them after a reboot/power-loss. Without this call a cold
    # boot leaves S3/object storage down on this node (the .254 filer is
    # separately owned by cluster_arbiter). promote_to_master_volume_host
    # is idempotent and role-aware (handles the master set + reset-failed).
    try:
        try:
            from lib import seaweedfs as _sw
        except ImportError:                       # source-tree layout
            from installer.lib import seaweedfs as _sw  # type: ignore
        log.info("services: starting per-node SeaweedFS "
                 "(weed-volume + weed-s3, + weed-master if in the Raft-3 set)")
        _sw.promote_to_master_volume_host()
    except Exception as e:
        log.warning("services: seaweedfs start failed "
                    "(will retry on next role change): %s", e)

    # Re-mount Bedrock-managed SMB/NFS storage endpoints. The two mountpoints
    # (/mnt/bedrock/{kopia,witness}/<id>) hold no systemd units, so nothing else
    # restores them after a reboot; a kopia backup target or a fileshare witness
    # whose share isn't mounted simply can't function. Reconcile from the view:
    # mount every endpoint a backup_target (kopia) or witness (witness) refers to,
    # unmount any stale one. Best-effort + per-endpoint isolated — a share that
    # won't mount is logged, never blocks boot (the witness/backup just stays
    # down until its share returns, which the slot protocol already tolerates).
    try:
        from lib import storage_mount as _sm
        from lib import bedrock_state as _bs
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _sm.reconcile_from_cluster(
                cluster,
                unseal_password=lambda eid: _bs.storage_endpoint_secret(
                    eid, "fs_password"),
                log=lambda m: log.warning("services: %s", m)),
        )
    except Exception as e:
        log.warning("services: storage-endpoint mount reconcile failed "
                    "(will retry on next role change/boot): %s", e)

    # libvirtd only after the DRBD devices its VMs need are up.
    log.info("services: starting libvirtd")
    subprocess.run(["systemctl", "start", "libvirtd"], check=False)
    await asyncio.sleep(2)

    # Start VMs the log says belong here.
    for vm_name in ours:
        log.info("services: virsh start %s", vm_name)
        subprocess.run(["virsh", "start", vm_name], check=False)

    # Reconcile backup targets. Reactor only runs on NEW log entries
    # while the node is up; entries seen during catch-up don't trigger
    # `_react_backup_target_set` because _SERVICES_STARTED is still
    # False then. So at boot we walk the materialised view and connect
    # each target idempotently. `kopia repository connect` is a no-op
    # if we're already connected.
    for target_id, t in (cluster.get("backup_targets") or {}).items():
        if t.get("is_mirror"):
            # Mirror targets are sync-to destinations only — never connected
            # independently (the first sync-to from the primary creates them).
            continue
        log.info("services: reconciling backup target %s", target_id)
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            import backup as bedrock_backup  # type: ignore
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda tid=target_id, t=t: bedrock_backup.configure_target_locally(
                    target_id=tid, kind=t.get("kind", "kopia-s3"),
                    s3_endpoint=t.get("s3_endpoint", ""),
                    s3_bucket=t.get("s3_bucket", ""),
                    s3_region=t.get("s3_region", ""),
                    s3_disable_tls=bool(t.get("s3_disable_tls", False)),
                    s3_disable_tls_verification=bool(t.get("s3_disable_tls_verification", False)),
                    filesystem_path=t.get("filesystem_path", ""),
                    override_source_prefix=t.get("override_source_prefix", ""),
                    cache_directory=t.get("cache_directory", ""),
                ),
            )
        except Exception as e:
            log.warning("services: backup target %s reconcile failed: %s",
                        target_id, e)


# ── ③ no_quorum_responder ───────────────────────────────────────────────

async def no_quorum_responder():
    """Watch /run/bedrock-no-quorum. On appearance, run cleanup
    (pause VMs) within NO_QUORUM_CLEANUP_TIMEOUT_S. Then poll for the
    election to leave NoQuorum (rqlite has a recorded mgmt_master)
    BEFORE clearing the marker — otherwise the election re-flags
    NoQuorum on the next tick and we flap.

    If this task crashes mid-cleanup or rejoin never completes, the
    marker stays present and the operator can troubleshoot (no
    auto-reboot)."""
    global _SERVICES_STARTED
    while True:
        await asyncio.sleep(1)
        if not NO_QUORUM_MARKER.exists():
            continue

        log.error("no_quorum: marker detected — entering cleanup")
        try:
            await asyncio.wait_for(
                _run_no_quorum_cleanup(),
                timeout=NO_QUORUM_CLEANUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error("no_quorum: cleanup did not complete in %ds — "
                      "will retry next tick", int(NO_QUORUM_CLEANUP_TIMEOUT_S))
            continue
        except Exception as e:
            log.error("no_quorum: cleanup raised %r — will retry next tick", e)
            continue

        # Wait for the election to leave NoQuorum before unlinking the
        # marker — otherwise election would re-create it on the next
        # tick and we'd flap. _wait_for_role returns 'leader'/'follower'
        # once rqlite has a recorded mgmt_master (i.e. the cluster has
        # reformed quorum and a leader has been elected).
        log.info("no_quorum: cleanup done; waiting for quorum to return "
                 "before clearing marker")
        role = await _wait_for_role(timeout_s=120.0, ignore_marker=True)
        if role not in ("leader", "follower"):
            # Long partitions are normal (peers may need to cold-boot,
            # or the cluster may just not have reformed yet). DO NOT
            # `return` — that would terminate this task and leave the
            # marker and the VM paused indefinitely. Sleep briefly and
            # loop back so the next iteration polls again.
            log.warning("no_quorum: still no quorum after 120s — will "
                        "retry next tick")
            await asyncio.sleep(10)
            continue

        try:
            NO_QUORUM_MARKER.unlink()
            log.info("no_quorum: quorum back as %s; marker cleared", role)
        except FileNotFoundError:
            pass

        _SERVICES_STARTED = False
        if role in ("leader", "follower"):
            await _reconcile_paused_vms()
            await _start_local_services()
            _SERVICES_STARTED = True
        else:
            log.warning("no_quorum: post-recovery role=%r — services "
                        "held until cluster recovers", role)


async def _run_no_quorum_cleanup():
    """The minimum required to make this node not-dangerous to peers:
    virsh suspend every running VM (preserve state).

    We do NOT demote DRBD here — qemu's open file descriptors on the
    DRBD device would EBUSY. The demote-when-needed happens in
    _reconcile_paused_vms after we destroy stale paused copies, which
    closes those FDs.

    libvirtd is left running; the daemon itself isn't dangerous, the
    writes were."""
    running = _running_vm_names()
    for vm in running:
        log.info("no_quorum: virsh suspend %s", vm)
        subprocess.run(["virsh", "suspend", vm], check=False)

    log.info("no_quorum: cleanup complete (paused %d VMs)", len(running))


async def _reconcile_paused_vms():
    """After quorum returns + log catch-up, decide for each paused VM:

      - log says VM moved (host != us) or destroyed → virsh destroy
        the local stale copy (qemu releases the DRBD FD), then
        drbdadm secondary that resource so DRBD's reconnect resyncs
        from the peer's primary.

      - log still has us as home + state == "running" → virsh resume.

    This is the moment "did failover happen?" gets resolved against
    the replicated log. The log is by construction up-to-date by the
    time _wait_for_role returns a settled role (subscriber catches
    us up over the just-restored network)."""
    self_name = _self_node_name()
    try:
        from lib import cluster_state as _cs
        # Recovery-path decisions (resume VM here vs. destroy stale
        # copy because a peer has taken over) must be made against
        # the authoritative state, not the local replica which is
        # still catching up from being partitioned. Force a Raft
        # round-trip.
        cluster = _cs.load_cluster(level="strong")
    except Exception:
        cluster = {}
    vms = cluster.get("vms", {}) or {}

    # VMs we resume here must be dropped from the vm_failover suspended
    # record so the 5-min kill timer doesn't destroy a VM that recovered
    # inside the window.
    resumed: list[str] = []

    for vm_name in _paused_vm_names():
        vm = vms.get(vm_name)
        res = _vm_drbd_resource(vm_name)

        if vm is None:
            log.warning("recover: paused VM %s not in log — destroying stale copy",
                        vm_name)
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            if res:
                log.info("recover: drbdadm secondary %s "
                         "(releasing for peer's primary)", res)
                subprocess.run(["drbdadm", "secondary", res], check=False)
            continue

        if vm.get("host") != self_name:
            log.info("recover: VM %s now hosted on %s — destroying our paused copy",
                     vm_name, vm.get("host"))
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            if res:
                log.info("recover: drbdadm secondary %s "
                         "(releasing for peer's primary)", res)
                subprocess.run(["drbdadm", "secondary", res], check=False)
            continue

        if vm.get("state") == "running":
            log.info("recover: VM %s still ours per log — resuming", vm_name)
            subprocess.run(["virsh", "resume", vm_name], check=False)
            resumed.append(vm_name)

    if resumed:
        try:
            from bedrock_d.orchestrator import vm_failover as _vmf
            _vmf.drop_suspended(resumed)
        except Exception as e:
            log.warning("recover: could not clear suspended record for "
                        "resumed VMs %s: %s", resumed, e)


# ── ④ reactor — snapshot-diff driven ─────────────────────────────────────

async def _reactor_diff(prev: dict, cur: dict, self_name: str):
    """Run side-effects derived from prev→cur snapshot transitions.
    Under rqlite there are no log entries to dispatch on, just
    before/after snapshots; the diff surfaces the transitions:

      - VMs that disappeared from cur.vms → virsh destroy + undefine
      - VMs whose host changed → start/destroy locally as appropriate
      - backup_targets that appeared → kopia repository connect

    Only active after boot_orchestrator has started services; the
    boot path picks up any state that the reactor skipped while
    services were still starting.
    """
    if not _SERVICES_STARTED:
        return

    prev_vms = prev.get("vms") or {}
    cur_vms = cur.get("vms") or {}

    # VMs that disappeared (destroyed) — clean up local copy.
    for name in prev_vms.keys() - cur_vms.keys():
        log.info("reactor: vm %s destroyed — virsh destroy + undefine", name)
        subprocess.run(["virsh", "destroy", name], check=False)
        subprocess.run(["virsh", "undefine", name], check=False)

    # VMs whose host changed (migrated).
    for name in prev_vms.keys() & cur_vms.keys():
        prev_host = (prev_vms[name] or {}).get("host", "")
        cur_host = (cur_vms[name] or {}).get("host", "")
        if prev_host == cur_host:
            continue
        if cur_host == self_name:
            log.info("reactor: vm %s migrated TO us — virsh start", name)
            subprocess.run(["virsh", "start", name], check=False)
        elif prev_host == self_name:
            log.info("reactor: vm %s migrated AWAY (now on %s) — "
                     "destroying local copy", name, cur_host)
            subprocess.run(["virsh", "destroy", name], check=False)

    # Critical-tier DRBD master transitions are owned by
    # cluster_arbiter.converge(), called from the rqlite subscriber
    # every revision tick. Nothing to do here.

    # backup_targets that appeared (or had their config refreshed).
    prev_targets = prev.get("backup_targets") or {}
    cur_targets = cur.get("backup_targets") or {}
    for tid, target in cur_targets.items():
        if prev_targets.get(tid) == target:
            continue
        await _react_backup_target_set(tid, target)


async def _react_backup_target_set(target_id: str, target: dict):
    """A backup target appeared or was reconfigured. Run `kopia
    repository connect` locally so subsequent backup/restore
    invocations work.

    No-op on missing credentials/key file — operator is expected to
    drop /etc/bedrock/backup.key and
    /etc/bedrock/backup-credentials/<target_id>.env onto every node
    before issuing the target-set."""
    if not target_id:
        return
    if target.get("is_mirror"):
        # A mirror target is a sync-to destination only — never independently
        # connected/created (that gives it an incompatible format block). The
        # first `kopia repository sync-to` from its primary creates it.
        return
    try:
        sys.path.insert(0, "/usr/local/lib/bedrock")
        sys.path.insert(0, str(Path(__file__).parent))
        import backup as bedrock_backup  # type: ignore
    except Exception as e:
        log.warning("reactor: cannot import mgmt/backup.py: %s", e)
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: bedrock_backup.configure_target_locally(
                target_id=target_id, kind=target.get("kind", "kopia-s3"),
                s3_endpoint=target.get("s3_endpoint", ""),
                s3_bucket=target.get("s3_bucket", ""),
                s3_region=target.get("s3_region", ""),
                s3_disable_tls=bool(target.get("s3_disable_tls", False)),
                s3_disable_tls_verification=bool(
                    target.get("s3_disable_tls_verification", False)),
                filesystem_path=target.get("filesystem_path", ""),
                override_source_prefix=target.get("override_source_prefix", ""),
                cache_directory=target.get("cache_directory", ""),
            ),
        )
        log.info("reactor: kopia repository connected (target=%s, kind=%s)",
                 target_id, target.get("kind", "kopia-s3"))
    except Exception as e:
        log.warning("reactor: kopia connect for target=%s failed: %s",
                    target_id, e)


# ── ⑤ backup_scheduler ───────────────────────────────────────────────────

# In-memory ledger of in-flight scheduled backups so a slow `run_backup`
# doesn't get re-queued every minute by the next tick. Cleared when the
# task finishes (success or failure). Only meaningful on the leader.
_SCHEDULED_INFLIGHT: set[str] = set()


async def backup_scheduler():
    """Master-only loop. Every 60 s, walks every VM's `backup_schedule`
    from cluster state (rqlite) and decides whether to fire a backup.
    Decision uses bedrock-managed mtime — `cron.should_fire_now`
    compares the cron expression against last-fired (most recent
    BACKUP_DONE for the same VM + target_id) and a 60-min grace window
    for first-time fires.

    Why master-only: writing the backup result to cluster state (and,
    more importantly, actually orchestrating the LV snapshot + dd |
    kopia stream) must not double-fire. The leader is the single writer
    of cluster state, so scheduling against the leader's view is
    naturally serialised. A follower running this loop would duplicate
    the work, so short-circuit on role.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    log.info("scheduler: starting (master-only loop)")
    while True:
        try:
            await asyncio.sleep(60)
            if not _is_leader():
                continue
            await _scheduler_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("scheduler: tick failed: %s", e)


def _is_leader() -> bool:
    """True iff rqlite says we are mgmt_master (level='none', works
    without quorum — falls back to False if local rqlite is down)."""
    try:
        with rqlite_client.RqliteClient() as _rc:
            row = _rc.query_one(
                "SELECT mgmt_master FROM cluster_info WHERE id = 1",
                level="none",
            )
        master = (row or {}).get("mgmt_master") or ""
    except Exception:
        return False
    return master == _self_node_name()


async def _scheduler_tick():
    """Single pass: load cluster state from local rqlite, evaluate every
    VM's schedule, queue run_backup for the ones that are due."""
    try:
        from lib import cluster_state as _cs
        cluster = _cs.load_cluster()
    except Exception as e:
        log.warning("scheduler: cluster_state load failed: %s", e)
        return

    import datetime as dt
    import cron as bedrock_cron  # type: ignore
    now = dt.datetime.utcnow()

    vms = cluster.get("vms") or {}
    targets = cluster.get("backup_targets") or {}

    for vm_name, vm in vms.items():
        sched = vm.get("backup_schedule")
        if not sched:
            continue
        target_id = sched.get("target_id") or ""
        cron_expr = sched.get("cron_expr") or ""
        if not target_id or not cron_expr:
            continue
        if target_id not in targets:
            log.warning("scheduler: VM %s scheduled to non-existent target %r",
                        vm_name, target_id)
            continue
        if vm_name in _SCHEDULED_INFLIGHT:
            continue  # previous run hasn't finished — skip this tick

        # last_fired_at: most recent BACKUP_DONE for this VM + target.
        # The cluster log doesn't carry wall-clock — only ts_index.
        # Approximate "last fired wall-clock" by mapping ts_index → minutes
        # ago: each new entry roughly corresponds to one operator action,
        # which doesn't give us minutes. Better: store the wall-clock the
        # scheduler fired AT in cluster-log via a SCHEDULE_FIRED entry,
        # OR rely on the fact that we only need MONOTONIC ordering
        # ("did we fire AFTER this cron tick?"), not absolute wall-clock.
        #
        # Pragmatic v1: don't try to fire missed catch-up windows from
        # before mgmt-startup. Use the master's process-start time as a
        # floor on "last_fired_at" if no BACKUP_DONE has happened yet
        # since startup. This avoids firing every scheduled VM on master
        # restart (which would happen if we passed last_fired_at=None to
        # should_fire_now and the cron has fired in the last hour).
        last_fired_at = _last_scheduled_fire_time(vm, sched, now)

        try:
            should_fire = bedrock_cron.should_fire_now(
                cron_expr, now=now, last_fired_at=last_fired_at,
                grace_minutes=60,
            )
        except bedrock_cron.CronError as e:
            log.warning("scheduler: invalid cron %r for %s: %s",
                        cron_expr, vm_name, e)
            continue

        if not should_fire:
            continue

        log.info("scheduler: firing scheduled backup for %s (target=%s, cron=%r, "
                 "last_fired=%s)",
                 vm_name, target_id, cron_expr, last_fired_at)
        _SCHEDULED_INFLIGHT.add(vm_name)
        asyncio.create_task(_run_scheduled_backup(vm_name, target_id, sched))


def _last_scheduled_fire_time(vm: dict, sched: dict,
                              now) -> "dt.datetime | None":
    """Best-effort wall-clock of the most recent scheduled fire. We
    reconstruct it from the VM's backup records (vm.backups) that carry
    the schedule's label_prefix — those are the auto-generated labels of
    the form "<prefix>-YYYYMMDDTHHMMSS" written by run_backup when
    invoked from the scheduler. Returns None if we've never fired one."""
    import datetime as dt
    prefix = sched.get("label_prefix") or "auto"
    target_id = sched.get("target_id") or ""
    backups = vm.get("backups") or []
    for b in backups:  # newest-first
        if b.get("target_id") != target_id:
            continue
        lbl = b.get("label") or ""
        if not lbl.startswith(prefix + "-"):
            continue
        ts_part = lbl[len(prefix) + 1:]
        try:
            return dt.datetime.strptime(ts_part, "%Y%m%dT%H%M%S")
        except ValueError:
            continue
    return None


async def _run_scheduled_backup(vm_name: str, target_id: str, sched: dict):
    import datetime as dt
    label = f"{sched.get('label_prefix') or 'auto'}-{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
    try:
        # Submit the SAME vm_backup saga the API uses (target_node=home), so the
        # backup AND the kopia sync-to mirror both run ON THE VM'S HOME NODE
        # where the primary repo lives. The scheduler runs on the master, which
        # is frequently NOT the VM's home; running the sync-to here would mirror
        # the master's (empty/divergent) local repo for a kopia-fs primary.
        from lib import cluster_state as _cs
        cluster = _cs.load_cluster()
        vm = (cluster.get("vms") or {}).get(vm_name) or {}
        home = vm.get("host") or ""
        if not home:
            log.warning("scheduler: VM %s has no home node recorded — skipping "
                        "scheduled backup", vm_name)
            return
        tgt = (cluster.get("backup_targets") or {}).get(target_id) or {}
        secondaries = list(tgt.get("sync_to") or [])

        import socket as _socket
        from bedrock_d.orchestrator.sagas import SagaExecutor
        from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
        from bedrock_d import state as _bst
        from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_backup
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_backup", target_node=home,
            params={"target_id": target_id, "vm_name": vm_name, "label": label,
                    "secondary_target_ids": secondaries},
            requested_by="backup_scheduler")
        log.info("scheduler: submitted vm_backup saga for %s (op %d, runs on %s,"
                 " %d mirror target(s))", vm_name, op_id, home, len(secondaries))
        # Hold _SCHEDULED_INFLIGHT until the home node's operations_drain runs
        # it (bounded poll), so the "skip if a prior run is in flight" guard in
        # _scheduler_tick stays accurate and we log the real outcome.
        for _ in range(900):       # ~15 min ceiling
            await asyncio.sleep(1)
            try:
                op = await asyncio.to_thread(backend.get_operation, op_id)
            except Exception:
                continue
            st = (op or {}).get("state", "")
            if st in ("completed", "failed"):
                if st == "failed":
                    log.warning("scheduler: backup of %s (op %d) FAILED: %s",
                                vm_name, op_id, (op or {}).get("error", ""))
                else:
                    log.info("scheduler: backup of %s (op %d) done (label=%s)",
                             vm_name, op_id, label)
                break
    except Exception as e:
        log.warning("scheduler: backup of %s failed to submit/run: %s",
                    vm_name, e)
    finally:
        _SCHEDULED_INFLIGHT.discard(vm_name)


# ── public registration ──────────────────────────────────────────────────

async def cluster_tier_watcher():
    """Calm-loop watcher: when this node is the mgmt-master, cluster
    size is ≥ 2, and the critical tier is still in ``mode='local'``,
    submit the ``cluster_tier_promote_master`` saga once.

    Only fires while the local node is the cluster's mgmt-master —
    the saga MUST run on the node currently holding ``.254`` (it
    needs the live filer/leveldb3 data on disk to snapshot before
    promoting the LV under it). Re-checks every 10 s.

    Submits the saga at most once per "needs promotion" cycle by
    keeping a set of in-flight op_ids; if the saga fails the
    operator can retry via ``POST /api/operations``.
    """
    self_name = _self_node_name()
    while not self_name:
        await asyncio.sleep(1)
        self_name = _self_node_name()
    submitted_for: set[str] = set()
    while True:
        await asyncio.sleep(10)
        try:
            try:
                from lib import cluster_state as _cs
                cluster = _cs.load_cluster()
            except Exception:
                continue
            nodes = cluster.get("nodes") or {}
            master = cluster.get("mgmt_master") or ""
            if master != self_name:
                continue                       # only the master promotes
            if len(nodes) < 2:
                continue                       # still N=1
            tier_mode = ((cluster.get("tiers") or {})
                         .get("cluster") or {}).get("mode", "local")
            if tier_mode == "drbd":
                continue                       # already done
            # Pick the first peer (lowest-octet) that isn't us. The
            # promote saga's first peer becomes the initial DRBD
            # secondary; further peers are added by a separate saga.
            peer_name = next(
                (n for n in sorted(nodes) if n != self_name), "")
            if not peer_name:
                continue
            peer_loopback = (nodes.get(peer_name) or {}).get("loopback_ip", "")
            if not peer_loopback:
                continue
            key = f"{peer_name}@{peer_loopback}"
            if key in submitted_for:
                continue                       # already submitted
            log.info(
                "cluster_tier_watcher: cluster reached N=%d, "
                "cluster-tier=local, master=self — submitting "
                "cluster_tier_promote_master(peer=%s)",
                len(nodes), peer_name)
            try:
                from bedrock_d.orchestrator.sagas import SagaExecutor
                from bedrock_d.orchestrator.sagas.rqlite_backend import (
                    RqliteSagaBackend,
                )
                from bedrock_d import state as _st
                # Importing registers the saga in SAGAS.
                from bedrock_d.install import cluster_tier  # noqa: F401
                backend = RqliteSagaBackend(_st.RqliteClient())
                ex = SagaExecutor(backend=backend, this_node=self_name)
                op_id = ex.submit(
                    kind="cluster_tier_promote_master",
                    target_node=self_name,
                    params={"peer_node": peer_name,
                            "peer_loopback": peer_loopback},
                    requested_by="cluster_tier_watcher",
                )
                # Run synchronously in a thread so we don't have two
                # promotes racing if the watcher fires twice.
                await asyncio.to_thread(ex.execute_one, op_id)
                submitted_for.add(key)
                log.info(
                    "cluster_tier_watcher: promote saga op=%d submitted",
                    op_id)
            except Exception as e:
                log.warning(
                    "cluster_tier_watcher: submit failed: %s "
                    "(will retry next tick)", e)
        except Exception as e:
            log.warning("cluster_tier_watcher: tick failed: %s", e)


async def converge_retry():
    """Periodically re-run cluster_arbiter.converge(). The rqlite
    subscriber already runs converge on every revision advance, but
    a failed promote during failover (e.g. DRBD primary refused
    because the isolated old master hasn't self-demoted yet) needs a
    timer-based retry: nothing in rqlite advances when we're just
    waiting for the OLD master to drop its DRBD-primary."""
    self_name = _self_node_name()
    # Wait until cluster.json has settled enough to have a role.
    while not _self_node_name():
        await asyncio.sleep(1)
    self_name = _self_node_name()
    try:
        try:
            from installer.lib import cluster_arbiter as _ca  # type: ignore
        except ImportError:
            sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import cluster_arbiter as _ca  # type: ignore
    except Exception as e:
        log.warning("converge_retry: import cluster_arbiter failed: %s", e)
        return
    while True:
        try:
            await asyncio.to_thread(_ca.converge)
        except Exception as e:
            log.warning("converge_retry: tick failed: %s", e)
        await asyncio.sleep(5)


# Cluster-tier sagas mutate the singleton DRBD topology and the .254
# arbiter — they may only run on the node currently holding the master
# role. The boot resume sweep skips them unless this node is the leader,
# so a half-finished promote isn't resumed on the wrong node.
_LEADER_ONLY_SAGA_KINDS = frozenset({
    "cluster_tier_promote_master",
    "cluster_tier_join_peer",
    "cluster_rename",
    "replica_repair",   # self-heal: mutates singleton/DRBD topology on the master
})


async def saga_resume():
    """Resume in-flight runtime sagas once at bedrock-d boot. A crash
    mid vm_create / cluster_tier_promote / cluster_rename leaves an
    ``in_progress`` operations row that nothing else picks back up —
    this delivers the "power-loss recoverable on boot" guarantee for
    the rqlite-backed runtime sagas.

    Gating:
      - run only after rqlite is reachable (we can read inflight rows),
        and after this node has a settled role (quorum reformed);
      - leader-only saga kinds (cluster-tier / rename) are skipped
        unless this node is the current mgmt-master, so a half-finished
        promote resumes on the .254 holder, not a follower.

    The install bootstrap sagas (cluster_init/node_join/node_leave) use
    a separate FileSagaBackend and resume via their own install path —
    they are deliberately out of scope here."""
    role = await _wait_for_role(timeout_s=120.0)
    if role in ("noquorum", "", "unknown"):
        log.info("saga_resume: role=%r at boot — skipping resume sweep "
                 "(no settled quorum; runtime sagas resume on the node "
                 "that owns them once quorum is back)", role)
        return
    self_name = _self_node_name()
    am_leader = role == "leader"
    try:
        from bedrock_d.orchestrator.sagas import SagaExecutor
        from bedrock_d.orchestrator.sagas.rqlite_backend import (
            RqliteSagaBackend,
        )
        from bedrock_d import state as _st
        # Importing registers every runtime saga kind in SAGAS so the
        # executor can match an inflight row's `kind`.
        from bedrock_d.vm import create, destroy, grow, migrate  # noqa: F401
        from bedrock_d.install import cluster_tier  # noqa: F401
        from bedrock_d.cluster import rename as _rename  # noqa: F401
        from bedrock_d.orchestrator import replica_repair  # noqa: F401
        backend = RqliteSagaBackend(_st.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=self_name)
    except Exception as e:
        log.warning("saga_resume: executor init failed: %s", e)
        return

    try:
        inflight = backend.list_inflight_for(self_name)
    except Exception as e:
        log.warning("saga_resume: could not list inflight ops: %s", e)
        return

    resumed = 0
    for op in inflight:
        kind = op.get("kind", "")
        if kind in _LEADER_ONLY_SAGA_KINDS and not am_leader:
            log.info("saga_resume: skipping op=%s kind=%s — leader-only "
                     "and this node is a follower", op.get("id"), kind)
            continue
        log.warning("saga_resume: resuming in-flight op=%s kind=%s "
                    "(state=%s)", op.get("id"), kind, op.get("state"))
        try:
            res = await asyncio.to_thread(ex.execute_one, op["id"])
            resumed += 1
            log.info("saga_resume: op=%s finished state=%s last_step=%s",
                     op.get("id"), res.state.value, res.last_step)
        except Exception as e:
            log.warning("saga_resume: op=%s raised %r", op.get("id"), e)
    log.info("saga_resume: resume sweep done (%d of %d inflight ops "
             "resumed on this node)", resumed, len(inflight))


_OPS_DRAIN_INTERVAL_S = 3.0
# Saga kinds dispatched to a specific node via `operations.target_node`
# and run by THAT node's operations_drain — NOT synchronously by the
# submitter. rqlite is the channel: the mgmt master submits with
# target_node=<the VM's home node>, and the home node runs the saga
# locally (no SSH). Every other saga kind runs synchronously on its
# submitter (vm_create, cluster_tier_promote, …) and is left alone here.
_NODE_DISPATCHED_KINDS = {"vm_backup", "vm_restore"}


async def operations_drain():
    """Per-node loop: execute rqlite `operations` rows targeted at this
    node for the node-dispatched saga kinds (vm_backup / vm_restore).

    This is what lets a backup or restore run on the VM's home node
    without the master SSHing in: the master writes the operation row,
    this loop on the home node picks it up (target_node match) and runs
    the saga locally, recording the result back to rqlite."""
    log.info("operations_drain: started (kinds=%s, floor %.0fs + event wake)",
             sorted(_NODE_DISPATCHED_KINDS), _OPS_DRAIN_INTERVAL_S)
    global _OPS_WAKE
    if _OPS_WAKE is None:
        _OPS_WAKE = asyncio.Event()
    while True:
        # Wait for the central loop to signal a state change (a saga op may have
        # just been queued for us) OR the poll floor — whichever comes first.
        # The floor is the correctness backstop; the wake is the fast path so a
        # backup/restore starts ~now instead of up to _OPS_DRAIN_INTERVAL_S late.
        try:
            await asyncio.wait_for(_OPS_WAKE.wait(),
                                   timeout=_OPS_DRAIN_INTERVAL_S)
        except asyncio.TimeoutError:
            pass  # poll floor fired
        _OPS_WAKE.clear()
        if NO_QUORUM_MARKER.exists():
            continue   # need rqlite to read the queue and record results
        try:
            self_name = _self_node_name()
            if not self_name:
                continue
            from bedrock_d.orchestrator.sagas import SagaExecutor
            from bedrock_d.orchestrator.sagas.rqlite_backend import (
                RqliteSagaBackend,
            )
            from bedrock_d import state as _st
            from bedrock_d.vm import backup as _bk  # noqa: F401  registers sagas
            backend = RqliteSagaBackend(_st.RqliteClient())
            ex = SagaExecutor(backend=backend, this_node=self_name)
            todo = [op for op in backend.list_inflight_for(self_name)
                    if op.get("kind") in _NODE_DISPATCHED_KINDS]
        except Exception as e:
            log.debug("operations_drain: list failed: %s", e)
            continue
        for op in todo:
            try:
                res = await asyncio.to_thread(ex.execute_one, op["id"])
                log.info("operations_drain: op=%s kind=%s -> %s",
                         op.get("id"), op.get("kind"), res.state.value)
            except Exception as e:
                log.warning("operations_drain: op=%s raised %r",
                            op.get("id"), e)


_TASKS_STARTED: bool = False
import threading as _t
_START_LOCK = _t.Lock()


def supervise(name: str, coro_fn, *, restart: bool = True):
    """Wrap a fire-and-forget orchestrator task so its death is LOUD.

    A bare asyncio.create_task() whose coroutine raises just vanishes
    (asyncio's 'Task exception was never retrieved' is easily missed) —
    the cluster keeps running with that whole subsystem silently dead and
    no alarm. That is the silent-killer class behind the obs outage.

      restart=True  (long-lived loops): on crash, log CRITICAL + full
                    traceback, back off (cap 60s), re-run. The loop is
                    meant to run forever, so keep it alive LOUDLY rather
                    than kill the daemon on a transient.
      restart=False (one-shots): clean return -> stop. On crash -> log
                    CRITICAL + traceback and stop. We deliberately do NOT
                    os._exit: these one-shots can raise on legitimate
                    timeouts (e.g. _wait_for_role) and killing the whole
                    daemon would be the WRONG escalation. The CRITICAL log
                    is the loud, obvious signal; a task that CRITICAL-loops
                    every backoff IS the alarm.

    Returns the coroutine to hand to asyncio.create_task()."""
    async def _run():
        backoff = 1.0
        while True:
            try:
                await coro_fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.critical("orchestrator task %r CRASHED", name,
                             exc_info=True)
                if not restart:
                    return
            else:
                if not restart:
                    return
                log.critical("orchestrator task %r returned unexpectedly "
                             "(a loop should run forever) — restarting", name)
            await asyncio.sleep(min(backoff, 60.0))
            backoff = min(backoff * 2.0, 60.0)
    return _run()


def start_all():
    """Spawn the orchestrator's tasks on the running event loop. Called
    from FastAPI's startup hook in mgmt/app.py. Under the unified
    bedrock-d daemon, the FastAPI startup hook fires once per uvicorn
    instance — and we run TWO uvicorns (8443 HTTPS + 8001 loopback) in
    SEPARATE threads — so without a real lock the idempotency guard
    races and every orchestrator task starts twice (visible as doubled
    `arbiter: promoting` / `boot: role=leader` log lines, two competing
    rqlite_subscribers, and two no_quorum_responders that clobber
    each other's wait_for_role timing)."""
    global _TASKS_STARTED, _MAIN_LOOP, _CLUSTER_SRC, _OPS_WAKE
    with _START_LOCK:
        if _TASKS_STARTED:
            log.info("orchestrator: start_all already invoked (second "
                     "FastAPI startup hook, dual-uvicorn) — skipping")
            return
        _TASKS_STARTED = True
    # operations_drain's wake event — created here on the running loop so the
    # central loop can nudge it from the very first revision (no lazy race).
    _OPS_WAKE = asyncio.Event()
    # Capture the running loop so worker-thread code (_apply_revision runs
    # in the rqlite_subscriber executor) can schedule tasks on it via
    # call_soon_threadsafe — asyncio.get_event_loop() raises in a worker
    # thread, which is what silently killed the reactor_diff.
    _MAIN_LOOP = asyncio.get_running_loop()
    # Deploy the DRBD fence-peer handler on EVERY node — not just arbiter hosts.
    # Per-VM disks (fencing resource-and-stonith) reference it from their .res and
    # can run on ANY node, including one OUTSIDE the 3-node arbiter set whose
    # arbiter path (cluster_arbiter._enforce_drbd_safety_options) never fires. A
    # missing handler -> DRBD leaves IO frozen on peer loss (safe but stuck).
    # Idempotent; the arbiter path also deploys it. See lib/fence_verdict.py.
    try:
        try:
            from lib import fence_verdict as _fv
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, "/usr/local/lib/bedrock")
            from lib import fence_verdict as _fv  # type: ignore
        _fv.deploy_handler()
    except Exception as e:
        log.warning("orchestrator: fence-peer handler deploy failed: %s", e)
    # All wrapped in supervise() so a crash is LOUD (CRITICAL + traceback)
    # instead of a silently-vanished task. Loops restart-with-backoff;
    # the two one-shots (boot_orchestrator, saga_resume) log-critical and
    # stop on failure.
    # The ONE central cluster-state loop (replaces rqlite_subscriber):
    # detects revision advances (master-first read, local fallback, CDC-
    # wakeable) and fans out to _cluster_change → the reactors.
    from bedrock_d.orchestrator.cluster_loop import ClusterStateSource
    _CLUSTER_SRC = ClusterStateSource(_cluster_change)
    asyncio.create_task(supervise("cluster_loop", _CLUSTER_SRC.run))
    asyncio.create_task(supervise("no_quorum_responder", no_quorum_responder))
    asyncio.create_task(supervise("boot_orchestrator", boot_orchestrator, restart=False))
    asyncio.create_task(supervise("backup_scheduler", backup_scheduler))
    asyncio.create_task(supervise("converge_retry", converge_retry))
    asyncio.create_task(supervise("cluster_tier_watcher", cluster_tier_watcher))
    asyncio.create_task(supervise("saga_resume", saga_resume, restart=False))
    asyncio.create_task(supervise("operations_drain", operations_drain))
    # Self-heal: leader-only calm loop that rebuilds redundancy after a
    # permanent host loss, one resource at a time, under the 80% disk
    # gate.
    try:
        from bedrock_d.orchestrator import self_heal as _sh
        asyncio.create_task(supervise("self_heal", _sh.self_heal_task))
    except Exception as e:
        log.warning("orchestrator: self_heal start failed: %s", e)
    # VM failover state machine — suspend-on-no-quorum,
    # takeover-after-35s, kill-suspended-after-5min. Per-VM workload
    # survival logic.
    try:
        from bedrock_d.orchestrator import vm_failover as _vmf
        _vmf.start_failover_tasks()
    except Exception as e:
        log.warning("orchestrator: vm_failover start failed: %s", e)
    log.info("orchestrator: tasks started (subscriber, no_quorum_responder, "
             "boot, backup_scheduler, converge_retry, "
             "cluster_tier_watcher, saga_resume, self_heal, vm_failover x3)")
