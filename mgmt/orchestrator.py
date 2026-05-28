"""Bedrock management-plane orchestrator (rqlite edition).

Per docs/post-alpha-rewrite-notes.md D-01..D-22, cluster state lives
in rqlite. This orchestrator runs as a set of asyncio tasks inside
the bedrock-mgmt FastAPI process — one Python process per node,
hosting:

  ① rqlite_subscriber    poll bedrock_meta.revision; on advance,
                          rebuild snapshot from rqlite, project to
                          cluster.json + state.json, run the
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
# This is the cap on the cleanup procedure only; the operator can
# troubleshoot a stuck cleanup in alpha/beta (no auto-reboot).
NO_QUORUM_CLEANUP_TIMEOUT_S = 30.0

# Live in-memory snapshot, updated by rqlite_subscriber and read by
# other tasks (and by the FastAPI handlers that want fresh state).
#
# Unification status: when running under bedrock-d (the unified
# daemon), these globals are bound to fields on the shared
# state_shared.BedrockState object by orchestrator.attach_state().
# Backwards-compat: standalone bedrock-mgmt still uses the module
# globals directly. The accessor helpers below abstract over both.
_SNAPSHOT: dict = view_builder.empty_snapshot()
# bedrock_meta.revision of the last snapshot we observed.
# Field name retained as _LAST_LOG_IDX for back-compat with existing
# external readers; semantically it's now the rqlite revision.
_LAST_LOG_IDX: int = 0
# Previous snapshot — kept so the reactor can diff prev→cur to drive
# transition handling (vm_destroyed, vm_migrated, etc.) the same way
# the old log-replay reactor did on per-entry events.
_PREV_SNAPSHOT: dict = view_builder.empty_snapshot()
_SERVICES_STARTED: bool = False

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


# ── ① rqlite_subscriber ──────────────────────────────────────────────────

async def rqlite_subscriber():
    """Watch the rqlite cluster-state store's revision counter; on
    every advance, rebuild the snapshot, project to cluster.json +
    state.json, run the reactor on the snapshot diff.

    Poll-based (per rqlite's HTTP semantics) at ~500ms cadence; the
    reactor sees one consolidated revision-advance per tick even if
    many mutations landed within it.
    """
    self_name = _self_node_name()
    log.info("rqlite_subscriber: starting (node=%r)", self_name)
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, _subscriber_pass, self_name)
        except Exception as e:
            log.warning("rqlite_subscriber: rqlite unreachable: %s", e)
        await asyncio.sleep(2)


def _subscriber_pass(self_name: str) -> None:
    """One subscriber lifecycle: poll bedrock_meta.revision and on
    each advance, refresh state. Called from a thread executor (the
    rqlite client is sync)."""
    global _LAST_LOG_IDX
    since = _STATE.last_log_idx if _STATE is not None else _LAST_LOG_IDX
    with rqlite_client.RqliteClient() as rc:
        for rev in rc.watch(since_revision=since, interval_s=0.5):
            _apply_revision(rc, rev, self_name)


def _apply_revision(rc: rqlite_client.RqliteClient, revision: int,
                    self_name: str) -> None:
    """Refresh the in-memory snapshot from rqlite, project to disk,
    queue a reactor task."""
    global _LAST_LOG_IDX, _PREV_SNAPSHOT, _SNAPSHOT
    if _STATE is not None:
        with _STATE.snapshot_lock:
            prev = copy.deepcopy(_STATE.snapshot)
            new = view_builder.build_snapshot(client=rc)
            _STATE.snapshot = new
            _STATE.prev_snapshot = prev
            _STATE.last_log_idx = int(new.get("log_index", revision))
        # Keep module globals in lockstep so legacy callers (anything
        # still doing `from orchestrator import _SNAPSHOT`) see the
        # same data. Cheap, ~few μs per tick.
        _SNAPSHOT = new
        _PREV_SNAPSHOT = prev
        _LAST_LOG_IDX = _STATE.last_log_idx
    else:
        prev = copy.deepcopy(_SNAPSHOT)
        new = view_builder.build_snapshot(client=rc)
        _SNAPSHOT.clear()
        _SNAPSHOT.update(new)
        _LAST_LOG_IDX = int(new.get("log_index", revision))

    # state.json projection of this node's role + mgmt_url. cluster.json
    # is no longer written — consumers query rqlite directly via
    # cluster_state.load_cluster() (level='none'). state.json holds the
    # per-node fields that need to survive cold-boot without rqlite:
    # node identity + the derived role + master URL for that node.
    try:
        if self_name and self_name in (_SNAPSHOT.get("nodes") or {}):
            existing = {}
            if STATE_JSON.exists():
                try:
                    existing = json.loads(STATE_JSON.read_text())
                except Exception:
                    pass
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

    # Arbiter mobility (D-04..D-08): converge based on whether this
    # node currently holds the mgmt master role. promote/demote is
    # idempotent so this is safe on every revision tick.
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
    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(_reactor_diff(prev, _SNAPSHOT, self_name))
        )
    except Exception:
        pass
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
    """Poll cluster.json until we have a quorum-recorded mgmt_master.
    Returns 'leader' if we are master, 'follower' if a peer is, or
    'noquorum' / 'unknown' on timeout. cluster.json is written by the
    rqlite subscriber every tick from the canonical rqlite state, so
    this becomes 'follower' or 'leader' as soon as quorum is back.

    `ignore_marker=True`: skip the NO_QUORUM_MARKER early-return. Used
    by no_quorum_responder — the caller will clear the marker once
    quorum is back.

    Quorum-is-back signal: at least one peer is mesh-reachable AND
    cluster.json says master is alive (someone, possibly us). We do
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

    # The `cluster` singleton (arbiter rqlite + filer/s3 + .254 VIP +
    # its DRBD primary/mount) is owned by cluster_arbiter.converge(),
    # driven from the rqlite subscriber + converge_retry. Per-VM DRBD
    # resources are ours to bring up: at boot nothing has run `drbdadm
    # up` yet (the units are disabled — quorum-aware boot, finding I-02),
    # so /dev/drbdN won't exist until we do it here, and libvirtd would
    # fail to open the VM's backing device.
    ours = [n for n, vm in vms.items()
            if vm.get("host") == self_name and vm.get("state") == "running"]
    for vm_name in ours:
        _bring_up_vm_drbd(vm_name)

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
    election to leave NoQuorum (cluster.json's mgmt_master becomes
    reachable from rqlite) BEFORE clearing the marker — otherwise the
    election re-flags NoQuorum on the next tick and we flap.

    If this task crashes mid-cleanup or rejoin never completes, the
    marker stays present and the operator can troubleshoot in
    alpha/beta (no auto-reboot)."""
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
        # once cluster.json has a recorded mgmt_master from rqlite (i.e.
        # the cluster has reformed quorum and a leader has been elected).
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
    # inside the window (VM-01).
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
    Replaces the old per-log-entry reactor — under rqlite there are
    no entries to dispatch on, just before/after snapshots. The diff
    surfaces the same transitions the old reactor reacted to:

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
    in cluster.json and decides whether to fire a backup. Decision
    uses bedrock-managed mtime — `cron.should_fire_now` compares the
    cron expression against last-fired (most recent BACKUP_DONE for
    the same VM + target_id) and a 60-min grace window for first-
    time fires.

    Why master-only: appending log entries (and, more importantly,
    actually orchestrating the LV snapshot + dd | kopia stream) must
    not double-fire. The leader is the single writer of the cluster
    log, so scheduling against the leader's view is naturally
    serialised.

    A follower running this loop would either (a) duplicate the work
    or (b) discover its IPC append fails (only the leader can append).
    Cleaner to short-circuit on role.
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
    reconstruct it from BACKUP_DONE entries that carry the schedule's
    label_prefix — those are the auto-generated labels of the form
    "<prefix>-YYYYMMDDTHHMMSS" written by run_backup when invoked from
    the scheduler. Returns None if we've never fired one."""
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
        sys.path.insert(0, str(Path(__file__).parent))
        import backup as bedrock_backup  # type: ignore
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: bedrock_backup.run_backup(target_id, vm_name, label=label),
        )
        log.info("scheduler: backup of %s done (label=%s)", vm_name, label)
    except Exception as e:
        log.warning("scheduler: backup of %s failed: %s", vm_name, e)
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
    """BAD-6 / SA-02: resume in-flight runtime sagas once at bedrock-d
    boot. A crash mid vm_create / cluster_tier_promote / cluster_rename
    leaves an ``in_progress`` operations row that nothing else picks
    back up — this delivers BEDROCK.md's "power-loss recoverable on
    boot" guarantee for the rqlite-backed runtime sagas.

    Gating, per the locked plan:
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


_TASKS_STARTED: bool = False
import threading as _t
_START_LOCK = _t.Lock()


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
    global _TASKS_STARTED
    with _START_LOCK:
        if _TASKS_STARTED:
            log.info("orchestrator: start_all already invoked (second "
                     "FastAPI startup hook, dual-uvicorn) — skipping")
            return
        _TASKS_STARTED = True
    asyncio.create_task(rqlite_subscriber())
    asyncio.create_task(no_quorum_responder())
    asyncio.create_task(boot_orchestrator())
    asyncio.create_task(backup_scheduler())
    asyncio.create_task(converge_retry())
    asyncio.create_task(cluster_tier_watcher())
    asyncio.create_task(saga_resume())
    # Self-heal: leader-only calm loop that rebuilds redundancy after a
    # permanent host loss (SG-05), one resource at a time, under the 80%
    # disk gate.
    try:
        from bedrock_d.orchestrator import self_heal as _sh
        asyncio.create_task(_sh.self_heal_task())
    except Exception as e:
        log.warning("orchestrator: self_heal start failed: %s", e)
    # VM failover state machine — suspend-on-no-quorum,
    # takeover-after-35s, kill-suspended-after-5min. Per-VM workload
    # survival logic (Gap 2 from 2026-05-26 review).
    try:
        from bedrock_d.orchestrator import vm_failover as _vmf
        _vmf.start_failover_tasks()
    except Exception as e:
        log.warning("orchestrator: vm_failover start failed: %s", e)
    log.info("orchestrator: tasks started (subscriber, no_quorum_responder, "
             "boot, backup_scheduler, converge_retry, "
             "cluster_tier_watcher, saga_resume, self_heal, vm_failover x3)")
