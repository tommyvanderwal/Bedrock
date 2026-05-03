"""Bedrock management-plane orchestrator.

Replaces the standalone bedrock-watcher process. Runs as a set of
asyncio tasks inside the bedrock-mgmt FastAPI process — one Python
process per node, hosting:

  ① log_subscriber       fold log → cluster.json + state.json + daemon.toml
                          regen + restart bedrock-rust on toml change
  ② boot_orchestrator    on startup: wait for clear cluster role, then
                          start drbd / libvirtd / VMs that belong here
  ③ fence_responder      on fence marker: pause VMs + stop NFS exports;
                          unfence (interfaces up, marker cleared);
                          re-run boot orchestrator
  ④ reactor              react to log entries that affect this node
                          (vm_migrated, vm_destroyed, …) once services
                          are up

Single source of role truth: /run/bedrock-rust.role, written by the
Rust daemon on every election change ("leader" / "follower" /
"noquorum" / "fenced").
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

# Watchdog: if mgmt's cleanup hasn't completed within this many seconds,
# fall back to systemctl reboot — the universal cleanup. Should never
# fire in a healthy cluster.
FENCE_CLEANUP_TIMEOUT_S = 270.0

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
    """Names of VMs currently in `running` state. Empty list if libvirtd
    is down — that's fine, nothing to pause anyway."""
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


def get_snapshot() -> dict:
    """Read-only access to the live snapshot for FastAPI handlers."""
    return _SNAPSHOT


# ── ① log_subscriber ──────────────────────────────────────────────────────

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

    # Catch-up phase: fold any entries the subscriber missed.
    with rust_ipc.Daemon() as d:
        cur = d.status()["latest_index"]
        if cur > _LAST_LOG_IDX:
            log.info("subscriber: catching up %d..%d", _LAST_LOG_IDX + 1, cur)
            for entry in d.read(from_index=_LAST_LOG_IDX + 1, to=cur):
                last_toml_hash = _apply_entry(entry, self_name, last_toml_hash)

    # Subscribe phase: drain pushed entries until the connection ends
    # or a gap is detected (which forces us back to catch-up).
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

    # Persist cluster.json + state.json — every node ends up with the
    # same projection because fold_into is deterministic.
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

    # daemon.toml is a deterministic projection too; only restart
    # bedrock-rust if its config actually changed (membership/witness/
    # maintenance flag drove it).
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

    # Schedule a reactor reaction. Fire-and-forget; reactor itself
    # is a no-op if services aren't started yet.
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
    """Start DRBD (if needed), libvirtd, then VMs the log says belong
    here. Idempotent — safe to call after a fence-recovery or repeated
    starts."""
    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    tiers = cluster.get("tiers", {}) or {}
    needs_drbd = any(t.get("mode") == "drbd-nfs" for t in tiers.values())

    if needs_drbd:
        log.info("services: starting drbd")
        subprocess.run(["systemctl", "start", "drbd"], check=False)
        # Best-effort: wait briefly for resources to connect; not fatal
        # if a peer isn't reachable yet.
        for tier in ("bulk", "critical"):
            subprocess.run(
                ["drbdadm", "wait-connect-resource", f"tier-{tier}"],
                timeout=20, check=False
            )

    log.info("services: starting libvirtd")
    subprocess.run(["systemctl", "start", "libvirtd"], check=False)
    await asyncio.sleep(2)

    self_name = _self_node_name()
    vms = cluster.get("vms", {}) or {}
    for vm_name, vm in vms.items():
        if vm.get("host") == self_name and vm.get("state") == "running":
            log.info("services: virsh start %s", vm_name)
            subprocess.run(["virsh", "start", vm_name], check=False)


# ── ③ fence_responder ────────────────────────────────────────────────────

async def fence_responder():
    """Watch /tmp/bedrock-rust.fence. On appearance, run the cleanup
    procedure (pause VMs, stop NFS exports) within FENCE_CLEANUP_TIMEOUT_S,
    then bring interfaces back up + clear the marker. Reboot is the
    safety net — only fires if the cleanup itself hangs or raises."""
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
            log.error("fence: cleanup did not complete in %ds — reboot fallback",
                      int(FENCE_CLEANUP_TIMEOUT_S))
            subprocess.run(["systemctl", "reboot"])
            return
        except Exception as e:
            log.error("fence: cleanup raised %r — reboot fallback", e)
            subprocess.run(["systemctl", "reboot"])
            return

        # Cleanup succeeded → unfence: bring interfaces back up, then
        # remove the marker so bedrock-rust resumes its normal loop.
        for iface in _fence_interfaces():
            log.info("fence: ip link set %s up (unfence)", iface)
            subprocess.run(["ip", "link", "set", iface, "up"], check=False)
        try:
            FENCE_MARKER.unlink()
            log.info("fence: marker cleared — bedrock-rust will resume election")
        except FileNotFoundError:
            pass

        # We've been off the network — services and any paused VMs
        # need re-evaluation against whatever the cluster decided
        # while we were dark.
        _SERVICES_STARTED = False
        role = await _wait_for_role(timeout_s=120.0)
        if role in ("leader", "follower"):
            await _reconcile_paused_vms()
            await _start_local_services()
            _SERVICES_STARTED = True
        else:
            log.warning("fence: post-unfence role=%r — services held until cluster recovers",
                        role)


async def _run_fence_cleanup():
    """Minimum required to make this node not-dangerous to peers:
       1. Pause every running VM (suspend; preserve state).
       2. Stop NFS exports so peers stop hammering our dead server.
    DRBD is left primary-but-quiet (paused VMs aren't writing); on
    reconnect, DRBD's protocol-C quorum logic resolves who's primary.
    libvirtd is left running (it doesn't endanger anyone)."""
    running = _running_vm_names()
    for vm in running:
        log.info("fence: virsh suspend %s", vm)
        subprocess.run(["virsh", "suspend", vm], check=False)

    log.info("fence: exportfs -au (drop all NFS exports)")
    subprocess.run(["exportfs", "-au"], check=False)

    log.info("fence: cleanup complete (paused %d VMs)", len(running))


async def _reconcile_paused_vms():
    """After unfence: each paused VM's correct state is determined by
    the (now-current) replicated log. If the log says we're the home
    and it should be running → resume. If the log moved it elsewhere
    while we were dark → destroy our stale paused copy."""
    self_name = _self_node_name()
    cluster = json.loads(CLUSTER_JSON.read_text()) if CLUSTER_JSON.exists() else {}
    vms = cluster.get("vms", {}) or {}

    for vm_name in _paused_vm_names():
        vm = vms.get(vm_name)
        if vm is None:
            log.warning("fence: paused VM %s not in log — destroying stale copy",
                        vm_name)
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            continue
        if vm.get("host") != self_name:
            log.info("fence: VM %s now hosted on %s — destroying our paused copy",
                     vm_name, vm.get("host"))
            subprocess.run(["virsh", "destroy", vm_name], check=False)
            continue
        if vm.get("state") == "running":
            log.info("fence: VM %s still ours per log — resuming", vm_name)
            subprocess.run(["virsh", "resume", vm_name], check=False)


# ── ④ reactor ────────────────────────────────────────────────────────────

async def _reactor(entry: dict, self_name: str):
    """Per-entry reactions to log changes that affect this node's VMs.
    Only active after boot_orchestrator has started services; otherwise
    it's a no-op (the boot path will catch up on missed work)."""
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


# ── public registration ──────────────────────────────────────────────────

def start_all():
    """Spawn the orchestrator's tasks on the running event loop. Called
    from FastAPI's startup hook in mgmt/app.py."""
    asyncio.create_task(log_subscriber())
    asyncio.create_task(fence_responder())
    asyncio.create_task(boot_orchestrator())
    log.info("orchestrator: tasks started (subscriber, fence_responder, boot)")
