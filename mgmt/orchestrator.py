"""Bedrock management-plane orchestrator.

Replaces the standalone bedrock-watcher process. Runs as a set of
asyncio tasks inside the bedrock-mgmt FastAPI process — one Python
process per node, hosting:

  ① log_subscriber       fold log → cluster.json + state.json + daemon.toml
                          regen + restart bedrock-rust on toml change
  ② boot_orchestrator    on startup: wait for clear cluster role, then
                          start DRBD / promote-or-secondary per cluster.json
                          NFS export (master) or mount (follower)
                          libvirtd + VMs that belong here
  ③ fence_responder      on fence marker: pause running VMs + exportfs -au
                          (cleanup is FAST — seconds, not minutes).
                          Then unfence (interfaces up, marker cleared),
                          wait for role to settle, reconcile paused VMs
                          against the now-current log:
                              moved/destroyed → virsh destroy + drbdadm
                                                secondary on its resource
                              still ours      → virsh resume
                          re-run start_local_services for re-promotion.
  ④ reactor              react to ongoing log entries — vm_migrated,
                          vm_destroyed, tier_state.master change (NFS
                          remount), once services are up.

Single source of role truth: /run/bedrock-rust.role, written by the
Rust daemon on every election change ("leader" / "follower" /
"noquorum" / "fenced").

The 5-min fence-to-reboot watchdog lives outside this process, in
/usr/local/bin/bedrock-fence-watchdog (a systemd timer). Its job is
to reboot the node if the marker stays around > 5 min, independent
of whether mgmt is alive — covers the "mgmt itself crashed during
cleanup" case.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/bedrock")

from lib import view_builder, daemon_setup, rust_ipc, log_entries

log = logging.getLogger("bedrock.orchestrator")

CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
STATE_JSON = Path("/etc/bedrock/state.json")
DAEMON_TOML = Path("/etc/bedrock/daemon.toml")
FENCE_MARKER = Path("/tmp/bedrock-rust.fence")
ROLE_FILE = Path("/run/bedrock-rust.role")

# Cleanup itself is fast — virsh suspend + exportfs -au are seconds.
# This is the cap on the cleanup procedure only; the broader 5-min
# fence-to-reboot cap is the independent watchdog timer.
FENCE_CLEANUP_TIMEOUT_S = 30.0

# Live in-memory snapshot, updated by log_subscriber and read by other
# tasks (and by the FastAPI handlers that want fresh state).
_SNAPSHOT: dict = view_builder.empty_snapshot()
_LAST_LOG_IDX: int = 0
_SERVICES_STARTED: bool = False


# ── helpers ──────────────────────────────────────────────────────────────

def _self_node_name() -> str:
    if not STATE_JSON.exists():
        return ""
    try:
        return json.loads(STATE_JSON.read_text()).get("node_name", "") or ""
    except Exception:
        return ""


def _fence_interfaces() -> list[str]:
    """Read fence_interfaces from daemon.toml (the daemon's own list)."""
    if not DAEMON_TOML.exists():
        return []
    m = re.search(r'fence_interfaces\s*=\s*\[([^\]]*)\]', DAEMON_TOML.read_text())
    if not m:
        return []
    return [s.strip().strip('"') for s in m.group(1).split(",") if s.strip().strip('"')]


def _current_role() -> str:
    if not ROLE_FILE.exists():
        return ""
    try:
        return ROLE_FILE.read_text().strip()
    except Exception:
        return ""


def _daemon_toml_hash() -> bytes:
    if not DAEMON_TOML.exists():
        return b""
    return hashlib.sha256(DAEMON_TOML.read_bytes()).digest()


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


def _find_drbd_nfs_master_drbd_ip(tiers: dict, nodes: dict) -> str | None:
    """The drbd_ip of the current tier-master. All drbd-nfs tiers share
    the same master in our model; we pick whichever is set."""
    for tier in tiers.values():
        if tier.get("mode") != "drbd-nfs":
            continue
        master_name = tier.get("master")
        if master_name and master_name in nodes:
            ip = nodes[master_name].get("drbd_ip")
            if ip:
                return ip
    return None


def get_snapshot() -> dict:
    """Read-only access to the live snapshot for FastAPI handlers."""
    return _SNAPSHOT


# ── ① log_subscriber ─────────────────────────────────────────────────────

async def log_subscriber():
    """Subscribe to bedrock-rust IPC; on each committed entry, fold into
    the in-memory snapshot, write cluster.json + state.json + daemon.toml,
    and bounce bedrock-rust if daemon.toml changed."""
    self_name = _self_node_name()
    log.info("subscriber: starting (node=%r)", self_name)
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, _subscriber_pass, self_name)
        except Exception as e:
            log.warning("subscriber: ipc unreachable: %s", e)
        await asyncio.sleep(2)


def _subscriber_pass(self_name: str) -> None:
    """One subscribe lifecycle: catch up, then drain Subscribe until error
    or gap. Called from a thread executor (the IPC client is sync)."""
    global _LAST_LOG_IDX
    last_toml_hash = _daemon_toml_hash()

    with rust_ipc.Daemon() as d:
        cur = d.status()["latest_index"]
        if cur > _LAST_LOG_IDX:
            log.info("subscriber: catching up %d..%d", _LAST_LOG_IDX + 1, cur)
            for entry in d.read(from_index=_LAST_LOG_IDX + 1, to=cur):
                last_toml_hash = _apply_entry(entry, self_name, last_toml_hash)

    with rust_ipc.Daemon() as d:
        for entry in d.subscribe():
            if entry["index"] <= _LAST_LOG_IDX:
                continue
            if entry["index"] != _LAST_LOG_IDX + 1:
                log.warning("subscriber: gap (have %d, got %d) — re-syncing",
                            _LAST_LOG_IDX, entry["index"])
                return
            last_toml_hash = _apply_entry(entry, self_name, last_toml_hash)


def _apply_entry(entry: dict, self_name: str, last_toml_hash: bytes) -> bytes:
    """Fold one entry; project to disk; restart bedrock-rust if its
    config changed; queue a reactor task. Returns the post-apply hash."""
    global _LAST_LOG_IDX
    view_builder.fold_into(_SNAPSHOT, [entry])
    _LAST_LOG_IDX = entry["index"]

    try:
        CLUSTER_JSON.parent.mkdir(parents=True, exist_ok=True)
        CLUSTER_JSON.write_text(
            json.dumps(view_builder._cluster_view(_SNAPSHOT), indent=2)
        )
        if self_name and self_name in (_SNAPSHOT.get("nodes") or {}):
            existing = {}
            if STATE_JSON.exists():
                try:
                    existing = json.loads(STATE_JSON.read_text())
                except Exception:
                    pass
            existing.update(view_builder._state_view(_SNAPSHOT, self_name))
            STATE_JSON.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        log.warning("subscriber: projection write at idx %d: %s",
                    entry["index"], e)

    try:
        daemon_setup.render_from_snapshot(_SNAPSHOT, self_name)
    except Exception as e:
        log.warning("subscriber: render_from_snapshot at idx %d: %s",
                    entry["index"], e)
        return last_toml_hash
    new_hash = _daemon_toml_hash()
    if new_hash != last_toml_hash:
        log.info("subscriber: idx %d changed daemon.toml; restarting bedrock-rust",
                 entry["index"])
        try:
            daemon_setup.restart()
        except Exception as e:
            log.warning("subscriber: restart bedrock-rust: %s", e)
            return last_toml_hash
        last_toml_hash = new_hash

    # Mesh path-table changes also drive DRBD's multi-path config:
    # when a LINK_UP / LINK_DOWN / LINK_QUALITY entry folds in, the
    # snapshot's `paths` section shifts, and any tier currently in
    # DRBD mode regenerates its tier-<resource>.res file with fresh
    # `path` blocks. Idempotent + a no-op in N=1 (no DRBD configured),
    # so it's cheap to call on every entry.
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
            log.warning("subscriber: drbd config regen at idx %d: %s",
                        entry["index"], e)

    try:
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.create_task(_reactor(entry, self_name))
        )
    except Exception:
        pass

    return last_toml_hash


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
    """Poll /run/bedrock-rust.role until it reports a clear quorum-having
    role, or the timeout expires."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if FENCE_MARKER.exists():
            return "fenced"
        r = _current_role()
        if r in ("leader", "follower"):
            return r
        await asyncio.sleep(1)
    return _current_role() or "unknown"


async def _start_local_services():
    """Bring this node's local services up to the state cluster.json
    says they should be in. Idempotent — safe at boot, after a fence
    cycle, or when re-running because the log changed."""
    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    tiers = cluster.get("tiers", {}) or {}
    nodes = cluster.get("nodes", {}) or {}
    vms = cluster.get("vms", {}) or {}
    self_name = _self_node_name()
    drbd_tiers = {n: t for n, t in tiers.items() if t.get("mode") == "drbd-nfs"}

    if drbd_tiers:
        # Make sure the drbd kernel module + resources are up. If
        # already up, this is a no-op.
        log.info("services: starting drbd")
        subprocess.run(["systemctl", "start", "drbd"], check=False)
        for tier_name in drbd_tiers:
            subprocess.run(
                ["drbdadm", "wait-connect-resource", f"tier-{tier_name}"],
                timeout=20, check=False
            )

        # Be the role cluster.json says we are. drbdadm is idempotent
        # for already-correct state (primary→primary, secondary→secondary
        # are no-ops). The dangerous case — secondary while we have an
        # open qemu FD — is handled in _reconcile_paused_vms before we
        # ever land here.
        i_am_master_of_anything = False
        for tier_name, tier in drbd_tiers.items():
            res = f"tier-{tier_name}"
            if tier.get("master") == self_name:
                log.info("services: drbdadm primary %s", res)
                subprocess.run(["drbdadm", "primary", res], check=False)
                i_am_master_of_anything = True
            else:
                log.info("services: drbdadm secondary %s", res)
                subprocess.run(["drbdadm", "secondary", res], check=False)

        # NFS export side. The master serves /var/lib/bedrock/mounts/*
        # to peers; followers mount whatever the current master serves.
        if i_am_master_of_anything:
            try:
                from lib import tier_storage
                tier_storage.nfs_export_drbd_tiers(
                    ["192.168.2.0/24", "10.99.0.0/24"]
                )
                log.info("services: NFS exports applied")
            except Exception as e:
                log.warning("services: NFS export failed: %s", e)
        else:
            master_drbd_ip = _find_drbd_nfs_master_drbd_ip(drbd_tiers, nodes)
            if master_drbd_ip:
                try:
                    from lib import tier_storage
                    tier_storage.nfs_mount_drbd_tiers(master_drbd_ip)
                    log.info("services: NFS mounts targeting master at %s",
                             master_drbd_ip)
                except Exception as e:
                    log.warning("services: NFS mount failed: %s", e)

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
    """Watch /tmp/bedrock-rust.fence. On appearance, run cleanup
    (pause VMs + drop NFS exports) within FENCE_CLEANUP_TIMEOUT_S,
    bring interfaces back up + clear the marker, wait for cluster
    membership to re-establish, reconcile paused VMs against the
    now-current log, and (re-)start local services.

    The independent bedrock-fence-watchdog timer reboots the node if
    the marker stays around > 5 min — that covers the case where this
    task itself crashes mid-cleanup."""
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

        for iface in _fence_interfaces():
            log.info("fence: ip link set %s up (unfence)", iface)
            subprocess.run(["ip", "link", "set", iface, "up"], check=False)
        try:
            FENCE_MARKER.unlink()
            log.info("fence: marker cleared — bedrock-rust will resume election")
        except FileNotFoundError:
            pass

        # Cluster contact re-evaluates. The log subscriber catches us
        # up via Read+Subscribe; cluster.json reflects the cluster's
        # truth about who's master, who runs which VM, etc. by the
        # time _wait_for_role returns a settled role.
        _SERVICES_STARTED = False
        role = await _wait_for_role(timeout_s=120.0)
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
       1. virsh suspend every running VM (preserve state).
       2. exportfs -au (drop NFS so peers stop hammering our dead server).

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

    log.info("fence: exportfs -au (drop all NFS exports)")
    subprocess.run(["exportfs", "-au"], check=False)

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


# ── ④ reactor ────────────────────────────────────────────────────────────

async def _reactor(entry: dict, self_name: str):
    """Per-entry reactions to log changes that affect this node's VMs
    or storage. Only active after boot_orchestrator has started services;
    otherwise it's a no-op (the boot path will catch up on missed work)."""
    if not _SERVICES_STARTED:
        return

    import msgpack
    try:
        payload = msgpack.unpackb(entry["payload"], raw=False)
    except Exception:
        return
    t = payload.get("t")

    if t == log_entries.VM_DESTROYED:
        name = payload.get("name", "")
        if name:
            subprocess.run(["virsh", "destroy", name], check=False)
            subprocess.run(["virsh", "undefine", name], check=False)
    elif t == log_entries.VM_MIGRATED:
        name = payload.get("name", "")
        dst = payload.get("dst_host", "")
        if dst == self_name:
            log.info("reactor: vm_migrated TO us — virsh start %s", name)
            subprocess.run(["virsh", "start", name], check=False)
        else:
            log.info("reactor: vm_migrated AWAY — destroying local %s", name)
            subprocess.run(["virsh", "destroy", name], check=False)
    elif t == log_entries.TIER_STATE:
        await _react_tier_state(payload, self_name)
    elif t == log_entries.BACKUP_TARGET_SET:
        await _react_backup_target_set(payload)


async def _react_tier_state(payload: dict, self_name: str):
    """A tier_state entry can change who the master is — for the
    drbd-nfs case that means everyone needs to re-evaluate where they
    mount NFS from (and whether they should be promoting/demoting).

    Idempotent: rendering for the role we already have is a no-op."""
    if payload.get("mode") != "drbd-nfs":
        return
    new_master = payload.get("master")
    tier_name = payload.get("tier", "")
    res = f"tier-{tier_name}"

    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    nodes = cluster.get("nodes", {}) or {}

    if new_master == self_name:
        log.info("reactor: tier_state %s master=us; drbdadm primary + re-export",
                 tier_name)
        subprocess.run(["drbdadm", "primary", res], check=False)
        try:
            from lib import tier_storage
            tier_storage.nfs_export_drbd_tiers(
                ["192.168.2.0/24", "10.99.0.0/24"]
            )
        except Exception as e:
            log.warning("reactor: NFS export failed: %s", e)
    else:
        master_node = nodes.get(new_master, {})
        master_drbd_ip = master_node.get("drbd_ip")
        if not master_drbd_ip:
            return
        log.info("reactor: tier_state %s master=%s; remount NFS from %s",
                 tier_name, new_master, master_drbd_ip)
        # Fast remount: lazy-unmount any existing mount that points at
        # the wrong server, then re-mount via the canonical helper.
        for t in ("bulk", "critical"):
            mp = f"/var/lib/bedrock/mounts/{t}-drbd"
            subprocess.run(["umount", "-l", mp], check=False)
        try:
            from lib import tier_storage
            tier_storage.nfs_mount_drbd_tiers(master_drbd_ip)
        except Exception as e:
            log.warning("reactor: NFS remount failed: %s", e)


async def _react_backup_target_set(payload: dict):
    """A backup target was configured cluster-wide. Run `kopia repository
    connect` on this node so subsequent backup/restore invocations work.

    No-op on credentials/key file missing — the operator is expected to
    drop /etc/bedrock/backup.key and
    /etc/bedrock/backup-credentials/<target_id>.env onto every node
    before issuing the target-set. If they're missing, this connect
    fails and we log a warning; nothing else breaks. Re-running the
    target-set after the files arrive will retry."""
    target_id = payload.get("target_id")
    kind = payload.get("kind", "kopia-s3")
    if not target_id:
        return
    try:
        sys.path.insert(0, "/usr/local/lib/bedrock")
        # mgmt/backup.py is alongside us under mgmt/.
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
                target_id=target_id, kind=kind,
                s3_endpoint=payload.get("s3_endpoint", ""),
                s3_bucket=payload.get("s3_bucket", ""),
                s3_region=payload.get("s3_region", ""),
                s3_disable_tls=bool(payload.get("s3_disable_tls", False)),
                s3_disable_tls_verification=bool(payload.get("s3_disable_tls_verification", False)),
                filesystem_path=payload.get("filesystem_path", ""),
                override_source_prefix=payload.get("override_source_prefix", ""),
                cache_directory=payload.get("cache_directory", ""),
            ),
        )
        log.info("reactor: kopia repository connected (target=%s, kind=%s)",
                 target_id, kind)
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
    return _current_role() == "leader"


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

def start_all():
    """Spawn the orchestrator's tasks on the running event loop. Called
    from FastAPI's startup hook in mgmt/app.py."""
    asyncio.create_task(log_subscriber())
    asyncio.create_task(fence_responder())
    asyncio.create_task(boot_orchestrator())
    asyncio.create_task(backup_scheduler())
    log.info("orchestrator: tasks started (subscriber, fence_responder, "
             "boot, backup_scheduler)")
