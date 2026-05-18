"""Bedrock management-plane orchestrator (rqlite edition).

Per docs/post-alpha-rewrite-notes.md D-01..D-22, cluster state lives
in rqlite. This orchestrator runs as a set of asyncio tasks inside
the bedrock-mgmt FastAPI process — one Python process per node,
hosting:

  ① rqlite_subscriber    poll bedrock_meta.revision; on advance,
                          rebuild snapshot from rqlite, project to
                          cluster.json + state.json, run the
                          snapshot-diff reactor.
  ② boot_orchestrator    on startup: wait for clear cluster role,
                          then start libvirtd + VMs that belong here.
                          Critical-tier DRBD primary/mount/.254 VIP/
                          arbiter rqlite/filer are owned by
                          cluster_arbiter.converge().
  ③ fence_responder      on fence marker (dropped by bedrock-net's
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

The 5-min fence-to-reboot watchdog lives outside this process, in
/usr/local/bin/bedrock-fence-watchdog (a systemd timer). Its job is
to reboot the node if the marker stays around > 5 min, independent
of whether mgmt is alive — covers the "mgmt itself crashed during
cleanup" case.
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
FENCE_MARKER = Path("/run/bedrock-cluster.fence")

# Cleanup itself is fast — virsh suspend on local VMs is seconds.
# This is the cap on the cleanup procedure only; the broader 5-min
# fence-to-reboot cap is the independent watchdog timer.
FENCE_CLEANUP_TIMEOUT_S = 30.0

# Live in-memory snapshot, updated by rqlite_subscriber and read by
# other tasks (and by the FastAPI handlers that want fresh state).
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


# ── helpers ──────────────────────────────────────────────────────────────

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


def get_snapshot() -> dict:
    """Read-only access to the live snapshot for FastAPI handlers."""
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
    with rqlite_client.RqliteClient() as rc:
        for rev in rc.watch(since_revision=_LAST_LOG_IDX, interval_s=0.5):
            _apply_revision(rc, rev, self_name)


def _apply_revision(rc: rqlite_client.RqliteClient, revision: int,
                    self_name: str) -> None:
    """Refresh the in-memory snapshot from rqlite, project to disk,
    queue a reactor task."""
    global _LAST_LOG_IDX, _PREV_SNAPSHOT
    prev = copy.deepcopy(_SNAPSHOT)
    new = view_builder.build_snapshot(client=rc)
    _SNAPSHOT.clear()
    _SNAPSHOT.update(new)
    _LAST_LOG_IDX = int(new.get("log_index", revision))

    try:
        CLUSTER_JSON.parent.mkdir(parents=True, exist_ok=True)
        view_builder._atomic_write_json(
            CLUSTER_JSON, view_builder._cluster_view(_SNAPSHOT))
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
        log.warning("rqlite_subscriber: projection write at rev %d: %s",
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
    if role in ("noquorum", "fenced", "", "unknown"):
        log.error("boot: role=%r — not starting local services; "
                  "fence_responder or future state changes will trigger start",
                  role)
        return
    log.info("boot: role=%s; starting local services", role)
    await _start_local_services()
    _SERVICES_STARTED = True


async def _wait_for_role(timeout_s: float) -> str:
    """Poll cluster.json until we have a quorum-recorded mgmt_master.
    Returns 'leader' if we are master, 'follower' if a peer is, or
    'fenced' / 'unknown' on timeout. cluster.json is written by the
    rqlite subscriber every tick from the canonical rqlite state, so
    this becomes 'follower' or 'leader' as soon as quorum is back."""
    deadline = time.monotonic() + timeout_s
    self_name = _self_node_name()
    while time.monotonic() < deadline:
        if FENCE_MARKER.exists():
            return "fenced"
        try:
            cluster = json.loads(CLUSTER_JSON.read_text())
        except (OSError, ValueError):
            cluster = {}
        master = cluster.get("mgmt_master") or ""
        if master:
            return "leader" if master == self_name else "follower"
        await asyncio.sleep(1)
    return "unknown"


async def _start_local_services():
    """Bring this node's local services up to the state cluster.json
    says they should be in. Idempotent — safe at boot, after a fence
    cycle, or when re-running because the log changed."""
    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    nodes = cluster.get("nodes", {}) or {}
    vms = cluster.get("vms", {}) or {}
    self_name = _self_node_name()

    # DRBD primary/secondary + mount + arbiter rqlite + .254 VIP for the
    # critical tier is owned by cluster_arbiter.converge(), driven from
    # the rqlite subscriber on every revision tick. Nothing to do here.

    log.info("services: starting libvirtd")
    subprocess.run(["systemctl", "start", "libvirtd"], check=False)
    await asyncio.sleep(2)

    # Start VMs the log says belong here.
    for vm_name, vm in vms.items():
        if vm.get("host") == self_name and vm.get("state") == "running":
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


# ── ③ fence_responder ────────────────────────────────────────────────────

async def fence_responder():
    """Watch /run/bedrock-cluster.fence. On appearance, run cleanup
    (pause VMs) within FENCE_CLEANUP_TIMEOUT_S. Then poll for the
    election to leave NoQuorum (cluster.json's mgmt_master becomes
    reachable from rqlite) BEFORE clearing the marker — otherwise the
    election re-flags NoQuorum on the next tick and we flap.

    The independent bedrock-fence-watchdog timer reboots the node if
    the marker stays around > 5 min — that covers the case where this
    task itself crashes mid-cleanup or rejoin never completes."""
    global _SERVICES_STARTED
    while True:
        await asyncio.sleep(1)
        if not FENCE_MARKER.exists():
            continue

        log.error("fence: marker detected — entering cleanup")
        try:
            await asyncio.wait_for(
                _run_fence_cleanup(),
                timeout=FENCE_CLEANUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error("fence: cleanup did not complete in %ds — leaving "
                      "marker for the watchdog to reboot us",
                      int(FENCE_CLEANUP_TIMEOUT_S))
            return
        except Exception as e:
            log.error("fence: cleanup raised %r — leaving marker for "
                      "the watchdog", e)
            return

        # Wait for the election to leave NoQuorum before unlinking the
        # marker — otherwise election would re-create it on the next
        # tick and we'd flap. _wait_for_role returns 'leader'/'follower'
        # once cluster.json has a recorded mgmt_master from rqlite (i.e.
        # the cluster has reformed quorum and a leader has been elected).
        log.info("fence: cleanup done; waiting for quorum to return before "
                 "clearing marker")
        role = await _wait_for_role(timeout_s=120.0)
        if role not in ("leader", "follower"):
            log.warning("fence: still no quorum after 120s — leaving marker "
                        "for watchdog reboot")
            return

        try:
            FENCE_MARKER.unlink()
            log.info("fence: quorum back as %s; marker cleared", role)
        except FileNotFoundError:
            pass

        # Cluster contact re-evaluates. Role is already known (the
        # wait above only returns on a settled role), so reconcile +
        # restart services without waiting again.
        _SERVICES_STARTED = False
        if role in ("leader", "follower"):
            await _reconcile_paused_vms()
            await _start_local_services()
            _SERVICES_STARTED = True
        else:
            log.warning("fence: post-unfence role=%r — services held until "
                        "cluster recovers (watchdog will reboot if this "
                        "stays past 5 min total since fence)", role)


async def _run_fence_cleanup():
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
        log.info("fence: virsh suspend %s", vm)
        subprocess.run(["virsh", "suspend", vm], check=False)

    log.info("fence: cleanup complete (paused %d VMs)", len(running))


async def _reconcile_paused_vms():
    """After unfence + log catch-up, decide for each paused VM:

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
    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    vms = cluster.get("vms", {}) or {}

    for vm_name in _paused_vm_names():
        vm = vms.get(vm_name)
        res = _vm_drbd_resource(vm_name)

        if vm is None:
            log.warning("unfence: paused VM %s not in log — destroying stale copy",
                        vm_name)
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            if res:
                log.info("unfence: drbdadm secondary %s "
                         "(releasing for peer's primary)", res)
                subprocess.run(["drbdadm", "secondary", res], check=False)
            continue

        if vm.get("host") != self_name:
            log.info("unfence: VM %s now hosted on %s — destroying our paused copy",
                     vm_name, vm.get("host"))
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            if res:
                log.info("unfence: drbdadm secondary %s "
                         "(releasing for peer's primary)", res)
                subprocess.run(["drbdadm", "secondary", res], check=False)
            continue

        if vm.get("state") == "running":
            log.info("unfence: VM %s still ours per log — resuming", vm_name)
            subprocess.run(["virsh", "resume", vm_name], check=False)


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
    """True iff cluster.json says we are mgmt_master."""
    try:
        master = json.loads(CLUSTER_JSON.read_text()).get("mgmt_master") or ""
    except (OSError, ValueError):
        return False
    return master == _self_node_name()


async def _scheduler_tick():
    """Single pass: load cluster.json, evaluate every VM's schedule,
    queue run_backup for the ones that are due."""
    if not CLUSTER_JSON.exists():
        return
    try:
        cluster = json.loads(CLUSTER_JSON.read_text())
    except Exception as e:
        log.warning("scheduler: cluster.json read failed: %s", e)
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


def start_all():
    """Spawn the orchestrator's tasks on the running event loop. Called
    from FastAPI's startup hook in mgmt/app.py."""
    asyncio.create_task(rqlite_subscriber())
    asyncio.create_task(fence_responder())
    asyncio.create_task(boot_orchestrator())
    asyncio.create_task(backup_scheduler())
    asyncio.create_task(converge_retry())
    log.info("orchestrator: tasks started (subscriber, fence_responder, "
             "boot, backup_scheduler, converge_retry)")
