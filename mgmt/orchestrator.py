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


# ── public registration ──────────────────────────────────────────────────

def start_all():
    """Spawn the orchestrator's tasks on the running event loop. Called
    from FastAPI's startup hook in mgmt/app.py."""
    asyncio.create_task(log_subscriber())
    asyncio.create_task(fence_responder())
    asyncio.create_task(boot_orchestrator())
    log.info("orchestrator: tasks started (subscriber, fence_responder, boot)")
