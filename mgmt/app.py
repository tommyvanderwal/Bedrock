#!/usr/bin/env python3
"""Bedrock cluster management dashboard — FastAPI backend with WebSocket hub."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

import paramiko
import urllib.request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ws import hub
from tasks import registry as task_registry, Task

# Peer-auth + operator-auth + join-handshake — these modules live in the
# bedrock lib tree (installer-deployed) rather than mgmt's own source
# dir so installers and mgmt share the same code.
import sys as _sys_peerauth
_sys_peerauth.path.insert(0, "/usr/local/lib/bedrock")
from lib import peer_auth as _peer_auth        # noqa: E402
from lib import operator_auth as _op_auth      # noqa: E402
from lib import join_handshake as _join_hs     # noqa: E402
from lib import bedrock_state as _bs           # noqa: E402
from lib import rqlite_client as _rqlite       # noqa: E402
from lib import cluster_state as _cluster_state  # noqa: E402
from lib import event_log as _events            # noqa: E402
from dependencies import (  # noqa: E402
    require_peer, require_operator, require_operator_or_peer,
)
from common import (  # noqa: E402
    load_cluster, save_cluster, write_scrape_config, get_nodes,
    ssh_cmd, ssh_cmd_rc, _ssh_connect, _ssh_pool_drop,
    get_node_info, parse_drbd_status, get_witness_status,
    get_vm_drbd_resource, get_vm_disks, get_vm_vnc_port,
    build_cluster_state, build_physical_topology, _mgmt_node_name, push_log,
    load_inventory, save_inventory, _import_dir, _write_import_meta,
    _vm_host, _vm_get_settings,
)
import common as _common  # noqa: E402


# require_peer / require_operator / require_operator_or_peer now live in
# dependencies.py (imported above) so the routers share one implementation.

# (The /api/peer-test smoke endpoint is registered after `app = FastAPI()`,
# search for it below.)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bedrock")
# Silence per-request HTTP chatter: httpx/httpcore log EVERY rqlite call at INFO
# ("HTTP Request: POST .../db/query ... 200 OK"). On the central loop + netd that
# is a steady stream into journald (wakeups + disk) for zero diagnostic value —
# rqlite errors still surface via our own loggers. (RCA L56 follow-up.)
for _noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Config ──────────────────────────────────────────────────────────────────

CLUSTER_FILE = Path("/etc/bedrock/cluster.json")
import os as _os

# ── SSH helpers ─────────────────────────────────────────────────────────────
#
# Connection pool: paramiko `SSHClient` per host, reused across calls.
# Without this, every `ssh_cmd` opened a fresh TCP+kex+auth — at N=4
# nodes × every-3-second probe loop × 4 mgmt processes (master + 3
# followers) sshd's pre-auth queue filled up and dropped connections
# with "exceeded LoginGraceTime" penalty, manifesting as nodes
# flapping between Online/Offline on the dashboard. Caching reuses
# a single Transport per peer + opens new channels on demand, which
# is what paramiko is designed for.
import threading as _threading


# ── FastAPI app ─────────────────────────────────────────────────────────────

app = FastAPI(title="Bedrock Cluster Manager")

from routers import internal as _r_internal  # noqa: E402
app.include_router(_r_internal.router)
from routers import auth as _r_auth  # noqa: E402
app.include_router(_r_auth.router)
from routers import tasks as _r_tasks  # noqa: E402
app.include_router(_r_tasks.router)
from routers import cron as _r_cron  # noqa: E402
app.include_router(_r_cron.router)
from routers import observability as _r_observability  # noqa: E402
app.include_router(_r_observability.router)
from routers import cluster as _r_cluster  # noqa: E402
app.include_router(_r_cluster.router)
from routers import exports as _r_exports  # noqa: E402
app.include_router(_r_exports.router)
from routers import join as _r_join  # noqa: E402
app.include_router(_r_join.router)
from routers import imports as _r_imports  # noqa: E402
app.include_router(_r_imports.router)


# ── Auth middleware ─────────────────────────────────────────────────
# Every /api/* request must carry either:
#   - operator Bearer token (issued by /api/login), OR
#   - peer Ed25519 signature (`Authorization: Bedrock-Ed25519 ...`)
# Public-path allow-list covers discovery, login, the join handshake
# (joiner doesn't yet have credentials), and the static dashboard
# assets (the browser fetches HTML/JS/CSS before login).

from fastapi.responses import JSONResponse as _JSONResponse  # noqa: E402

_PUBLIC_PREFIXES = (
    "/_app/", "/favicon", "/static/", "/assets/",
)
_PUBLIC_EXACT = {
    "/", "/login", "/cluster-info", "/health",
    "/api/login",
    "/api/join/request", "/api/join/status",
}


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    for pfx in _PUBLIC_PREFIXES:
        if path.startswith(pfx):
            return True
    # SvelteKit routes that the browser may hit before login (the
    # static-adapter prerenders them). Treat all non-/api/ paths as
    # static-page-fetches → the route guard does the redirect to /login.
    if not path.startswith("/api/") and not path.startswith("/ws"):
        return True
    return False


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path
    if _is_public(path):
        return await call_next(request)

    # Loopback is the trusted local CLI on :8001 (bound 127.0.0.1 only).
    # Local root is already fully privileged, so the `bedrock` CLI's POSTs
    # carry no operator token. LAN requests (:8443) still require operator
    # or peer auth below. A spoofed-loopback source from a real NIC is
    # dropped by rp_filter/martian filtering, so this can't be reached
    # remotely.
    _ch = request.client.host if request.client else ""
    if _ch in ("127.0.0.1", "::1"):
        return await call_next(request)

    authz = request.headers.get("authorization", "")
    if authz.startswith("Bearer "):
        try:
            _op_auth.verify_token(authz[7:].strip())
            return await call_next(request)
        except ValueError as e:
            return _JSONResponse({"detail": f"operator auth: {e}"}, status_code=401)

    if authz.startswith(_peer_auth.SCHEME + " "):
        body = await request.body()
        # Restore body for the route handler. Without this, request.body()
        # in the handler hangs because the stream is already drained.

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive

        def _lookup(node_name: str):
            cluster = load_cluster()
            n = (cluster.get("nodes") or {}).get(node_name) or {}
            pk_hex = (n.get("bedrock_pubkey") or "").strip()
            try:
                return bytes.fromhex(pk_hex) if pk_hex else None
            except ValueError:
                return None
        try:
            _peer_auth.verify(
                authz, request.method,
                path + (("?" + request.url.query) if request.url.query else ""),
                body, _lookup)
            return await call_next(request)
        except ValueError as e:
            return _JSONResponse({"detail": f"peer auth: {e}"}, status_code=401)

    return _JSONResponse({"detail": "authentication required"}, status_code=401)


# In-memory cache of master's X25519 private halves, keyed by request_id.
# When operator approves, we look up the private key here, do ECDH +
# AEAD, then drop the private key. Lost on mgmt restart — joiners that
# polled before approval and saw their request go stale need to retry,
# which is correct UX for a security-critical handshake.
_MASTER_EPH_PRIV: dict[str, "X25519PrivateKey"] = {}   # noqa: F821

# ── WebSocket endpoint ──────────────────────────────────────────────────────

# Last-known cluster state. The state push loop fills it; /ws and /api/cluster
# serve from here instantly so the dashboard never waits on fresh SSH probes.
# _last_state WS cache now lives in common.py (shared with the cluster router)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # WebSockets bypass the HTTP middleware. Token comes via query param
    # because the browser WebSocket API can't set custom headers.
    token = ws.query_params.get("token", "")
    try:
        _op_auth.verify_token(token)
    except ValueError as e:
        await ws.close(code=1008, reason=f"auth: {e}")
        return
    await hub.connect(ws)
    # Push cached state immediately so the UI renders before the next refresh.
    await hub.send_to(ws, "cluster", _common.get_last_state())
    # The push loop only probes WHILE a client is connected, so the cache may be
    # stale after an idle period. Kick a fresh gather now (off the handler) so
    # this client sees current state promptly; the loop keeps it fresh after.
    asyncio.create_task(_gather_and_push())
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                channel = msg.get("channel", "")

                if channel == "rpc":
                    result = await handle_rpc(msg.get("method", ""), msg.get("params", {}))
                    await hub.send_to(ws, "rpc.response", {"id": msg.get("id"), "result": result})
            except Exception as e:
                await hub.send_to(ws, "rpc.response", {"id": msg.get("id", 0), "error": str(e)})
    except WebSocketDisconnect:
        hub.disconnect(ws)

async def handle_rpc(method: str, params: dict) -> dict:
    loop = asyncio.get_event_loop()
    if method == "vm.start":
        return await loop.run_in_executor(None, _vm_start, params["name"])
    elif method == "vm.shutdown":
        return await loop.run_in_executor(None, _vm_shutdown, params["name"])
    elif method == "vm.poweroff":
        return await loop.run_in_executor(None, _vm_poweroff, params["name"])
    elif method == "vm.migrate":
        return await loop.run_in_executor(
            None, lambda: api_vm_migrate(
                params["name"], MigrateRequest(target_node=params.get("target_node"))))
    raise ValueError(f"Unknown method: {method}")

# ── Background task: push cluster state every 3 seconds ────────────────────

async def _gather_and_push():
    """One expensive cluster gather (per-node SSH fanout) → cache → broadcast."""
    loop = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, build_cluster_state)
    _common.set_last_state(state)
    await hub.broadcast("cluster", state)


async def state_push_loop():
    while True:
        try:
            # Only probe the cluster while a dashboard is actually OPEN. With no
            # WebSocket client connected, build_cluster_state's per-node SSH +
            # `virsh list --all` fanout is pure waste — nobody is watching — and
            # it is the DRBD-modprobe fork storm of RCA L54. A connecting client
            # triggers an immediate fresh gather (see websocket_endpoint), and
            # this loop keeps it fresh only for as long as someone is looking.
            if hub.has_clients():
                await _gather_and_push()
        except Exception as e:
            log.error("State push error: %s", e)
        await asyncio.sleep(3)

_main_loop: Optional[asyncio.AbstractEventLoop] = None
_STARTUP_DONE: bool = False
_STARTUP_LOCK = threading.Lock()


@app.on_event("startup")
async def startup():
    global _main_loop, _STARTUP_DONE
    # Under bedrock-d we run TWO uvicorn instances (8443 HTTPS + 8001
    # loopback) in SEPARATE threads, each with its own event loop —
    # both call this hook on the same `app`. Without a real lock the
    # `if _STARTUP_DONE` check + assign races and both threads proceed.
    # That spawned two no_quorum_responder tasks in v22, which clobbered
    # the 120s wait_for_role (visible as "still no quorum after 120s"
    # appearing within 0 seconds of "no_quorum: cleanup done").
    with _STARTUP_LOCK:
        if _STARTUP_DONE:
            return
        _STARTUP_DONE = True
    _main_loop = asyncio.get_running_loop()
    _common.set_main_loop(_main_loop)
    # Seed from cluster state so the sidebar shows host names instantly.
    cfg = load_cluster()
    _common.set_last_state({
        "nodes": {n: {"name": n, "host": c.get("host", ""), "online": False,
                      "kernel": "", "uptime_since": "", "load": "",
                      "mem_total_mb": 0, "mem_used_mb": 0,
                      "all_vms": [], "running_vms": [], "drbd_raw": "",
                      "switches": {},
                      "cockpit_url": c.get("cockpit", f"https://{c.get('host', '')}:9090")}
                  for n, c in cfg.get("nodes", {}).items()},
        "vms": {},
        "witness": {"nodes": {}},
        "topology": {"switches": {}, "links": [], "node_count": 0,
                     "switch_count": 0, "link_count": 0,
                     "computed_at": 0.0},
    })
    task_registry().wire(_main_loop, hub.broadcast)
    asyncio.create_task(state_push_loop())
    write_scrape_config(cfg)

    # Boot the cluster-protocol orchestrator: log subscriber, boot
    # service-starter, no_quorum responder, reactor.
    #
    # Use sys.modules to share the SAME module instance as bedrock-d.
    # bedrock-d does `from mgmt import orchestrator` (creates
    # `mgmt.orchestrator`) and calls `orchestrator.attach_state(state)`
    # there. A plain `import orchestrator` here would create a SECOND
    # module object (because sys.path has /opt/bedrock/mgmt) with its
    # own _STATE = None — so no_quorum_responder's
    # state.last_election_outcome gate never fires, marker flapping
    # loop bites (observed v29–v31 5c regression: "no_quorum: quorum
    # back as leader; marker cleared" at 0 s after cleanup, repeats
    # every 3 s).
    import sys as _sys
    if "mgmt.orchestrator" in _sys.modules:
        orchestrator = _sys.modules["mgmt.orchestrator"]
    else:
        import orchestrator
    orchestrator.start_all()


class NodeRegister(BaseModel):
    name: str
    host: str
    role: str = "compute"
    pubkey: Optional[str] = None          # SSH ed25519 — paramiko mesh
    bedrock_pubkey: Optional[str] = None  # Ed25519 identity — inter-node API auth


# ── ISO library ─────────────────────────────────────────────────────────────
# The three endpoints (list / upload / delete) live in mgmt/routes_iso.py.
# The ISO_DIR constant + VM inventory helpers stay here because the VM
# creation paths in app.py import them.

# Cluster-wide SeaweedFS FUSE mount — identical on every node, so
# `--cdrom {ISO_DIR}/<name>.iso` works from anywhere. See routes_iso.py
# for the upload path that writes here.
ISO_DIR = Path("/mnt/bedrock/iso")


# ── VM lifecycle saga runner ────────────────────────────────────────────────
#
# create / migrate / delete all run through the bedrock_d/vm/* sagas — the
# single live VM-lifecycle path (T-01/T-02). The saga executes ON THE MASTER
# (this mgmt process), which holds DRBD/arbiter authority; the CLI is a thin
# HTTP client that POSTs here. Returns the saga's final state dict.

def _run_vm_saga(kind: str, params: dict) -> dict:
    """Submit + synchronously run a VM-lifecycle saga on this node.
    Raises HTTPException on saga failure so the API returns a real 5xx
    instead of a 200 with a buried error."""
    import sys as _sys
    import socket as _socket
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from bedrock_d.orchestrator.sagas import SagaExecutor, SagaState
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _st
    # Importing the saga modules registers them in SAGAS.
    from bedrock_d.vm import create as _c, destroy as _d, migrate as _m  # noqa: F401
    ex = SagaExecutor(backend=RqliteSagaBackend(_st.RqliteClient()),
                      this_node=_socket.gethostname())
    op_id = ex.submit(kind=kind, target_node=_socket.gethostname(),
                      params=params, requested_by="mgmt")
    result = ex.execute_one(op_id)
    if result.state != SagaState.COMPLETED:
        raise HTTPException(
            500, f"{kind} saga failed at step "
                 f"{result.last_step!r}: {result.error}")
    return {"op_id": op_id, "state": result.state.value,
            "last_step": result.last_step}


def _vm_create_peers(vm_type: str) -> tuple[str, list[str]]:
    """Resolve (home, peers) for a create. home = the mgmt master; peers
    = home + (replicas-1) other nodes. Raises HTTPException if the
    cluster is too small for the requested type."""
    home = _mgmt_node_name()
    others = [n for n in get_nodes() if n != home]
    if vm_type == "cattle":
        return home, [home]
    if vm_type == "pet":
        if not others:
            raise HTTPException(400, "pet requires ≥1 peer")
        return home, [home, others[0]]
    if vm_type == "vipet":
        if len(others) < 2:
            raise HTTPException(400, "vipet requires ≥2 peers")
        return home, [home, others[0], others[1]]
    raise HTTPException(400, f"unknown vm_type: {vm_type}")


class MigrateRequest(BaseModel):
    target_node: Optional[str] = None


class HaLevelRequest(BaseModel):
    vm_type: str  # "cattle", "pet", or "vipet"
    peer_nodes: Optional[list] = None  # auto-pick if not specified


class VMDiskSpec(BaseModel):
    size_gb: int


class VMCreateRequest(BaseModel):
    name: str
    vcpus: int = 2
    ram_mb: int = 2048
    disk_gb: int = 20        # size of the primary (boot) disk
    priority: str = "normal"  # low | normal | high
    iso: Optional[str] = None  # filename in /mnt/bedrock/iso/, optional
    # Workload type. Must satisfy workload.validate_type against current
    # cluster size — pet needs ≥2 nodes, vipet needs ≥3.
    vm_type: str = "cattle"  # cattle | pet | vipet
    # Additional data disks, in order — vdb, vdc, vdd … Each is another thin LV
    # attached to the VM via virtio. Empty list = single-disk VM (unchanged).
    extra_disks: list[VMDiskSpec] = []

@app.post("/api/vms/{vm_name}/start")
def api_vm_start(vm_name: str):
    return _vm_start(vm_name)

@app.post("/api/vms/{vm_name}/stop")
def api_vm_stop(vm_name: str):
    return _vm_shutdown(vm_name)

@app.post("/api/vms/{vm_name}/force-stop")
def api_vm_force_stop(vm_name: str):
    return _vm_poweroff(vm_name)

@app.post("/api/vms/{vm_name}/ha-level")
async def api_vm_set_ha_level(vm_name: str, req: HaLevelRequest):
    """Fire-and-forget. Returns task_id immediately; the dashboard reads
    progress from /api/tasks (WS 'task' channel).

    All validation happens synchronously BEFORE creating the task, so
    clearly-invalid requests fail with a proper 4xx — they don't get a
    200 / task_id + async task-fail, which would mislead the caller."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    if req.vm_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid vm_type: {req.vm_type}")
    nodes_cfg = get_nodes()
    # `running_on` is empty for shut-off VMs — fall back to the first
    # `defined_on` node (where virsh dumpxml resolved) so offline
    # convert works too. Online convert keeps using the live host.
    src_name = (vm.get("running_on")
                or (vm.get("defined_on") or [None])[0])
    if not src_name:
        raise HTTPException(400,
            f"Cannot resolve home node for {vm_name} — VM not defined "
            f"on any cluster node")
    current_type = (
        "vipet" if vm.get("drbd_resource")
            and _count_drbd_peers(nodes_cfg[src_name]["host"], vm["drbd_resource"]) >= 3
        else ("pet" if vm.get("drbd_resource") else "cattle")
    )
    if current_type == req.vm_type:
        return {"status": "no-op", "current": current_type}
    # Upgrade (cattle/pet → pet/vipet): require enough peers up front so
    # an empty peer_nodes list errors before we burn a task on it.
    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[req.vm_type] > rank[current_type]:
        need_peers = {"pet": 1, "vipet": 2}[req.vm_type]
        chosen = req.peer_nodes or [n for n in nodes_cfg if n != src_name]
        # Filter to only nodes we don't already have on this resource
        if current_type == "pet" and req.vm_type == "vipet":
            existing = _parse_drbd_res(nodes_cfg[src_name]["host"],
                                       vm["drbd_resource"]) or {}
            chosen = [n for n in chosen if n not in existing.get("peers", [])]
            need_peers = 1
        else:
            chosen = [n for n in chosen if n != src_name]
        chosen = chosen[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400,
                f"{req.vm_type} needs {need_peers} peer node(s), "
                f"found {len(chosen)} usable")

    task = task_registry().create(
        "vm.set_ha_level", f"VM {vm_name}: {current_type} → {req.vm_type}",
        vm_name=vm_name, node=src_name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _vm_set_ha_level, vm_name, req.vm_type, req.peer_nodes, task)
            task.log(f"result: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id,
            "from": current_type, "to": req.vm_type}


@app.post("/api/vms")
async def api_vm_create(req: VMCreateRequest):
    """Fire-and-forget: returns {task_id} immediately. Create can take 1-2
    minutes for VMs with a big ISO or many disks; we don't block the UI.

    All input validation happens sync up-front so a bad name or ISO path
    returns 4xx immediately — not a 200 / task_id followed by an async
    task-fail (which would mislead the caller)."""
    if not _VM_NAME_RE.match(req.name):
        raise HTTPException(400,
            "VM name: 3-32 chars, lowercase letters/digits/dashes, "
            "start with a letter")
    if req.priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")
    if req.vcpus < 1 or req.vcpus > 32:
        raise HTTPException(400, "vcpus must be 1-32")
    if req.ram_mb < 128 or req.ram_mb > 131072:
        raise HTTPException(400, "ram_mb must be 128-131072")
    if req.disk_gb < 1 or req.disk_gb > 2048:
        raise HTTPException(400, "disk_gb must be 1-2048")
    for i, d in enumerate(req.extra_disks or []):
        if d.size_gb < 1 or d.size_gb > 8192:
            raise HTTPException(400,
                f"extra_disks[{i}].size_gb must be 1-8192")
    if req.iso:
        iso_name = Path(req.iso).name
        if not (ISO_DIR / iso_name).exists():
            raise HTTPException(400, f"ISO not found: {iso_name}")

    # Validate vm_type against current cluster size. Cattle = local LV
    # (no DRBD, any cluster); pet = 2-way DRBD (≥2 nodes); vipet = 3-way
    # DRBD (≥3 nodes). Reject early — never accept then quietly
    # downgrade pet→cattle, which would silently turn a replicated
    # workload into a single-host one.
    import sys as _sys
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    from lib import workload as _workload
    cluster_state_pre = build_cluster_state()
    node_count = len(cluster_state_pre.get("nodes") or {})
    ok, msg = _workload.validate_type(req.vm_type, node_count)
    if not ok:
        raise HTTPException(400, msg)

    # Existing VM?
    if req.name in cluster_state_pre["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    disk_count = 1 + len(req.extra_disks or [])

    # Resolve home + the full replica peer set BEFORE returning; a bad
    # cluster size for the requested type fails 4xx here, not async.
    # The intent breadcrumb is a secondary durability marker — the saga
    # itself writes a durable operations row that crash-resume keys off.
    home, peers = _vm_create_peers(req.vm_type)
    intent_idx = None
    try:
        intent_idx = _bs.vm_create_intent(
            name=req.name,
            vm_type=req.vm_type,
            host=home,
            ram_mb=int(req.ram_mb),
            disk_gb=int(req.disk_gb),
            requested_by=_os.environ.get("USER", "api"),
        )
    except Exception as e:
        # rqlite unreachable → fall through. The saga still creates the
        # VM (and writes its own durable operations row); we just don't
        # get the vm_create_intent breadcrumb for this run.
        log.warning(f"vm_create_intent write skipped: {e}")

    task = task_registry().create(
        "vm.create",
        f"Create {req.vm_type} VM {req.name} ({req.vcpus} vCPU, "
        f"{req.ram_mb} MB, {disk_count} disk"
        f"{'s' if disk_count != 1 else ''})",
        vm_name=req.name)

    # The bedrock_d vm_create saga is the single live path for every
    # type (cattle / pet / vipet) and is multi-disk aware. It runs ON
    # THE MASTER (this process) and crash-resumes from its own
    # operations row.
    saga_params = {
        "vm_name": req.name, "vcpus": int(req.vcpus),
        "ram_mb": int(req.ram_mb), "disk_gb": int(req.disk_gb),
        "extra_disks": [d.size_gb for d in (req.extra_disks or [])],
        "vm_type": req.vm_type, "priority": req.priority,
        "iso": req.iso, "peers": peers, "home": home,
    }

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            task.step_start(f"provision {req.vm_type}")
            result = await loop.run_in_executor(
                None, _run_vm_saga, "vm_create", saga_params)
            task.step_done(f"provision {req.vm_type}")
            task.log(f"created: {result}")
            task.succeed()
            # NOTE: the saga's register_vm step is the authoritative vms-row
            # writer (state='running' + failover_order). We deliberately do
            # NOT call _bs.vm_created here — it would reset state back to
            # 'created' on the just-started VM (ON CONFLICT … state='created').
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
            _log_create_failed(req.name, f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))
            _log_create_failed(req.name, str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": req.name,
            "intent_revision": intent_idx}


def _log_create_failed(vm_name: str, reason: str) -> None:
    """Settle a vm_create_intent with vm_create_failed when the async
    creator throws. Best-effort — logging shouldn't mask the original
    failure path."""
    try:
        _bs.vm_create_failed(name=vm_name, reason=reason)
    except Exception as e:
        log.warning(f"vm_create_failed write skipped: {e}")


@app.delete("/api/vms/{vm_name}")
async def api_vm_delete(vm_name: str):
    """Fire-and-forget. Runs teardown in background; task reports per-disk
    per-node progress so the UI can show what's happening."""
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm:
        raise HTTPException(404, f"Unknown VM: {vm_name}")
    disk_count = len(vm.get("disks") or []) or 1
    task = task_registry().create(
        "vm.delete",
        f"Delete VM {vm_name} ({disk_count} disk{'s' if disk_count != 1 else ''})",
        vm_name=vm_name)

    async def _runner():
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, _run_vm_saga, "vm_destroy", {"vm_name": vm_name})
            # Drop the dashboard inventory breadcrumb the saga doesn't know about.
            try:
                inv = load_inventory()
                if inv.pop(vm_name, None) is not None:
                    save_inventory(inv)
            except Exception as e:
                log.warning(f"vm_delete: inventory cleanup skipped: {e}")
            task.log(f"deleted: {result}")
            task.succeed()
        except HTTPException as e:
            task.fail(f"{e.status_code}: {e.detail}")
        except Exception as e:
            task.fail(str(e))

    asyncio.create_task(_runner())
    return {"status": "accepted", "task_id": task.id, "name": vm_name}


# ── VM settings (vcpus, ram, disk, priority, cdrom) ─────────────────────────

class ComputeRequest(BaseModel):
    vcpus: Optional[int] = None
    ram_mb: Optional[int] = None
    disk_gb: Optional[int] = None


class PriorityRequest(BaseModel):
    priority: str  # low | normal | high


class CdromRequest(BaseModel):
    action: str  # "eject" | "insert"
    iso: Optional[str] = None  # required when action=insert


@app.get("/api/vms/{vm_name}/settings")
def api_vm_get_settings(vm_name: str):
    return _vm_get_settings(vm_name)


@app.post("/api/vms/{vm_name}/compute")
def api_vm_compute(vm_name: str, req: ComputeRequest):
    return _vm_set_resources(vm_name, req)


@app.post("/api/vms/{vm_name}/priority")
def api_vm_priority(vm_name: str, req: PriorityRequest):
    return _vm_set_priority(vm_name, req.priority)


@app.post("/api/vms/{vm_name}/cdrom")
def api_vm_cdrom(vm_name: str, req: CdromRequest):
    return _vm_set_cdrom(vm_name, req.action, req.iso)


class AttachDiskRequest(BaseModel):
    size_gb: int  # thin LV size


@app.post("/api/vms/{vm_name}/disks")
def api_vm_attach_disk(vm_name: str, req: AttachDiskRequest):
    """Attach a new thin-provisioned disk to an existing VM. Live-attach via
    `virsh attach-disk --live --config` so the guest sees the new disk
    immediately and it survives reboot. For pet/ViPet VMs, converting the
    newly-attached disk to DRBD is a separate `pet → pet` re-convert step
    (not implemented in this endpoint; the attach only adds a local LV."""
    if req.size_gb < 1 or req.size_gb > 8192:
        raise HTTPException(400, "size_gb must be 1-8192")
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")
    nodes_cfg = get_nodes()
    host_name = vm.get("running_on") or (vm.get("defined_on") or [None])[0]
    if not host_name: raise HTTPException(503, "VM has no known node")
    host = nodes_cfg[host_name]["host"]

    existing_targets = {d["target"] for d in vm.get("disks", [])}
    # Pick next free vd* letter
    for ch in "bcdefghijklmnop":
        tgt = f"vd{ch}"
        if tgt not in existing_targets: break
    else:
        raise HTTPException(400, "No free virtio target (vda..vdp in use)")
    idx = len(vm.get("disks", []))
    lv_name = f"vm-{vm_name}-disk{idx}"
    vg = _vm_disk_vg(host)
    lv_path = f"/dev/{vg}/{lv_name}"

    _ensure_thinpool(host, vg_name=vg)
    push_log(f"Attach disk to {vm_name}: lvcreate {req.size_gb}G ({lv_name}) "
             f"in VG {vg}",
             node=host_name, app="bedrock-mgmt")
    out, rc = ssh_cmd_rc(host,
        f"lvcreate -y -V {req.size_gb}G --thin -n {lv_name} {vg}/thinpool "
        f"2>&1", timeout=60)
    if rc != 0 and "already exists" not in out:
        raise HTTPException(500, f"lvcreate failed: {out}")

    # virsh attach-disk — live attach when VM is running, --config either way
    live_flag = "--live" if vm["state"] == "running" else ""
    out, rc = ssh_cmd_rc(host,
        f"virsh attach-disk {vm_name} {lv_path} {tgt} --targetbus virtio "
        f"--driver qemu --subdriver raw --sourcetype block "
        f"{live_flag} --config 2>&1", timeout=30)
    if rc != 0:
        ssh_cmd_rc(host, f"lvremove -f {lv_path} 2>&1", timeout=15)
        raise HTTPException(500, f"attach-disk failed: {out}")

    # Update inventory
    inv = load_inventory()
    entry = inv.setdefault(vm_name, {})
    entry.setdefault("disks", [
        {"index": 0, "lv": f"vm-{vm_name}-disk0",
         "size_gb": entry.get("disk_gb", 0)},
    ])
    entry["disks"].append({"index": idx, "lv": lv_name, "size_gb": req.size_gb})
    save_inventory(inv)

    push_log(f"Attached {req.size_gb}G disk {tgt} to VM {vm_name}",
             node=host_name, app="bedrock-mgmt", level="info")
    return {"status": "attached", "target": tgt, "lv": lv_name,
            "size_gb": req.size_gb}


@app.post("/api/vms/{vm_name}/migrate")
def api_vm_migrate(vm_name: str, req: MigrateRequest = MigrateRequest()):
    """Live-migrate via the vm_migrate saga (the single migrate path).
    The saga resolves source/target/resources from rqlite, cycles
    dual-primary across every disk, records the post-promote UUID on the
    new primary (so HA survives the move — VM-02), and keeps the domain
    defined on the source for failback."""
    target = req.target_node
    if not target:
        # No explicit target → pick the VM's backup peer.
        vm = build_cluster_state()["vms"].get(vm_name)
        if not vm:
            raise HTTPException(404, f"Unknown VM: {vm_name}")
        target = vm.get("backup_node")
        if not target:
            raise HTTPException(400, "no target node and no backup peer to pick")
    return _run_vm_saga("vm_migrate",
                        {"vm_name": vm_name, "target": target})


# ── Backup endpoints ────────────────────────────────────────────────────────
# Kopia orchestration. The mgmt master writes the backup target to rqlite;
# every node's reactor reacts by running `kopia repository connect` locally
# so any node can do backups/restores of its locally-resident VMs.
# See snapshots-and-backup.md §9c-bis.

class BackupTargetSetRequest(BaseModel):
    target_id: str = "main"
    kind: str = "kopia-s3"           # "kopia-s3" | "kopia-fs"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    # Self-hosted S3 (QNAP, MinIO with self-signed certs) often needs
    # one of these. Default off — operator opts in per target.
    s3_disable_tls: bool = False              # plain HTTP
    s3_disable_tls_verification: bool = False  # HTTPS, skip cert check
    filesystem_path: str = ""
    override_source_prefix: str = ""  # default: "<cluster_uuid>:vms"
    cache_directory: str = ""         # default: /var/cache/bedrock-kopia
    reason: str = ""
    # ── Multi-target replication ───────────────────────────────────
    # Ordered list of OTHER backup_targets ids this target mirrors to via
    # `kopia repository sync-to` after each backup. Secondaries are normal
    # targets (own endpoint/bucket/creds) sharing the one cluster password.
    # Empty = single-target (the default; no behaviour change).
    sync_to: list[str] = []
    delete_orphans: bool = False      # kopia sync-to --delete (prune mirrors)
    # A mirror target is a sync-to DESTINATION only — registered for its
    # storage config + creds but NEVER independently created (an independent
    # `kopia repository create` gives it an incompatible format block). It
    # starts empty; the first sync-to from its primary copies the source
    # format. Set this when adding a replication destination.
    is_mirror: bool = False
    # ── Credentials (NEVER logged) ─────────────────────────────────
    # Optional inline secrets. When present, mgmt writes the
    # corresponding files on every cluster node before recording the
    # backup target in rqlite. When absent, the operator is expected
    # to have dropped the files manually.
    s3_access_key: Optional[str] = None    # → KOPIA_S3_ACCESS_KEY in env file
    s3_secret_key: Optional[str] = None    # → KOPIA_S3_SECRET_KEY in env file
    # The per-repo kopia password → sealed in rqlite (backup_targets.
    # repo_password_enc), the cluster-internal source of truth. None = unchanged;
    # nodes materialize their own 0600 override from rqlite via the reactor.
    encryption_password: Optional[str] = None
    # If True, overwrite this repo's existing REAL password. Defaults to False —
    # changing it makes existing encrypted backups unreadable, a deliberate
    # destructive action. (Switching off the public default needs no force.)
    force_password_overwrite: bool = False


class BackupRunRequest(BaseModel):
    target_id: str = "main"
    label: str = ""                   # operator-visible tag


class RestoreRequest(BaseModel):
    target_id: str = "main"
    # Empty → restore the VM's NEWEST recorded backup. run_restore_to_ha
    # resolves vms[<name>].backups[0] when this is blank, so the common
    # "restore latest" case needs no snapshot id from the operator.
    kopia_snapshot_id: str = ""
    dest_node: Optional[str] = None
    target_lv_path: Optional[str] = None


class BackupDeleteRequest(BaseModel):
    target_id: str = "main"
    reason: str = ""


def _import_backup_module():
    """Lazy-import mgmt/backup.py — keeps app.py importable when the
    module is missing (e.g. during partial install) and matches the
    lazy-import pattern used elsewhere for lib modules."""
    import backup as _b
    return _b


# ── Backup secret propagation ──────────────────────────────────────────────
#
# Two secrets need to live on every node, mode 0600, never in cluster state:
#   - /etc/bedrock/backup.key                    (kopia repo password)
#   - /etc/bedrock/backup-credentials/<id>.env   (S3 access/secret keys)
#
# The dashboard collects them once on the master, then mgmt fans them
# out via the existing root@host SSH mesh that agent_install set up.
# Failure to propagate to one node is logged (push_log) but doesn't
# abort the target-set; the affected node will fail loudly the first
# time its reactor tries to `kopia repository connect`. The operator
# can re-trigger propagation by submitting the same form again.

BACKUP_KEY_FILE = "/etc/bedrock/backup.key"
BACKUP_CRED_DIR = "/etc/bedrock/backup-credentials"


def _write_local_secret(path: str, content: str, mode: int = 0o600):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.parent.chmod(0o700)
    p.write_text(content)
    p.chmod(mode)


def _write_remote_secret(host: str, path: str, content: str,
                         mode: int = 0o600, timeout: int = 15):
    """Push a secret file via paramiko SFTP. Atomic-replace via tmp +
    POSIX rename. Caller passes already-rendered content (env file or
    raw key).

    Why posix_rename: plain `sftp.rename()` maps to SSH_FXP_RENAME
    which (per the SFTP spec) refuses to overwrite an existing target.
    `posix_rename` maps to OpenSSH's `posix-rename@openssh.com`
    extension and behaves like POSIX `rename(2)` — atomic replace.
    Without this, every secret update past the first one fails with a
    nondescript "Failure" from the server."""
    c = _ssh_connect(host)
    try:
        parent = str(Path(path).parent)
        # exec_command is async-fire-and-forget; wait for the channel
        # to close so the directory definitely exists before SFTP open.
        _, so, _ = c.exec_command(f"mkdir -p -m 700 {parent}", timeout=timeout)
        so.channel.recv_exit_status()

        sftp = c.open_sftp()
        tmp = f"{path}.tmp.bedrock"
        with sftp.open(tmp, "wb") as f:
            f.write(content.encode())
        sftp.chmod(tmp, mode)
        sftp.posix_rename(tmp, path)
        sftp.close()
    finally:
        c.close()


def _propagate_secret(rel_path: str, content: str, mode: int = 0o600):
    """Write a secret to `rel_path` on every node (including this one).
    Returns (ok_nodes, failed_nodes) so the caller can surface partial
    failure to the UI."""
    ok: list[str] = []
    failed: list[tuple[str, str]] = []  # (node_name, reason)
    self_host = _self_host()
    for name, node in get_nodes().items():
        host = node.get("host")
        if not host:
            continue
        try:
            if host == self_host:
                _write_local_secret(rel_path, content, mode)
            else:
                _write_remote_secret(host, rel_path, content, mode)
            ok.append(name)
        except Exception as e:
            failed.append((name, str(e)))
            push_log(f"backup: propagate {rel_path} → {name} ({host}) "
                     f"failed: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")
    return ok, failed


def _self_host() -> str:
    """Best-effort detection of this node's IP/hostname so we don't
    SSH-loop to ourselves (some sshd configs reject that)."""
    try:
        from lib import state as _state
        s = _state.load() if hasattr(_state, "load") else {}
        nodes = get_nodes()
        n = nodes.get(s.get("node_name", ""))
        if n and n.get("host"):
            return n["host"]
    except Exception:
        pass
    return ""


def _render_s3_creds_env(access_key: str, secret_key: str) -> str:
    """Bash-sourceable env file. Variable names match what kopia's S3
    backend reads from the environment (KOPIA_S3_ACCESS_KEY, etc.)."""
    import shlex as _sh
    return (
        "# bedrock-managed; do not edit by hand. mode 0600.\n"
        f"export KOPIA_S3_ACCESS_KEY={_sh.quote(access_key)}\n"
        f"export KOPIA_S3_SECRET_KEY={_sh.quote(secret_key)}\n"
        # AWS_* mirrors so other tools that read the file work too
        f"export AWS_ACCESS_KEY_ID={_sh.quote(access_key)}\n"
        f"export AWS_SECRET_ACCESS_KEY={_sh.quote(secret_key)}\n"
    )


@app.get("/api/backup/credentials/status")
def api_backup_credentials_status():
    """What secrets exist on each node? UI uses this to decide whether
    to show empty fields (operator must enter creds) vs. "already
    configured" placeholders.

    Returns per-node booleans:
      - has_password: /etc/bedrock/backup.key exists, mode 0600
      - has_creds.<target_id>: corresponding env file exists
    """
    out: dict = {"nodes": {}}
    for name, node in get_nodes().items():
        host = node.get("host", "")
        if not host:
            continue
        info: dict = {"has_password": False, "creds": {}}
        try:
            r = ssh_cmd(host, f"[ -f {BACKUP_KEY_FILE} ] && echo yes || echo no")
            info["has_password"] = (r.strip() == "yes")
            r2 = ssh_cmd(host, f"ls {BACKUP_CRED_DIR}/*.env 2>/dev/null | xargs -n1 basename 2>/dev/null")
            for ln in (r2 or "").splitlines():
                ln = ln.strip()
                if ln.endswith(".env"):
                    info["creds"][ln[:-4]] = True
        except Exception as e:
            info["error"] = str(e)
        out["nodes"][name] = info
    return out


@app.post("/api/backup/targets")
def api_backup_target_set(req: BackupTargetSetRequest):
    """Configure (or update) the cluster's backup target. Idempotent —
    emitting the same target twice produces a single fold result.

    Action sequence:
      1. (Optional) Propagate inline credentials to every node:
           - encryption_password → /etc/bedrock/backup.key
           - s3_access_key/secret → /etc/bedrock/backup-credentials/<id>.env
         Files are written mode 0600. Failure on individual nodes is
         logged but doesn't abort — the affected node will fail loudly
         on its reactor's `kopia repository connect`.
      2. Run `kopia repository connect` (or create) locally on master.
         Verifies the repo's block hash is ≥256 bits.
      3. Write the backup target to rqlite. Every node's reactor reacts
         by running `kopia repository connect` against the new target.
      4. Return the revision so callers know the change is committed.

    Credentials are NEVER persisted to cluster state — only file paths
    and metadata (endpoint, bucket, region) are stored."""
    backup = _import_backup_module()

    propagation_warnings: list[str] = []

    # ── (0) Validate the mirror set UP FRONT, before any writes ──────
    # A bad sync_to must 400 with NO partial commit (no kopia repo created, no
    # target row written). STRONG read so a sibling target created moments ago
    # is visible (a level='none' local replica can lag and falsely reject a
    # valid secondary). Each secondary must EXIST and be is_mirror=true — a
    # non-mirror (independently-created) repo has an incompatible format block
    # and every sync-to into it would fail "incompatible data" forever.
    sync_to = list(req.sync_to or [])
    if req.target_id in sync_to:
        raise HTTPException(
            400, f"a backup target cannot mirror to itself ({req.target_id!r})")
    try:
        strong_targets = _cluster_state.load_cluster(
            level="strong").get("backup_targets", {}) or {}
    except Exception as e:
        raise HTTPException(
            503, f"could not validate sync_to — rqlite strong read failed "
            f"(no leader?): {e}")
    for sid in sync_to:
        t = strong_targets.get(sid)
        if t is None:
            raise HTTPException(
                400, f"sync_to references unknown backup target {sid!r} — "
                f"create it as a mirror (is_mirror=true) first")
        if not t.get("is_mirror"):
            raise HTTPException(
                400, f"sync_to secondary {sid!r} is not a mirror target. Create "
                f"the mirror destination with is_mirror=true — a mirror is never "
                f"independently initialized; the first sync-to copies the "
                f"primary's repo format into it.")
        # A mirror must belong to exactly ONE primary. Two primaries syncing to
        # the same mirror push incompatible repo formats (every sync after the
        # first fails "incompatible data") and, with delete_orphans, their
        # --delete passes would prune each other's blobs (data loss). Reject a
        # secondary already owned by a different primary.
        other = next((pid for pid, pt in strong_targets.items()
                      if pid != req.target_id
                      and sid in (pt.get("sync_to") or [])), None)
        if other is not None:
            raise HTTPException(
                400, f"mirror {sid!r} is already a replication target of "
                f"{other!r}. A mirror can belong to only one primary "
                f"(two primaries would push incompatible formats and "
                f"--delete-prune each other). Use a separate mirror target.")
    # Existing mirror set for this primary (strong, so a clear isn't skipped
    # against a stale replica).
    current_mirrors = (strong_targets.get(req.target_id) or {}).get("sync_to") or []

    # ── (1a) Encryption password → RQLITE (the central, cluster-internal store) ─
    # The repo password is NOT pushed to per-node files here; it is persisted
    # sealed in rqlite (backup_targets.repo_password_enc) in step (3), and each
    # node's reactor materializes its own 0600 override from rqlite. rqlite (mTLS,
    # cluster-internal) is the single source of truth — so a repo's key survives
    # any node loss and is never out of sync across the cluster.
    if req.encryption_password is not None:
        # Only a REAL (non-public-default) password already on THIS repo is
        # protected from overwrite — changing it makes existing encrypted backups
        # unreadable. has_repo_password (from the strong rqlite read) is True only
        # when a real per-repo password is set; the public default is not.
        existing_has_real = bool(
            (strong_targets.get(req.target_id) or {}).get("has_repo_password"))
        if existing_has_real and not req.force_password_overwrite:
            raise HTTPException(
                400,
                "encryption_password supplied but this repo already has a real "
                "password. Changing it makes existing encrypted backups "
                "unreadable. Pass force_password_overwrite=true to confirm — or "
                "omit encryption_password to keep the current key."
            )

    # ── (1b) S3 credentials → RQLITE (the central store), not per-node files ──
    # The secret is sealed in rqlite (backup_targets.s3_secret_key_enc) in step
    # (3); each node materializes its own 0600 .env cache from rqlite via the
    # reactor (and the master directly, below). No _propagate_secret.
    if req.kind == "kopia-s3" and (req.s3_access_key or req.s3_secret_key):
        if not (req.s3_access_key and req.s3_secret_key):
            raise HTTPException(
                400, "s3_access_key and s3_secret_key must be supplied together"
            )

    # ── (2) Connect this node + verify hash floor ──────────────────
    # SKIP for a mirror target: it must stay empty so the first
    # `kopia repository sync-to` can copy the source's format block into it.
    # Independently creating it here would give it an incompatible format
    # and every sync-to would fail "destination contains incompatible data".
    if not req.is_mirror:
        try:
            backup.configure_target_locally(
                target_id=req.target_id, kind=req.kind,
                s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
                s3_region=req.s3_region,
                s3_disable_tls=req.s3_disable_tls,
                s3_disable_tls_verification=req.s3_disable_tls_verification,
                filesystem_path=req.filesystem_path,
                override_source_prefix=req.override_source_prefix,
                cache_directory=req.cache_directory,
                # Master's own setup runs BEFORE the rqlite write below, so pass
                # the new password + S3 creds explicitly; None/'' → read rqlite.
                repo_password=req.encryption_password,
                s3_access_key=req.s3_access_key or "",
                s3_secret_key=req.s3_secret_key or "",
            )
        except Exception as e:
            raise HTTPException(400, f"backup target setup failed locally: {e}")

    # ── (3) Persist to rqlite so peers get it via their reactors ──
    try:
        rev = _bs.backup_target_set(
            target_id=req.target_id, kind=req.kind,
            s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
            s3_region=req.s3_region,
            s3_disable_tls=req.s3_disable_tls,
            s3_disable_tls_verification=req.s3_disable_tls_verification,
            filesystem_path=req.filesystem_path,
            override_source_prefix=req.override_source_prefix,
            cache_directory=req.cache_directory,
            is_mirror=req.is_mirror,
            # Every secret lands sealed in rqlite here (the source of truth).
            # '' when unchanged → CASE-preserve keeps the stored value.
            repo_password=req.encryption_password or "",
            s3_access_key=req.s3_access_key or "",
            s3_secret_key=req.s3_secret_key or "",
            reason=req.reason,
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    # ── (3b) Persist the mirror set (already validated in step 0). Write only
    # when setting OR clearing, so single-target sets don't churn the table.
    if sync_to or current_mirrors:
        try:
            rev = _bs.backup_target_sync_set(
                req.target_id, sync_to,
                delete_orphans=req.delete_orphans, reason=req.reason,
            )
        except Exception as e:
            raise HTTPException(500, f"rqlite write (mirror set) failed: {e}")

    push_log(f"backup target {req.target_id!r} set ({req.kind})"
             + (f" → mirrors {sync_to}" if sync_to else ""),
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "target_id": req.target_id,
        "sync_to": list(req.sync_to or []),
        "warnings": propagation_warnings,
    }


@app.get("/api/backup/targets")
def api_backup_targets_list():
    """List configured backup targets, drawn from cluster state.
    Always returns immediately — no kopia roundtrip."""
    cluster = load_cluster()
    return {"targets": cluster.get("backup_targets", {})}


@app.get("/api/backups")
def api_backups_list_all():
    """Cluster-wide backup history. Walks every VM in cluster state
    and flattens its `backups` list, decorating each row with the
    owning vm name + whether the source VM still exists. Used by
    the dashboard's Backups page to render a single restore-able
    list across the whole cluster.

    Sorted newest-first by ts_index (monotonic timestamp).
    vm_present=False rows are kept so operators can still restore a
    deleted VM's snapshots into a fresh LV."""
    cluster = load_cluster()
    vms = cluster.get("vms", {}) or {}
    out = []
    for vm_name, vm in vms.items():
        for b in (vm.get("backups") or []):
            row = dict(b)
            row["vm"] = vm_name
            row["vm_present"] = True
            out.append(row)
    # Snapshots whose source VM was deleted: cluster state only retains
    # backup entries on live VM records, so there are no orphan rows to
    # surface here. (Listing orphans from the repo via
    # `kopia snapshot list` is a possible future addition.)
    out.sort(key=lambda r: r.get("ts_index", 0), reverse=True)
    return {"backups": out}


@app.delete("/api/backup/targets/{target_id}")
def api_backup_target_remove(target_id: str, reason: str = ""):
    try:
        rev = _bs.backup_target_removed(
            target_id=target_id, reason=reason or "operator-remove",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "target_id": target_id}


# ── Witness management ──────────────────────────────────────────────
# Add / list / remove cluster witnesses for the weighted-vote quorum
# (each valid witness = 1 vote; nodes = 100). Writes the rqlite
# `witnesses` table — Raft replicates it, and EVERY node's netd 1 Hz
# election tick reloads the list automatically, so no explicit daemon
# propagation is needed from mgmt (unlike the CLI path). The operator
# dashboard drives these.

class WitnessAddRequest(BaseModel):
    witness_id: str
    addr: str = ""             # echo: "host[:port]"; fileshare: mounted dir path
    witness_pubkey: str = ""   # X25519 pubkey hex (64 chars) — required for echo
    backend: str = "echo"      # "echo" | "fileshare" (smb/s3 = future managed)
    reason: str = ""


@app.get("/api/witnesses")
def api_witnesses_list():
    return {"witnesses": load_cluster().get("witnesses", {})}


def _api_witness_add_fileshare(wid: str, req: WitnessAddRequest):
    """Register a PATH-BASED fileshare witness. addr = an absolute directory the
    operator has mounted the shared store (NFS/SMB/object) at on EVERY node;
    netd's off-hot-path worker writes slot-<NN>.bin there and folds the verdict
    into the vote. We probe writability on THIS node (the master) as a fail-fast
    UX guard — full per-node assurance is enforced at vote time by the slot
    protocol (a node that can't write leaves its slot absent → 0 votes, never a
    miscount)."""
    import os as _os
    try:
        from lib import witness_file as _wf  # type: ignore
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import witness_file as _wf  # type: ignore
    path = (req.addr or "").strip()
    if not path:
        raise HTTPException(400, "addr (the mounted share directory) is required "
                                 "for a fileshare witness")
    if not _os.path.isabs(path):
        raise HTTPException(400, f"fileshare witness path must be absolute, "
                                 f"got {path!r}")
    err = _wf.probe_writable(path)
    if err:
        raise HTTPException(
            400, f"fileshare witness path {path!r} is not usable on this node: "
            f"{err}. Mount the share and ensure it is writable on EVERY node "
            f"before adding it.")
    try:
        rev = _bs.witness_register(witness_id=wid, addr=path,
                                   witness_pubkey_hex="",
                                   encrypted_witness_key_hex="",
                                   backend="fileshare")
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} added (fileshare {path})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "addr": path, "backend": "fileshare"}


@app.post("/api/witnesses")
def api_witness_add(req: WitnessAddRequest):
    wid = (req.witness_id or "").strip()
    if not wid:
        raise HTTPException(400, "witness_id is required")
    backend = (req.backend or "echo").strip().lower()
    if backend not in ("echo", "fileshare", "smb", "s3"):
        raise HTTPException(400, f"unknown witness backend {backend!r} "
                                 f"(expected echo | fileshare)")
    if backend in ("smb", "s3"):
        # NATIVE (Bedrock-managed-creds) SMB/S3 is a future build — backup uses
        # kopia's own S3 client and there is no mount/cred infra to reuse, so an
        # S3 blob client in the quorum path would be net-new. Today a fileshare
        # witness is PATH-BASED: the operator mounts the SMB/S3/NFS share on
        # every node and adds it with backend='fileshare' + that path; Bedrock
        # writes slot files there. Refuse smb/s3 rather than register a witness
        # with no transport (it would raise the quorum bar without ever voting →
        # can BLOCK failover on a 2-node cluster).
        raise HTTPException(
            400, f"witness backend {backend!r} is not a managed backend yet. "
            f"Mount the {backend.upper()} share on every node and add it as a "
            f"fileshare witness (backend='fileshare', addr=<mounted dir>) — "
            f"Bedrock writes slot files there. Managed-{backend} is a future build.")
    if backend == "fileshare":
        return _api_witness_add_fileshare(wid, req)
    addr = (req.addr or "").strip()
    if not addr:
        raise HTTPException(400, "addr is required (ipv4 or ipv4:port)")
    # An Echo witness must be an IPv4 UNICAST literal: netd directed-probes it
    # from the single-threaded 1Hz election tick over an AF_INET socket, so a
    # hostname (synchronous getaddrinfo would stall failover detection), an
    # IPv6 literal (unreachable on AF_INET), or a multicast/broadcast/0.0.0.0
    # addr (would flood the segment) are all refused HERE — fail loud at add
    # time rather than register an unusable witness that silently raises the
    # quorum bar. host:port, default port 12321.
    import ipaddress as _ipaddr
    host, _, port_s = addr.partition(":") if ":" in addr else (addr, "", "")
    port = 12321
    if port_s:
        try:
            port = int(port_s)
        except ValueError:
            raise HTTPException(400, f"invalid port {port_s!r} in addr {addr!r}")
        if not (1 <= port <= 65535):
            raise HTTPException(400, f"port {port} out of range (1-65535)")
    try:
        ip = _ipaddr.ip_address(host)
    except ValueError:
        raise HTTPException(
            400, f"Echo witness address must be an IPv4 literal, not a "
            f"hostname ({host!r}). A hostname would block the election tick on "
            f"DNS. Add the Echo by its IP.")
    if (ip.version != 4 or ip.is_multicast or ip.is_unspecified
            or ip.is_reserved or ip.is_loopback or ip.is_link_local):
        raise HTTPException(
            400, f"Echo witness address {host!r} is not a usable IPv4 unicast "
            f"address (no multicast/broadcast/loopback/link-local/unspecified).")
    stored_addr = f"{host}:{port}"
    pubkey = (req.witness_pubkey or "").strip().lower()
    if backend == "echo":
        # An Echo's X25519 public key is 32 bytes = 64 hex chars. Validate
        # FAIL-LOUD: a bad paste would silently write a witness netd can never
        # authenticate against (it would just never count toward quorum).
        if len(pubkey) != 64 or any(c not in "0123456789abcdef" for c in pubkey):
            raise HTTPException(
                400, "witness_pubkey must be 64 hex chars (the Echo's X25519 "
                "public key) for an echo witness")
    try:
        rev = _bs.witness_register(witness_id=wid, addr=stored_addr,
                                   witness_pubkey_hex=pubkey,
                                   encrypted_witness_key_hex="",
                                   backend=backend)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} added ({backend} {stored_addr})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "addr": stored_addr, "backend": backend}


@app.delete("/api/witnesses/{witness_id}")
def api_witness_remove(witness_id: str, reason: str = ""):
    # 404 for a non-existent witness — witness_unregister's DELETE matches 0
    # rows but still "succeeds" and bumps the revision, so without this a
    # typo'd delete reports success and churns every node's reactor for nothing.
    if witness_id not in (load_cluster().get("witnesses") or {}):
        raise HTTPException(404, f"witness {witness_id!r} not found")
    try:
        rev = _bs.witness_unregister(witness_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {witness_id!r} removed", app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": witness_id}


@app.get("/api/witnesses/discover")
def api_witnesses_discover():
    """Best-effort mDNS discovery of BedRock Echo witnesses (bedrock-echo.local)
    on the LAN, so the dashboard can offer one-click add. Each result carries
    echo_id (used AS the witness_id — netd binds the vote to echo_id==witness_id)
    and the Echo's pubkey, so nothing needs hand-typing. Echoes advertise this
    service (real firmware + the testbed stub); an Echo on a routed segment that
    doesn't answer multicast can still be added by IP."""
    try:
        from lib import discovery as _disc
        echoes = _disc.discover_echo_witnesses(timeout=2.0)
    except Exception as e:
        raise HTTPException(500, f"discovery failed: {e}")
    return {"candidates": [
        {"ip": e.ip, "echo_id": e.echo_id, "pubkey": e.pubkey}
        for e in (echoes or [])]}


# ── Consolidated storage endpoints (S3 / SMB / NFS) — the unification (#5) ──
# ONE endpoint row carries the location + storage creds; it is then ACTIVATED for
# backups (a backup_target) and/or as a fileshare/S3 witness, each referencing it
# by endpoint_id. Secrets (s3_secret_key, fs_password) are AEAD-sealed in rqlite by
# the setter and NEVER returned by the list API (only has_* flags).

class StorageEndpointSetRequest(BaseModel):
    endpoint_id: str
    type: str                                  # 's3' | 'smb' | 'nfs'
    label: str = ""
    # S3
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_prefix: str = ""
    s3_disable_tls: bool = False
    s3_disable_tls_verification: bool = False
    s3_access_key: str = ""
    # SMB/NFS
    fs_server: str = ""
    fs_share: str = ""
    fs_options: str = ""
    fs_username: str = ""
    reason: str = ""
    # Secrets: None = KEEP the stored value (a label edit must not wipe a secret
    # the operator didn't re-type); a string (incl. "") = set it.
    s3_secret_key: Optional[str] = None
    fs_password: Optional[str] = None


class StorageEndpointTestRequest(StorageEndpointSetRequest):
    # Test BEFORE committing: the operator may pass freshly-typed secrets that are
    # not yet in rqlite, so the test request carries them inline (never logged).
    usage: str = "witness"                     # 'witness' (strict) | 'kopia' (cached)


def _endpoint_usage(cluster: dict, endpoint_id: str) -> dict:
    """Which backup_targets + witnesses reference this endpoint (for the list UI
    + the in-use delete guard)."""
    bts = [tid for tid, t in (cluster.get("backup_targets") or {}).items()
           if (t or {}).get("endpoint_id") == endpoint_id]
    wits = [wid for wid, w in (cluster.get("witnesses") or {}).items()
            if (w or {}).get("endpoint_id") == endpoint_id]
    return {"backup_targets": bts, "witnesses": wits}


@app.get("/api/storage-endpoints")
def api_storage_endpoints_list():
    cluster = load_cluster()
    eps = cluster.get("storage_endpoints") or {}
    out = []
    for eid, ep in eps.items():
        row = dict(ep)
        row["endpoint_id"] = eid
        row["usage"] = _endpoint_usage(cluster, eid)
        out.append(row)
    return {"endpoints": out}


@app.post("/api/storage-endpoints")
def api_storage_endpoint_set(req: StorageEndpointSetRequest):
    eid = (req.endpoint_id or "").strip()
    if not eid:
        raise HTTPException(400, "endpoint_id is required")
    typ = (req.type or "").strip().lower()
    if typ not in ("s3", "smb", "nfs"):
        raise HTTPException(400, f"type must be s3|smb|nfs, not {typ!r}")
    # Secret-reuse on edit: None → keep the stored (sealed) secret, so editing the
    # label/region can't silently wipe a password the operator didn't re-type.
    s3_secret = req.s3_secret_key
    if s3_secret is None:
        s3_secret = _bs.storage_endpoint_secret(eid, "s3_secret_key")
    fs_password = req.fs_password
    if fs_password is None:
        fs_password = _bs.storage_endpoint_secret(eid, "fs_password")
    try:
        rev = _bs.storage_endpoint_set(
            eid, typ, label=req.label,
            s3_endpoint=req.s3_endpoint, s3_bucket=req.s3_bucket,
            s3_region=req.s3_region, s3_prefix=req.s3_prefix,
            s3_disable_tls=req.s3_disable_tls,
            s3_disable_tls_verification=req.s3_disable_tls_verification,
            s3_access_key=req.s3_access_key, s3_secret_key=s3_secret or "",
            fs_server=req.fs_server, fs_share=req.fs_share,
            fs_options=req.fs_options, fs_username=req.fs_username,
            fs_password=fs_password or "", reason=req.reason)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"storage endpoint {eid!r} ({typ}) saved",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "endpoint_id": eid}


@app.delete("/api/storage-endpoints/{endpoint_id}")
def api_storage_endpoint_remove(endpoint_id: str, reason: str = ""):
    cluster = load_cluster()
    if endpoint_id not in (cluster.get("storage_endpoints") or {}):
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    usage = _endpoint_usage(cluster, endpoint_id)
    if usage["backup_targets"] or usage["witnesses"]:
        raise HTTPException(
            409, f"endpoint {endpoint_id!r} is still in use by "
            f"backup_targets={usage['backup_targets']} "
            f"witnesses={usage['witnesses']} — deactivate those first")
    try:
        rev = _bs.storage_endpoint_removed(endpoint_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"storage endpoint {endpoint_id!r} removed", app="bedrock-mgmt",
             level="info")
    return {"status": "ok", "revision": rev}


@app.post("/api/storage-endpoints/test")
def api_storage_endpoint_test(req: StorageEndpointTestRequest):
    """Test-on-MASTER-before-commit (this handler runs on the mgmt master). Mounts
    /connects the endpoint with the SUPPLIED creds, proves a real write + read-back
    round-trip (S3 PUT/GET/DELETE; SMB/NFS mount + read-after-write = best-effort
    DFS-R guard), and returns (ok, reason). Never writes to rqlite — pure probe."""
    typ = (req.type or "").strip().lower()
    if typ not in ("s3", "smb", "nfs"):
        raise HTTPException(400, f"type must be s3|smb|nfs, not {typ!r}")
    # Secrets: prefer the freshly-typed value; fall back to the stored one when the
    # operator re-tests an existing endpoint without re-typing.
    s3_secret = req.s3_secret_key
    if s3_secret is None:
        s3_secret = _bs.storage_endpoint_secret(req.endpoint_id, "s3_secret_key")
    fs_password = req.fs_password
    if fs_password is None:
        fs_password = _bs.storage_endpoint_secret(req.endpoint_id, "fs_password")
    endpoint = {
        "type": typ, "s3_endpoint": req.s3_endpoint, "s3_bucket": req.s3_bucket,
        "s3_region": req.s3_region, "s3_prefix": req.s3_prefix,
        "s3_disable_tls": req.s3_disable_tls,
        "s3_disable_tls_verification": req.s3_disable_tls_verification,
        "s3_access_key": req.s3_access_key,
        "fs_server": req.fs_server, "fs_share": req.fs_share,
        "fs_options": req.fs_options, "fs_username": req.fs_username,
    }
    try:
        from lib import storage_mount as _sm
        usage = _sm.WITNESS if (req.usage or "witness") == "witness" else _sm.KOPIA
        ok, reason = _sm.test_endpoint(
            endpoint, usage,
            username=req.fs_username, password=fs_password or "",
            s3_secret_key=s3_secret or "")
    except Exception as e:
        raise HTTPException(500, f"test error: {e}")
    return {"ok": ok, "reason": reason}


class EndpointActivateRequest(BaseModel):
    # Optional explicit id; defaults to deriving one from the endpoint id.
    witness_id: str = ""
    skip_test: bool = False           # operator override of the master pre-test


@app.post("/api/storage-endpoints/{endpoint_id}/enable-witness")
def api_storage_endpoint_enable_witness(endpoint_id: str,
                                        req: EndpointActivateRequest =
                                        EndpointActivateRequest()):
    """The "Enable as fileshare-Witness" box: register a witness backed by this
    endpoint. The witness backend follows the endpoint type (s3 → native S3
    witness; smb/nfs → fileshare witness at the Bedrock-managed strict mount). netd
    (increment 4) resolves the endpoint to its S3 client / mountpoint and writes
    slots there; the slot protocol seals with the CLUSTER key, so no per-witness
    key is needed. Tests on the MASTER first (unless skip_test)."""
    cluster = load_cluster()
    ep = (cluster.get("storage_endpoints") or {}).get(endpoint_id)
    if ep is None:
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    typ = (ep.get("type") or "").lower()
    backend = "s3" if typ == "s3" else "fileshare"
    wid = (req.witness_id or f"wit-{endpoint_id}").strip()
    # Test-on-master-before-commit (the share must be usable here before we raise
    # the quorum bar cluster-wide). Reads the sealed creds from rqlite.
    if not req.skip_test:
        s3_secret = _bs.storage_endpoint_secret(endpoint_id, "s3_secret_key")
        fs_password = _bs.storage_endpoint_secret(endpoint_id, "fs_password")
        endpoint = dict(ep); endpoint["type"] = typ
        try:
            from lib import storage_mount as _sm
            ok, reason = _sm.test_endpoint(
                endpoint, _sm.WITNESS, username=ep.get("fs_username", ""),
                password=fs_password or "", s3_secret_key=s3_secret or "")
        except Exception as e:
            raise HTTPException(500, f"witness pre-test error: {e}")
        if not ok:
            raise HTTPException(
                400, f"endpoint {endpoint_id!r} failed the master witness test: "
                f"{reason}. Fix it before enabling as a witness (a witness that "
                f"can't be written would raise the quorum bar without ever voting).")
    try:
        rev = _bs.witness_register(witness_id=wid, addr="",
                                   witness_pubkey_hex="",
                                   encrypted_witness_key_hex="",
                                   backend=backend, endpoint_id=endpoint_id)
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"witness {wid!r} enabled on endpoint {endpoint_id!r} ({backend})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "witness_id": wid,
            "backend": backend, "endpoint_id": endpoint_id}


class EnableBackupRequest(BaseModel):
    target_id: str = ""
    # Optional per-repo encryption password. "" / None = the published PUBLIC
    # default (effectively-unencrypted); a real value opts this repo into encryption.
    encryption_password: Optional[str] = None
    skip_test: bool = False


@app.post("/api/storage-endpoints/{endpoint_id}/enable-backup")
def api_storage_endpoint_enable_backup(endpoint_id: str,
                                       req: EnableBackupRequest =
                                       EnableBackupRequest()):
    """The "Enable for backups" box: create a backup_target that REFERENCES this
    endpoint (endpoint_id) — the storage location + creds resolve from the endpoint
    at read time (view_builder fills s3_*/filesystem_path; backup_target_s3_creds
    reads the endpoint secret), so there is no duplicated/stale inline config. kind
    follows the endpoint type (s3 → kopia-s3; smb/nfs → kopia-fs at the managed
    cached mount /mnt/bedrock/kopia/<id>). Tests on the MASTER first unless skip_test."""
    cluster = load_cluster()
    ep = (cluster.get("storage_endpoints") or {}).get(endpoint_id)
    if ep is None:
        raise HTTPException(404, f"storage endpoint {endpoint_id!r} not found")
    typ = (ep.get("type") or "").lower()
    kind = "kopia-s3" if typ == "s3" else "kopia-fs"
    tid = (req.target_id or f"bk-{endpoint_id}").strip()
    if not req.skip_test:
        s3_secret = _bs.storage_endpoint_secret(endpoint_id, "s3_secret_key")
        fs_password = _bs.storage_endpoint_secret(endpoint_id, "fs_password")
        endpoint = dict(ep); endpoint["type"] = typ
        try:
            from lib import storage_mount as _sm
            ok, reason = _sm.test_endpoint(
                endpoint, _sm.KOPIA, username=ep.get("fs_username", ""),
                password=fs_password or "", s3_secret_key=s3_secret or "")
        except Exception as e:
            raise HTTPException(500, f"backup pre-test error: {e}")
        if not ok:
            raise HTTPException(
                400, f"endpoint {endpoint_id!r} failed the master backup test: "
                f"{reason}. Fix it before enabling for backups.")
    try:
        rev = _bs.backup_target_set(
            tid, kind, endpoint_id=endpoint_id,
            repo_password=(req.encryption_password or ""), reason="enable-backup")
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    push_log(f"backup target {tid!r} enabled on endpoint {endpoint_id!r} ({kind})",
             app="bedrock-mgmt", level="info")
    return {"status": "ok", "revision": rev, "target_id": tid, "kind": kind,
            "endpoint_id": endpoint_id}


@app.post("/api/vms/{vm_name}/backup")
async def api_vm_backup(vm_name: str, req: BackupRunRequest = BackupRunRequest()):
    """Take a backup of `vm_name` to `target_id`. Returns 202 + task_id;
    the UI watches /api/tasks (or the WS task channel) for completion.

    The backup runs on the VM's home node: take an LV snapshot, kopia
    snapshot create, drop the LV snapshot. Idempotent only at the
    log-entry level; multiple in-flight backups of the same VM are not
    serialised here — the operator is expected not to double-click."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    target = (cluster.get("backup_targets") or {}).get(req.target_id)
    if target is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    # Multi-target: resolve the primary's mirror set NOW (from cluster state)
    # and carry it in the saga params so the home node's sync_to_secondaries
    # step mirrors the backup. Putting it in params (vs re-reading on the home
    # node) makes it durable across resume + master failover.
    secondary_target_ids = list(target.get("sync_to") or [])

    # Submit a vm_backup saga targeted at the VM's HOME node. That node's
    # operations_drain (mgmt/orchestrator.py) runs it locally — kopia on
    # the node that owns the disks — recording the result to rqlite. No
    # SSH from here; rqlite's `operations` table is the channel.
    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_backup
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_backup", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "label": req.label or "",
                    "secondary_target_ids": secondary_target_ids},
            requested_by="api_vm_backup",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue backup: {e}")
    push_log(f"VM {vm_name}: backup queued → {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}


@app.get("/api/vms/{vm_name}/backups")
def api_vm_backups_list(vm_name: str):
    """Backup history for a VM, drawn from cluster state. Newest first."""
    cluster = load_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    return {
        "vm": vm_name,
        "backups": vm.get("backups") or [],
        "last_backup_error": vm.get("last_backup_error"),
        "last_restore": vm.get("last_restore"),
        "last_restore_error": vm.get("last_restore_error"),
    }


@app.post("/api/vms/{vm_name}/restore")
async def api_vm_restore(vm_name: str, req: RestoreRequest):
    """Restore a VM from a kopia backup and bring it back up HA. Submits
    a vm_restore saga targeted at the VM's home node; that node powers the
    VM off, restores each disk through its DRBD primary (so the bytes
    replicate to peers), and starts it again. Returns 202 + operation_id."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise HTTPException(404, f"VM {vm_name!r} not present in this cluster")
    home = vm.get("host") or ""
    if not home:
        raise HTTPException(400, f"VM {vm_name!r} has no home node recorded")

    import socket as _socket
    from bedrock_d.orchestrator.sagas import SagaExecutor
    from bedrock_d.orchestrator.sagas.rqlite_backend import RqliteSagaBackend
    from bedrock_d import state as _bst
    from bedrock_d.vm import backup as _vmbk  # noqa: F401  registers vm_restore
    try:
        backend = RqliteSagaBackend(_bst.RqliteClient())
        ex = SagaExecutor(backend=backend, this_node=_socket.gethostname())
        op_id = ex.submit(
            kind="vm_restore", target_node=home,
            params={"target_id": req.target_id, "vm_name": vm_name,
                    "kopia_snapshot_id": req.kopia_snapshot_id or ""},
            requested_by="api_vm_restore",
        )
    except Exception as e:
        raise HTTPException(500, f"could not queue restore: {e}")
    push_log(f"VM {vm_name}: restore queued from {req.target_id} "
             f"(op {op_id}, runs on {home})", level="info")
    return {"status": "accepted", "operation_id": op_id, "home_node": home}


class BackupScheduleSetRequest(BaseModel):
    target_id: str = "main"
    cron_expr: str               # 5-field UTC cron, e.g. "0 2 * * *" or "@daily"
    label_prefix: str = "auto"   # auto-generated labels start with "<prefix>-"
    retention_count: int = 0     # 0 = keep all (v1.0 default)
    reason: str = ""


@app.post("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_set(vm_name: str, req: BackupScheduleSetRequest):
    """Set or replace the periodic-backup schedule for a VM. The
    schedule is stored in the cluster log so it survives master
    failover; the master's `backup_scheduler` loop is the only firer.

    Returns the next 5 fire times (UTC) so the caller can sanity-check
    their cron expression before relying on it."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    import cron as _cron

    cluster = load_cluster()
    if (cluster.get("vms") or {}).get(vm_name) is None:
        raise HTTPException(404, f"VM {vm_name!r} not found")
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")

    # Validate the cron expression server-side. Better to fail at submit
    # time than have the scheduler silently skip the VM forever.
    try:
        next_fires = _cron.next_n(req.cron_expr, n=5)
    except _cron.CronError as e:
        raise HTTPException(400, f"invalid cron expression: {e}")

    try:
        rev = _bs.backup_schedule_set(
            vm=vm_name, target_id=req.target_id,
            cron_expr=req.cron_expr,
            label_prefix=req.label_prefix,
            retention_count=req.retention_count,
            reason=req.reason or "set via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")

    push_log(f"backup schedule set for VM {vm_name}: cron={req.cron_expr!r} "
             f"target={req.target_id}",
             app="bedrock-mgmt", level="info")
    return {
        "status": "ok",
        "revision": rev,
        "vm": vm_name,
        "cron_expr": req.cron_expr,
        "next_fires_utc": next_fires,
    }


@app.delete("/api/vms/{vm_name}/backup-schedule")
def api_vm_backup_schedule_remove(vm_name: str, reason: str = ""):
    try:
        rev = _bs.backup_schedule_removed(
            vm=vm_name, reason=reason or "removed via dashboard",
        )
    except Exception as e:
        raise HTTPException(500, f"rqlite write failed: {e}")
    return {"status": "ok", "revision": rev, "vm": vm_name}


@app.delete("/api/vms/{vm_name}/backups/{kopia_snapshot_id}")
def api_vm_backup_delete(vm_name: str, kopia_snapshot_id: str,
                         req: BackupDeleteRequest = BackupDeleteRequest()):
    """Delete one snapshot from the kopia repo. Returns synchronously —
    delete is fast (just drops a manifest). GC of the underlying chunks
    happens during the next `kopia maintenance run` on the master."""
    cluster = load_cluster()
    if (cluster.get("backup_targets") or {}).get(req.target_id) is None:
        raise HTTPException(400, f"backup target {req.target_id!r} not configured")
    backup = _import_backup_module()
    try:
        backup.delete_backup(req.target_id, kopia_snapshot_id, vm_name,
                             reason=req.reason or "operator-delete")
    except Exception as e:
        raise HTTPException(500, f"delete failed: {e}")
    return {"status": "ok", "kopia_snapshot_id": kopia_snapshot_id}


# ── VM action implementations ──────────────────────────────────────────────

def _vm_start(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"Unknown VM: {vm_name}")
    if vm["state"] == "running": raise HTTPException(400, "Already running")
    resource = vm.get("drbd_resource", "")
    nodes_cfg = get_nodes()

    target = None
    # Prefer node where DRBD is already Primary
    if resource:
        for nname, cfg in nodes_cfg.items():
            if state["nodes"][nname]["online"]:
                drbd = parse_drbd_status(state["nodes"][nname]["drbd_raw"])
                if resource in drbd and drbd[resource]["role"] == "Primary":
                    target = nname; break
    # Fallback: any defined node that's online
    if not target:
        for nname in vm.get("defined_on", []):
            if nname in state["nodes"] and state["nodes"][nname]["online"]:
                target = nname; break
    if not target:
        raise HTTPException(503, "No online node with this VM defined")

    # Promote DRBD if needed (cattle VMs have no DRBD)
    if resource:
        ssh_cmd_rc(nodes_cfg[target]["host"], f"drbdadm primary {resource}")

    out, rc = ssh_cmd_rc(nodes_cfg[target]["host"], f"virsh start {vm_name}")
    if rc != 0:
        # The exact virsh/qemu error is the most useful thing to keep — record it
        # as a per-VM event so it's findable later (the baseline lifecycle event
        # never fires here because the state didn't change).
        _events.emit("vm_error", f"VM {vm_name} start FAILED on {target}: {out}",
                     vm=vm_name, node=target, level="error", op="start", error=out)
        raise HTTPException(500, f"Failed: {out}")
    _events.emit("vm_lifecycle", f"operator started {vm_name} on {target}",
                 vm=vm_name, node=target, reason="operator", op="start")
    return {"status": "started", "node": target}


def _vm_shutdown(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm["state"] != "running": raise HTTPException(400, "Not running")
    nodes_cfg = get_nodes()
    on = vm["running_on"]
    out, rc = ssh_cmd_rc(nodes_cfg[on]["host"], f"virsh shutdown {vm_name}")
    if rc != 0:
        _events.emit("vm_error", f"VM {vm_name} shutdown FAILED on {on}: {out}",
                     vm=vm_name, node=on, level="error", op="shutdown", error=out)
        raise HTTPException(500, f"Failed: {out}")
    _events.emit("vm_lifecycle", f"operator shutdown {vm_name} on {on}",
                 vm=vm_name, node=on, reason="operator", op="shutdown")
    return {"status": "shutdown sent"}


def _vm_poweroff(vm_name: str) -> dict:
    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm or vm["state"] != "running": raise HTTPException(400, "Not running")
    nodes_cfg = get_nodes()
    on = vm["running_on"]
    out, rc = ssh_cmd_rc(nodes_cfg[on]["host"], f"virsh destroy {vm_name}")
    if rc != 0:
        _events.emit("vm_error", f"VM {vm_name} poweroff FAILED on {on}: {out}",
                     vm=vm_name, node=on, level="error", op="poweroff", error=out)
        raise HTTPException(500, f"Failed: {out}")
    _events.emit("vm_lifecycle", f"operator powered off {vm_name} on {on}",
                 vm=vm_name, node=on, reason="operator", op="poweroff")
    return {"status": "powered off"}


# ── Workload conversion (cattle ↔ pet ↔ vipet) ──────────────────────────────

def _vm_disk_vg(host: str) -> str:
    """Return the LVM VG bedrock uses on `host` for thin LVs (tiers,
    VM disks, all the dynamic ones). Reads /etc/bedrock/storage.json
    which `tier_storage.ensure_vg()` writes at bootstrap time. Falls
    back to detecting the only VG present, then to the literal name
    `bedrock`. No loop-file fallback: bedrock-bootstrap is expected to
    have set up the layout. If it hasn't, downstream lvcreate calls fail
    loudly — the right reaction is to fix the install, not to silently
    put VM data on a sparse file on `/`."""
    try:
        out = ssh_cmd(host,
            "cat /etc/bedrock/storage.json 2>/dev/null", timeout=8)
        if out.strip():
            import json as _json
            cfg = _json.loads(out)
            if cfg.get("vg"):
                return cfg["vg"]
    except Exception:
        pass
    try:
        vgs = ssh_cmd(host,
            "vgs --noheadings -o vg_name 2>/dev/null", timeout=10).split()
        vgs = [v.strip() for v in vgs if v.strip()]
        if len(vgs) == 1:
            return vgs[0]
        # Prefer `bedrock-vg`, then `bedrock` (mirrors
        # tier_storage.detect_vg's multi-VG heuristic).
        if "bedrock-vg" in vgs:
            return "bedrock-vg"
        if "bedrock" in vgs:
            return "bedrock"
    except Exception:
        pass
    return "bedrock"


def _ensure_thinpool(host: str, vg_name: Optional[str] = None, pool: str = "thinpool"):
    """Verify the thin pool exists on `host`. Creating it is the
    responsibility of `bedrock bootstrap` (tier_storage.ensure_thinpool),
    NOT this runtime helper — runtime is the wrong moment to make
    architectural-level storage decisions. If the pool isn't there,
    raise so the operator gets a clear failure pointing at the missing
    install step."""
    if vg_name is None:
        vg_name = _vm_disk_vg(host)
    out = ssh_cmd(host, f"lvs --noheadings -o lv_name {vg_name} 2>/dev/null || true")
    if pool in out.split():
        return
    raise HTTPException(
        500,
        f"thin pool {vg_name}/{pool} does not exist on {host}. "
        f"Run `bedrock storage init` (or re-run `bedrock bootstrap`) on that "
        f"node before creating VMs. Bedrock no longer auto-creates loop-backed "
        f"thin pools at runtime — that path put VM I/O on `/` and filled the "
        f"root filesystem during multi-GB installs.")


def _find_vm_disk(host: str, vm_name: str) -> dict:
    """Return {target, source_dev} for the VM's primary block disk."""
    xml = ssh_cmd(host, f"virsh dumpxml {vm_name}")
    import re as _re
    for m in _re.finditer(r"<disk\b[^>]*type=['\"]block['\"][^>]*>(.*?)</disk>",
                          xml, _re.DOTALL):
        chunk = m.group(1)
        src = _re.search(r"<source\s+dev=['\"]([^'\"]+)['\"]", chunk)
        tgt = _re.search(r"<target\s+dev=['\"]([^'\"]+)['\"]", chunk)
        if src and tgt:
            return {"target": tgt.group(1), "source_dev": src.group(1)}
    raise HTTPException(500, f"Cannot find block disk for {vm_name}")


# Process-local reservation set for DRBD minors chosen by in-flight
# converts that haven't yet created their /dev/drbdN. Without this, two
# parallel converts both query `ls /dev/drbd*`, both see "nothing here in
# the target range", both pick the same minor, and one fails at
# `drbdadm create-md` / `up`. The lock below serialises the pick+reserve.
_drbd_minor_lock = threading.Lock()
_drbd_minor_reserved: set[int] = set()


def _next_drbd_minor(hosts: list) -> int:
    """Pick + atomically reserve an unused minor in the VM band
    (1102..1189) across all hosts. The band keeps every VM-disk DRBD
    port inside 7700-7799 (drbd_port_for) and clear of the singleton
    minor (1101) + the netd mesh minors (1132/1133/1134 → UDP probe
    7732, advert 7733, election heartbeat 7734=netd.HB_PORT). The
    reservation lives until `_release_drbd_minor` is called (after the
    resource is fully up, or on rollback)."""
    reserved_minors = {1132, 1133, 1134}
    with _drbd_minor_lock:
        used = set(_drbd_minor_reserved)
        for h in hosts:
            out = ssh_cmd(h, "ls /dev/drbd* 2>/dev/null | grep -oE '[0-9]+$' || true")
            for n in out.split():
                try: used.add(int(n))
                except ValueError: pass
        for i in range(1102, 1190):
            if i not in used and i not in reserved_minors:
                _drbd_minor_reserved.add(i)
                return i
    raise HTTPException(500, "No free DRBD minor")


def _release_drbd_minor(minor: int):
    """Drop the in-process reservation. Called after the DRBD device is up
    (the ssh-ls check will now see /dev/drbdN directly) OR on rollback."""
    with _drbd_minor_lock:
        _drbd_minor_reserved.discard(minor)


def _lv_bytes(host: str, lv_path: str) -> int:
    """Block device size in bytes. Returns 0 if the device doesn't exist
    or blockdev returned nothing — callers (e.g. the silent-truncation
    guard) treat a zero result as "something is wrong, fail loud"."""
    out = ssh_cmd(host, f"blockdev --getsize64 {lv_path} 2>/dev/null || echo 0")
    try:
        return int(out.strip() or "0")
    except (ValueError, AttributeError):
        return 0


def _gen_drbd_res(resource: str, minor: int, peers: list) -> str:
    """peers: list of (node_name, loopback_ip, lv_path, meta_lv_path). 2 or 3 entries.
    External meta-disk keeps the DRBD device the same size as the data LV,
    so virsh blockcopy can pivot 1:1 without size mismatch.
    """
    # Shared DRBD port formula (7700-7799 band) — same mapping the
    # cluster singleton + the VM sagas use. See drbd_config.drbd_port_for.
    import sys as _sys
    _sys.path.insert(0, "/usr/local/lib/bedrock")
    from bedrock_d.vm import drbd_config as _cfg
    port = _cfg.drbd_port_for(minor)
    lines = [f"resource {resource} {{",
             "    protocol C;",
             "    net { allow-two-primaries no; after-sb-0pri discard-zero-changes;",
             "          after-sb-1pri discard-secondary; after-sb-2pri disconnect; }"]
    for i, (name, ip, lv, meta) in enumerate(peers):
        lines.append(f"    on {name} {{ node-id {i}; device /dev/drbd{minor}; "
                     f"disk {lv}; address {ip}:{port}; meta-disk {meta}; }}")
    if len(peers) == 2:
        lines.append(f"    connection {{ host {peers[0][0]}; host {peers[1][0]}; }}")
    else:
        lines.append("    connection-mesh { hosts " +
                     " ".join(p[0] for p in peers) + "; }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _write_drbd_res(hosts: list, resource: str, content: str):
    """Write /etc/drbd.d/<resource>.res on all hosts via SSH. The file name
    matches the resource name so one VM can have multiple .res files
    (vm-foo-disk0.res, vm-foo-disk1.res)."""
    import base64
    b64 = base64.b64encode(content.encode()).decode()
    path = f"/etc/drbd.d/{resource}.res"
    for h in hosts:
        ssh_cmd(h, f"echo {b64} | base64 -d > {path}")


def _vm_set_ha_level(vm_name: str, vm_type: str, peer_nodes=None,
                task: Optional[Task] = None) -> dict:
    if vm_type not in ("cattle", "pet", "vipet"):
        raise HTTPException(400, f"Invalid vm_type: {vm_type}")

    state = build_cluster_state()
    vm = state["vms"].get(vm_name)
    if not vm: raise HTTPException(404, f"VM {vm_name} not found")

    nodes_cfg = get_nodes()
    # Running → use running_on; offline → first defined_on node.
    src_name = (vm.get("running_on")
                or (vm.get("defined_on") or [None])[0])
    if not src_name:
        raise HTTPException(400,
            f"Cannot resolve home node for {vm_name}")
    is_running = (vm.get("state") == "running")
    src = nodes_cfg[src_name]
    current_type = "vipet" if vm.get("drbd_resource") and _count_drbd_peers(src["host"], vm["drbd_resource"]) >= 3 \
                   else ("pet" if vm.get("drbd_resource") else "cattle")

    if current_type == vm_type:
        return {"status": "no-op", "current": current_type}

    rank = {"cattle": 0, "pet": 1, "vipet": 2}
    if rank[vm_type] > rank[current_type]:
        return _vm_set_ha_level_up(vm_name, current_type, vm_type, src_name,
                                   peer_nodes, task, is_running=is_running)
    else:
        return _vm_set_ha_level_down(vm_name, current_type, vm_type, src_name,
                                     peer_nodes, task)


def _count_drbd_peers(host: str, resource: str) -> int:
    try:
        out = ssh_cmd(host, f"drbdsetup status {resource} --json 2>/dev/null || echo '[]'")
        import json as _json
        data = _json.loads(out)
        if isinstance(data, list) and data:
            return 1 + len(data[0].get("connections", []))
    except Exception: pass
    return 0


def _vm_set_ha_level_up(vm_name: str, cur: str, tgt: str, src_name: str,
                         peer_nodes, task: Optional[Task] = None,
                         is_running: bool = True) -> dict:
    """Cattle → pet / cattle → ViPet / pet → ViPet.

    Iterates over every disk the VM has, so multi-disk guests become
    pet/ViPet across ALL their disks. Atomic: if any disk fails mid-way,
    rollback unwinds the changes already made to earlier disks.

    Two execution paths:

      - **online** (`is_running=True`): use `virsh blockcopy ...
        --pivot` to swap qemu's disk reference from the local LV to
        /dev/drbdN with no guest pause. Required for "convert this
        VM to HA without downtime" — the operator never reboots.

      - **offline** (`is_running=False`): the VM is shut off, so no
        qemu is holding the LV. Skip blockcopy; directly rewrite the
        persistent libvirt XML to point at /dev/drbdN, redefine on
        all peers. DRBD does its own initial sync from primary
        (this side, marked `--force`) to the peer's empty LV in the
        background. Faster, no live-migration risk, and matches the
        operator expectation that "convert" should also work for VMs
        that haven't been booted yet."""
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]

    need_peers = {"pet": 1, "vipet": 2}[tgt]
    available = [n for n in nodes_cfg if n != src_name]

    # Enumerate disks the VM actually has. Works for cattle (plain LVs) and
    # for pet→ViPet (DRBD devices). cdroms are excluded by get_vm_disks.
    disks = get_vm_disks(src["host"], vm_name)
    if not disks:
        raise HTTPException(500, f"No disks found on VM {vm_name}")

    if cur == "cattle":
        chosen = (peer_nodes or available)[:need_peers]
        if len(chosen) < need_peers:
            raise HTTPException(400, f"{tgt} needs {need_peers} peers, have {len(chosen)}")

        # No artificial pool-fill guard here. Convert is sometimes
        # exactly the operation an operator runs to free up space
        # (move a VM off a tight node), so refusing it on a soft
        # threshold would block a legitimate use case. The thin pool
        # itself is the safety net — lvcreate fails loudly if there
        # really isn't room. The supportability dashboard surfaces
        # the 80% warning where it belongs (advisory monitoring),
        # not as a write-block.

        # Track what we created so we can unwind on failure
        created: list[dict] = []  # [{resource, hosts: [host, lv, meta], target_dev}]
        # Targets we started a blockcopy on; need `virsh blockjob --abort` if it
        # was interrupted, otherwise libvirt keeps disk->blockjob set and all
        # future blockcopies on this disk fail with "already in active block
        # job" until the daemon restarts.
        copy_started: list[str] = []

        def _unwind():
            # First: abort any blockcopy that failed mid-flight, so libvirt
            # clears disk->blockjob. The pivot was never reached (blockcopy
            # raised) so the VM is still on its original LV.
            for tgt in copy_started:
                ssh_cmd_rc(src["host"],
                    f"virsh blockjob {vm_name} {tgt} --abort 2>&1 || true",
                    timeout=15)
                ssh_cmd_rc(src["host"],
                    f"virsh blockjob {vm_name} {tgt} --abort --async 2>&1 || true",
                    timeout=15)
            for c in reversed(created):
                for h, lv, meta in c["hosts"]:
                    ssh_cmd_rc(h, f"drbdadm down {c['resource']} 2>&1 || true", timeout=15)
                    ssh_cmd_rc(h, f"drbdadm wipe-md --force {c['resource']} 2>&1 || true", timeout=15)
                    ssh_cmd_rc(h, f"rm -f /etc/drbd.d/{c['resource']}.res", timeout=5)
                    rm_paths = " ".join(p for p in (lv, meta) if p and "-meta" in (meta or ""))
                    if rm_paths:
                        ssh_cmd_rc(h, f"lvremove -f {rm_paths} 2>&1 || true", timeout=30)
                # Release the minor reservation so another concurrent convert
                # can use it (or its number — this one). Safe regardless of
                # whether the create-md / up calls even ran.
                if "minor" in c:
                    _release_drbd_minor(c["minor"])

        t_start = time.time()
        try:
            converted_disks = []
            for i, disk in enumerate(disks):
                src_lv = disk["backing_lv"]
                target_dev = disk["target"]
                lv_name = src_lv.split("/")[-1]
                vg_name = src_lv.split("/")[-2]
                resource = f"vm-{vm_name}-disk{i}"
                # Meta LV name must be unique per resource; the `<lv>-meta`
                # suffix is the convention _parse_drbd_res expects.
                meta_lv_name = f"{lv_name}-meta"
                meta_path = f"/dev/{vg_name}/{meta_lv_name}"

                src_size = _lv_bytes(src["host"], src_lv)
                size_mb = (src_size + 1024*1024 - 1) // (1024*1024)
                # DRBD 9 external metadata size (max-peers=7):
                #   superblock   = 4 KB
                #   bitmap       = 1 bit per 4 KB of data × max_peers
                #                ≈ 1.5 MB per GB of data at max_peers=7
                #   activity log = 32 MB (default)
                #   safety       = 2× headroom
                # Formula: 32 MB base + 2 MB per GB of data. Thin-provisioned
                # so only actually-used meta blocks allocate.
                # Note: DRBD doesn't error on an undersized meta LV — it
                # silently truncates /dev/drbdN to whatever fits. The
                # silent-truncation guard after `drbdadm up` asserts
                # /dev/drbdN size == backing LV size before blockcopy runs,
                # so any future regression here fails loud, pre-pivot.
                size_gb = (src_size + (1 << 30) - 1) >> 30
                meta_mb = max(32, 32 + size_gb * 2)

                step_prefix = f"disk{i} ({target_dev})"
                if task: task.step_start(f"{step_prefix}: create meta LV on source")

                # 1. Create external metadata LV on source for this disk
                ssh_cmd(src["host"],
                        f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                        f"-n {meta_lv_name} -y 2>&1 || true", timeout=30)

                # 2. Create matching data + meta LV on each peer
                peers_info = [(src_name, src.get("loopback_ip") or src["host"],
                               src_lv, meta_path)]
                for pname in chosen:
                    p = nodes_cfg[pname]
                    _ensure_thinpool(p["host"], vg_name)
                    ssh_cmd(p["host"],
                            f"lvcreate -V {size_mb}M -T {vg_name}/thinpool "
                            f"-n {lv_name} -y", timeout=30)
                    ssh_cmd(p["host"],
                            f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                            f"-n {meta_lv_name} -y", timeout=30)
                    peers_info.append((pname, p.get("loopback_ip") or p["host"],
                                       f"/dev/{vg_name}/{lv_name}",
                                       f"/dev/{vg_name}/{meta_lv_name}"))
                all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
                # record for unwind: hosts + the peer LV paths (we don't
                # remove the source-side original LV — blockcopy will
                # repoint the VM away from it but the original LV stays)
                created.append({
                    "resource": resource,
                    "hosts": [(nodes_cfg[n]["host"],
                               f"/dev/{vg_name}/{lv_name}" if n != src_name else "",
                               f"/dev/{vg_name}/{meta_lv_name}")
                              for n, _, _, _ in peers_info],
                })
                if task: task.step_done(f"{step_prefix}: create meta LV on source")

                if task: task.step_start(f"{step_prefix}: generate DRBD res")
                minor = _next_drbd_minor(all_hosts)
                # Record the minor on the `created` entry so _unwind can
                # release the reservation on failure.
                created[-1]["minor"] = minor
                res_text = _gen_drbd_res(resource, minor, peers_info)
                _write_drbd_res(all_hosts, resource, res_text)
                if task: task.step_done(f"{step_prefix}: generate DRBD res")

                if task: task.step_start(f"{step_prefix}: create-md + up")
                for h in all_hosts:
                    ssh_cmd(h, f"drbdadm create-md --force --max-peers=7 "
                               f"{resource}", timeout=30)
                    ssh_cmd(h, f"drbdadm up {resource}", timeout=30)
                ssh_cmd(src["host"], f"drbdadm primary --force {resource}",
                        timeout=30)
                if task: task.step_done(f"{step_prefix}: create-md + up")

                # SILENT-TRUNCATION GUARD.
                # DRBD silently shrinks the effective /dev/drbdN if the meta
                # LV is too small, if internal meta is used by mistake, or on
                # any other failure path we haven't anticipated. No error,
                # just a shorter device — the blockcopy pivot would then fail
                # with "Copy failed" at 0 % (destination < source). Assert
                # equality HERE so a mismatch is caught before blockcopy
                # touches anything, and with the real byte counts in the log
                # so operators see exactly what went wrong.
                if task: task.step_start(f"{step_prefix}: assert /dev/drbd{minor} == backing LV")
                drbd_bytes = _lv_bytes(src["host"], f"/dev/drbd{minor}")
                if drbd_bytes != src_size:
                    msg = (f"DRBD silent-truncation guard tripped on {resource}: "
                           f"/dev/drbd{minor} = {drbd_bytes} bytes, "
                           f"backing LV = {src_size} bytes (delta "
                           f"{src_size - drbd_bytes} bytes). Meta LV almost "
                           f"certainly too small — check meta_mb formula.")
                    if task: task.step_fail(
                        f"{step_prefix}: assert /dev/drbd{minor} == backing LV", msg)
                    raise HTTPException(500, msg)
                if task: task.step_done(
                    f"{step_prefix}: assert /dev/drbd{minor} == backing LV")

                if is_running:
                    if task: task.step_start(f"{step_prefix}: blockcopy → /dev/drbd{minor}")
                    # Belt-and-braces: clear any stale libvirt blockjob state on
                    # this disk before we start. No-op if nothing is pending.
                    ssh_cmd_rc(src["host"],
                        f"virsh blockjob {vm_name} {target_dev} --abort 2>&1 || true",
                        timeout=10)
                    copy_started.append(target_dev)
                    out, rc = ssh_cmd_rc(src["host"],
                        f"virsh blockcopy {vm_name} {target_dev} /dev/drbd{minor} "
                        f"--reuse-external --wait --pivot --verbose "
                        f"--transient-job --blockdev --format raw", timeout=1800)
                    if rc != 0:
                        if task: task.step_fail(f"{step_prefix}: blockcopy → /dev/drbd{minor}",
                                                f"rc={rc}: {out[-400:]}")
                        raise HTTPException(500, f"blockcopy failed on disk{i}: {out}")
                    # Blockcopy succeeded + pivoted → target_dev is no longer in
                    # the `needs-abort` set (pivot drops the mirror).
                    if target_dev in copy_started:
                        copy_started.remove(target_dev)
                    if task: task.step_done(f"{step_prefix}: blockcopy → /dev/drbd{minor}")
                else:
                    # Offline path: rewrite this disk's <source dev='…'> in
                    # the persistent XML on the source. DRBD's local side is
                    # already primary --force on the live data LV, so no
                    # data copy is needed locally — DRBD's initial-sync from
                    # primary streams to peers in the background. The VM,
                    # when restarted, opens /dev/drbdN and reads the same
                    # bytes through the replication layer.
                    if task: task.step_start(f"{step_prefix}: rewrite XML offline")
                    xml_text = ssh_cmd(src["host"],
                        f"virsh dumpxml --inactive {vm_name}", timeout=15)
                    needle = f"source dev='{src_lv}'"
                    if needle not in xml_text:
                        # Try double-quoted variant (libvirt may emit either)
                        needle_dq = f'source dev="{src_lv}"'
                        if needle_dq not in xml_text:
                            raise HTTPException(500,
                                f"could not find {src_lv!r} in {vm_name}'s "
                                f"persistent XML — XML schema unexpected")
                        new_xml = xml_text.replace(
                            needle_dq, f'source dev="/dev/drbd{minor}"')
                    else:
                        new_xml = xml_text.replace(
                            needle, f"source dev='/dev/drbd{minor}'")
                    import base64 as _b64
                    xml_b64 = _b64.b64encode(new_xml.encode()).decode()
                    ssh_cmd(src["host"],
                        f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                        f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
                    if task: task.step_done(f"{step_prefix}: rewrite XML offline")

                converted_disks.append({"index": i, "target": target_dev,
                                        "resource": resource, "minor": minor})
                # DRBD device is now live cluster-wide; future ssh-ls checks
                # will see /dev/drbd{minor} directly — drop the reservation.
                _release_drbd_minor(minor)

            # After all disks succeed: define VM on peers so migration works.
            if task: task.step_start("define VM on peers")
            xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
            import base64 as _b64
            xml_b64 = _b64.b64encode(xml_text.encode()).decode()
            for pname in chosen:
                ph = nodes_cfg[pname]["host"]
                ssh_cmd(ph, f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                            f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
            if task: task.step_done("define VM on peers")

            dur = round(time.time() - t_start, 2)
            push_log(f"Convert {vm_name}: {cur} → {tgt} in {dur}s "
                     f"({len(converted_disks)} disk(s))",
                     node=src_name, app="bedrock-mgmt", level="info")
            return {"status": "converted", "from": cur, "to": tgt,
                    "disks": converted_disks, "duration_s": dur,
                    "peers": [src_name] + chosen}
        except Exception as e:
            push_log(f"Convert {vm_name}: FAILED ({e}) — unwinding",
                     node=src_name, app="bedrock-mgmt", level="error")
            _unwind()
            raise

    elif cur == "pet" and tgt == "vipet":
        # Add a third peer to every existing DRBD resource the VM has.
        resources = [d["drbd_resource"] for d in disks if d.get("drbd_resource")]
        if not resources:
            raise HTTPException(500, f"No DRBD resources found on {vm_name}")

        chosen = peer_nodes or []
        if not chosen:
            # Pick a node not already in the first resource's peer list
            first_existing = _parse_drbd_res(src["host"], resources[0]) or {}
            chosen = [n for n in available if n not in first_existing.get("peers", [])][:1]
        if not chosen:
            raise HTTPException(400, "vipet needs a third peer")
        new_peer = chosen[0]
        p = nodes_cfg[new_peer]

        added = []
        t_start = time.time()
        for i, resource in enumerate(resources):
            existing = _parse_drbd_res(src["host"], resource)
            if not existing:
                raise HTTPException(500, f"Cannot parse existing {resource}")
            vg_name = existing["lv_vg"]
            lv_name = existing["lv_name"]
            meta_lv_name = f"{lv_name}-meta"
            size_mb = (existing["size_bytes"] + 1024*1024 - 1) // (1024*1024)
            # Meta LV sized to match the other peers — see _vm_set_ha_level_up
            # cattle→pet path for the formula derivation.
            size_gb = (existing["size_bytes"] + (1 << 30) - 1) >> 30
            meta_mb = max(32, 32 + size_gb * 2)

            step_prefix = f"disk{i} ({resource})"
            if task: task.step_start(f"{step_prefix}: LVs on new peer {new_peer}")
            _ensure_thinpool(p["host"], vg_name)
            ssh_cmd(p["host"], f"lvcreate -V {size_mb}M -T {vg_name}/thinpool "
                               f"-n {lv_name} -y", timeout=30)
            ssh_cmd(p["host"], f"lvcreate -V {meta_mb}M -T {vg_name}/thinpool "
                               f"-n {meta_lv_name} -y", timeout=30)
            if task: task.step_done(f"{step_prefix}: LVs on new peer {new_peer}")

            peers_info = [(n, nodes_cfg[n].get("loopback_ip") or nodes_cfg[n]["host"],
                           existing["lv_path"], existing["meta_path"])
                          for n in existing["peers"]]
            peers_info.append((new_peer, p.get("loopback_ip") or p["host"],
                               f"/dev/{vg_name}/{lv_name}",
                               f"/dev/{vg_name}/{meta_lv_name}"))
            minor = existing["minor"]
            res_text = _gen_drbd_res(resource, minor, peers_info)
            all_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in peers_info]
            _write_drbd_res(all_hosts, resource, res_text)

            if task: task.step_start(f"{step_prefix}: create-md + adjust")
            ssh_cmd(p["host"], f"drbdadm create-md --force --max-peers=7 "
                               f"{resource}", timeout=30)
            for h in all_hosts:
                ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)
            ssh_cmd(p["host"], f"drbdadm up {resource}", timeout=30)
            if task: task.step_done(f"{step_prefix}: create-md + adjust")
            added.append(resource)

        # Define VM on new peer (once; shared XML for all disks)
        if task: task.step_start(f"define VM on new peer {new_peer}")
        xml_text = ssh_cmd(src["host"], f"virsh dumpxml {vm_name}", timeout=15)
        import base64 as _b64
        xml_b64 = _b64.b64encode(xml_text.encode()).decode()
        ssh_cmd(p["host"], f"echo {xml_b64} | base64 -d > /tmp/{vm_name}.xml && "
                            f"virsh define /tmp/{vm_name}.xml >/dev/null", timeout=15)
        if task: task.step_done(f"define VM on new peer {new_peer}")

        dur = round(time.time() - t_start, 2)
        push_log(f"Convert {vm_name}: pet → vipet in {dur}s "
                 f"({len(added)} resource(s) added peer {new_peer})",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt,
                "resources": added, "added_peer": new_peer,
                "duration_s": dur}


def _vm_set_ha_level_down(vm_name: str, cur: str, tgt: str, src_name: str,
                           peer_nodes, task: Optional[Task] = None) -> dict:
    """ViPet → pet / pet → cattle / ViPet → cattle. Iterates over every
    DRBD resource the VM has (one per disk)."""
    nodes_cfg = get_nodes()
    src = nodes_cfg[src_name]
    disks = get_vm_disks(src["host"], vm_name)
    resources = [d["drbd_resource"] for d in disks if d.get("drbd_resource")]
    if not resources:
        raise HTTPException(500, f"No DRBD resources found on {vm_name}")

    if cur == "vipet" and tgt == "pet":
        # Pick one peer to drop (not src). Use first resource's peer list
        # to make the choice; we'll drop the same peer from every resource.
        first_existing = _parse_drbd_res(src["host"], resources[0]) or {}
        candidates = [n for n in first_existing.get("peers", []) if n != src_name]
        drop_name = (peer_nodes[0] if peer_nodes else (candidates[0] if candidates else None))
        if not drop_name or drop_name == src_name:
            raise HTTPException(400, "Cannot drop primary / no drop candidate")
        drop = nodes_cfg[drop_name]

        # 1. Undefine VM on dropped peer (once for all disks)
        if task: task.step_start(f"undefine VM on {drop_name}")
        ssh_cmd(drop["host"], f"virsh undefine {vm_name} 2>&1 || true", timeout=15)
        if task: task.step_done(f"undefine VM on {drop_name}")

        # 2. Per-resource: tear down DRBD on drop, rewrite config on kept, remove LVs
        for i, resource in enumerate(resources):
            existing = _parse_drbd_res(src["host"], resource)
            if not existing: continue
            step_prefix = f"disk{i} ({resource})"

            if task: task.step_start(f"{step_prefix}: drop DRBD on {drop_name}")
            ssh_cmd(drop["host"], f"drbdadm down {resource} 2>&1 || true", timeout=30)
            ssh_cmd(drop["host"], f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)

            remaining = [(n, nodes_cfg[n].get("loopback_ip") or nodes_cfg[n]["host"],
                          existing["lv_path"], existing["meta_path"])
                         for n in existing["peers"] if n != drop_name]
            minor = existing["minor"]
            res_text = _gen_drbd_res(resource, minor, remaining)
            kept_hosts = [nodes_cfg[n]["host"] for n, _, _, _ in remaining]
            _write_drbd_res(kept_hosts, resource, res_text)
            ssh_cmd(drop["host"], f"rm -f /etc/drbd.d/{resource}.res", timeout=10)

            drop_idx = existing["peers"].index(drop_name)
            for h in kept_hosts:
                ssh_cmd(h, f"drbdsetup disconnect {resource} {drop_idx} --force 2>&1 || true", timeout=15)
                ssh_cmd(h, f"drbdsetup del-peer {resource} {drop_idx} --force 2>&1 || true", timeout=15)
                ssh_cmd(h, f"drbdadm adjust {resource} 2>&1 || true", timeout=30)

            ssh_cmd(drop["host"],
                    f"lvremove -f {existing['lv_path']} {existing['meta_path']} 2>&1 || true",
                    timeout=30)
            if task: task.step_done(f"{step_prefix}: drop DRBD on {drop_name}")

        push_log(f"Convert {vm_name}: vipet → pet (dropped {drop_name}, "
                 f"{len(resources)} resource(s))",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt,
                "dropped": drop_name, "resources": resources}

    elif cur in ("pet", "vipet") and tgt == "cattle":
        # Pivot every DRBD device back to its raw LV, tear down DRBD, drop peer LVs.
        t_start = time.time()
        # Collect all peers affected across all resources (they should overlap).
        all_peer_names: set[str] = set()
        per_resource: list[dict] = []
        for r in resources:
            existing = _parse_drbd_res(src["host"], r)
            if not existing:
                raise HTTPException(500, f"Cannot parse {r}")
            per_resource.append({"resource": r, "existing": existing})
            all_peer_names.update(existing["peers"])

        # Pivot each disk from /dev/drbdN → raw LV (same backing bytes)
        for i, pr in enumerate(per_resource):
            existing = pr["existing"]
            # Find the disk in the VM XML that matches this resource's minor
            target_dev = None
            for d in disks:
                if d.get("drbd_minor") == existing["minor"]:
                    target_dev = d["target"]; break
            if target_dev is None:
                raise HTTPException(500, f"Cannot match disk for resource {pr['resource']}")
            step_prefix = f"disk{i} ({pr['resource']})"
            if task: task.step_start(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}")
            out, rc = ssh_cmd_rc(src["host"],
                f"virsh blockcopy {vm_name} {target_dev} {existing['lv_path']} "
                f"--reuse-external --wait --pivot --verbose --transient-job "
                f"--blockdev --format raw", timeout=1800)
            if rc != 0:
                if task: task.step_fail(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}",
                                        f"rc={rc}: {out[-400:]}")
                raise HTTPException(500, f"blockcopy pivot failed on {pr['resource']}: {out}")
            if task: task.step_done(f"{step_prefix}: pivot {target_dev} → {existing['lv_path']}")

        # Undefine VM on non-primary peers (once)
        for n in all_peer_names:
            if n == src_name: continue
            if n not in nodes_cfg: continue
            ssh_cmd(nodes_cfg[n]["host"], f"virsh undefine {vm_name} 2>&1 || true", timeout=15)

        # For every resource, tear DRBD down on every peer, remove peer data LVs,
        # remove only meta on primary (data LV IS the VM disk now).
        for i, pr in enumerate(per_resource):
            existing = pr["existing"]
            resource = pr["resource"]
            step_prefix = f"disk{i} ({resource})"
            if task: task.step_start(f"{step_prefix}: tear DRBD down + remove LVs")
            for n in existing["peers"]:
                if n not in nodes_cfg: continue
                h = nodes_cfg[n]["host"]
                ssh_cmd(h, f"drbdadm down {resource} 2>&1 || true", timeout=30)
                ssh_cmd(h, f"drbdadm wipe-md --force {resource} 2>&1 || true", timeout=30)
                ssh_cmd(h, f"rm -f /etc/drbd.d/{resource}.res", timeout=10)
                if n == src_name:
                    ssh_cmd(h, f"lvremove -f {existing['meta_path']} 2>&1 || true", timeout=30)
                else:
                    ssh_cmd(h, f"lvremove -f {existing['lv_path']} "
                               f"{existing['meta_path']} 2>&1 || true", timeout=30)
            if task: task.step_done(f"{step_prefix}: tear DRBD down + remove LVs")

        dur = round(time.time() - t_start, 2)

        push_log(f"Convert {vm_name}: {cur} → cattle in {dur}s",
                 node=src_name, app="bedrock-mgmt", level="info")
        return {"status": "converted", "from": cur, "to": tgt, "duration_s": dur}


def _parse_drbd_res(host: str, resource: str) -> dict:
    """Parse /etc/drbd.d/<resource>.res for peers, LV path, meta path, minor, size."""
    try:
        txt = ssh_cmd(host, f"cat /etc/drbd.d/{resource}.res 2>/dev/null")
    except Exception:
        return {}
    import re as _re
    peers, lv_path, meta_path, minor = [], "", "", 0
    for m in _re.finditer(
        r"on\s+(\S+)\s*\{[^}]*device\s+/dev/drbd(\d+)[^}]*disk\s+(\S+);[^}]*"
        r"meta-disk\s+(\S+);", txt, _re.DOTALL):
        peers.append(m.group(1))
        minor = int(m.group(2))
        lv_path = m.group(3)
        meta_path = m.group(4)
    if not lv_path:
        return {}
    parts = lv_path.split("/")
    lv_name, vg_name = parts[-1], parts[-2]
    try:
        size = _lv_bytes(host, lv_path)
    except Exception:
        size = 0
    return {"peers": peers, "lv_path": lv_path, "lv_name": lv_name,
            "lv_vg": vg_name, "meta_path": meta_path,
            "minor": minor, "size_bytes": size}


# ── VM creation (cattle, optionally ISO-booted) ─────────────────────────────

_VM_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
_VALID_PRIORITIES = ("low", "normal", "high")
# Maps priority → libvirt cpu_shares (cgroup weight; default is 1024).
# Powers of 2 on either side so the relative weights are clearly visible.
PRIORITY_CPU_SHARES = {"low": 256, "normal": 1024, "high": 4096}
ISO_MOUNT_DIR = "/mnt/bedrock/iso"  # identical on every cluster node (SeaweedFS FUSE)


def _vm_create_from_import(meta: dict, req, task: Optional[Task] = None) -> dict:
    """Turn a converted import (qcow2 on mgmt node) into a cattle VM.
    Creates a thin LV sized to the qcow2 virtual size, qemu-img converts the
    qcow2 into the LV (raw), then virt-installs with machine=q35, UEFI
    firmware, clock=UTC. Marks the import meta as consumed.

    Not routed through the bedrock_d vm_create saga (unlike POST /api/vms,
    which uses _run_vm_saga). The saga's image-fill step only knows how to
    write the cached Alpine image or boot an ISO — it has no "import a
    pre-existing disk image" mode, and import additionally needs source-disk
    firmware sniffing (BIOS vs UEFI), Windows Hyper-V enlightenments, and
    import-meta consumption that the saga doesn't model. Import is also
    cattle-only (single local LV, no DRBD), so there is no post-promote DRBD
    UUID to record (INV-5 only applies to replicated pet/vipet disks).

    Per-resource naming: this path uses vm-<name>-disk<N> LV names, matching
    the rest of the mgmt VM layer (attach-disk at _next_drbd_minor /
    api_vm_attach_disk, _vm_get_settings, mgmt-side destroy) so disk ops on an
    imported VM stay consistent."""
    if not _VM_NAME_RE.match(req.name):
        raise HTTPException(400, "invalid VM name (3-32 chars, lowercase)")
    if req.priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")

    state = build_cluster_state()
    if req.name in state["vms"]:
        raise HTTPException(409, f"VM {req.name} already exists")

    home_name = _mgmt_node_name()
    nodes_cfg = get_nodes()
    host = nodes_cfg[home_name]["host"]

    # Multi-disk imports: OVA with multiple VMDKs produces meta['disks'] with
    # one entry per disk. Single-disk imports (VHDX/qcow2/etc) still fill in
    # disk_path/virtual_size_gb so we synthesise a one-element disks list
    # for uniform iteration below.
    src_disks = meta.get("disks") or [{
        "index": 0,
        "path": meta.get("disk_path", ""),
        "virtual_size_bytes": meta.get("virtual_size_bytes", 0),
        "virtual_size_gb": meta.get("virtual_size_gb", 20),
        "actual_size_bytes": 0,
        "boot": True,
    }]
    for sd in src_disks:
        if not sd.get("path") or not Path(sd["path"]).exists():
            raise HTTPException(500,
                f"converted disk {sd.get('path','?')} is gone — re-run convert?")

    # Firmware: trust the inspection result from _run_convert if available.
    # Otherwise sniff the BOOT disk's partition table here. Rationale: a BIOS
    # -boot disk can't boot on UEFI firmware — Windows traps 0x7B, Linux drops
    # to EFI shell. Match the source to avoid the footgun.
    boot_src = next((d for d in src_disks if d.get("boot")), src_disks[0])
    firmware = meta.get("detected_firmware")
    if firmware not in ("bios", "uefi"):
        firmware = "bios"
        try:
            head = subprocess.run(
                ["qemu-img", "dd", "-O", "raw", "bs=512", "count=34",
                 f"if={boot_src['path']}", "of=/dev/stdout"],
                capture_output=True, timeout=20).stdout
            if len(head) >= 520 and head[512:520] == b"EFI PART":
                firmware = "uefi"
        except Exception: pass

    vg = _vm_disk_vg(host)
    _ensure_thinpool(host, vg_name=vg)

    # Pre-flight: thin-pool must fit the SUM of actual sizes of all disks.
    total_actual_b = sum(int(d.get("actual_size_bytes") or 0) for d in src_disks)
    if not total_actual_b:
        # Fallback when we couldn't read actual-size from the qcow2
        for sd in src_disks:
            try:
                iq = json.loads(subprocess.run(
                    ["qemu-img", "info", "--output=json", sd["path"]],
                    capture_output=True, text=True).stdout or "{}")
                sd["actual_size_bytes"] = int(iq.get("actual-size") or 0)
            except Exception: pass
        total_actual_b = sum(int(d.get("actual_size_bytes") or 0) for d in src_disks)
    pool_info, _ = ssh_cmd_rc(host,
        f"lvs --noheadings --units b --nosuffix --separator '|' "
        f"-o lv_size,data_percent {vg}/thinpool 2>/dev/null | head -1",
        timeout=10)
    try:
        parts = [p.strip() for p in pool_info.split("|") if p.strip()]
        pool_size_b = int(parts[0]); pool_used_pct = float(parts[1])
        pool_free_b = int(pool_size_b * (100.0 - pool_used_pct) / 100.0)
        need_b = total_actual_b or \
                 sum(d["virtual_size_gb"] for d in src_disks) * (1 << 30)
        if pool_free_b < need_b + (1 << 30):  # +1 GB slack
            raise HTTPException(507,
                f"Thin pool on {home_name} has "
                f"{pool_free_b // (1<<30)} GB free; this import needs "
                f"{need_b // (1<<30)} GB + 1 GB slack. Free space or grow "
                f"the pool before retrying.")
    except HTTPException:
        raise
    except Exception:
        pass

    # Per-disk plan: one LV per source disk, named vm-<vm>-disk0/1/2...
    # Resolved VG (never hardcode 'almalinux').
    vg = _vm_disk_vg(host)
    disks_plan = []
    for sd in src_disks:
        vgb = sd["virtual_size_gb"] or 1
        ln = f"vm-{req.name}-disk{sd['index']}"
        disks_plan.append({
            "index": sd["index"],
            "lv_name": ln,
            "lv_path": f"/dev/{vg}/{ln}",
            "size_gb": vgb,
            "size_mb": max(vgb * 1024, 1024),
            "src_qcow": sd["path"],
        })

    # 1. lvcreate + qemu-img convert for every disk. Iterative, unwind on fail.
    created_lvs: list[str] = []
    for d in disks_plan:
        step_name = f"disk{d['index']}: lvcreate + qemu-img convert ({d['size_gb']} GB)"
        if task: task.step_start(step_name)
        push_log(f"Import {meta['id']} → create VM {req.name}: "
                 f"lvcreate {d['size_gb']}G thin ({d['lv_name']})",
                 node=home_name, app="bedrock-mgmt", level="info")
        out, rc = ssh_cmd_rc(host,
            f"lvcreate -y -V {d['size_mb']}M --thin -n {d['lv_name']} "
            f"{vg}/thinpool 2>&1", timeout=60)
        if rc != 0 and "already exists" not in out:
            for lv in created_lvs:
                ssh_cmd_rc(host, f"lvremove -f {lv} 2>&1", timeout=15)
            if task: task.step_fail(step_name, out[-300:])
            raise HTTPException(500, f"lvcreate {d['lv_name']} failed: {out}")
        created_lvs.append(d["lv_path"])
        # Sparse-preserving convert into the LV
        out, rc = ssh_cmd_rc(host,
            f"qemu-img convert -p -n -S 4k --target-is-zero -O raw "
            f"{d['src_qcow']} {d['lv_path']} 2>&1", timeout=3600)
        if rc != 0:
            for lv in created_lvs:
                ssh_cmd_rc(host, f"lvremove -f {lv} 2>&1", timeout=30)
            if task: task.step_fail(step_name, (out or "")[-300:])
            raise HTTPException(500,
                f"qemu-img convert {d['lv_name']} failed:\n" + (out or "(no output)"))
        if task: task.step_done(step_name)

    # virt-install with Q35 + matched firmware + UTC. --import + --wait 0
    # means "define and start the VM, then return immediately" (don't block
    # waiting for the guest to shut down — it has an OS, not an installer).
    boot_arg = "--boot uefi" if firmware == "uefi" else ""

    # Hyper-V enlightenments for Windows guests — Windows detects these at
    # boot and uses faster code paths for APICs, spinlocks, synthetic timer,
    # etc. Red Hat's recommended safe set; measurable CPU-load drop on idle
    # Windows VMs, a few % win on busy ones. No-op for non-Windows guests,
    # so we only set it when we're confident the guest is Windows.
    is_windows = meta.get("os_type", "").lower() == "windows"
    if is_windows:
        features_arg = (
            "--features acpi=on,apic=on,"
            "hyperv.relaxed.state=on,hyperv.vapic.state=on,"
            "hyperv.spinlocks.state=on,hyperv.spinlocks.retries=8191,"
            "hyperv.vpindex.state=on,hyperv.runtime.state=on,"
            "hyperv.synic.state=on,hyperv.stimer.state=on,"
            "hyperv.reset.state=on,hyperv.frequencies.state=on "
        )
        clock_arg = "--clock offset=utc,hypervclock_present=yes "
    else:
        features_arg = ""
        clock_arg = "--clock offset=utc "

    # One --disk arg per data disk, in index order → vda, vdb, vdc, ...
    disk_args = " ".join(
        f"--disk path={d['lv_path']},format=raw,bus=virtio,cache=none,discard=unmap"
        for d in disks_plan)

    vi_cmd = (
        f"virt-install --name {req.name} --vcpus {req.vcpus} --ram {req.ram_mb} "
        f"{disk_args} "
        f"--network bridge=br0,model=virtio "
        f"--graphics vnc,listen=0.0.0.0 "
        f"--channel unix,target_type=virtio,name=org.qemu.guest_agent.0 "
        f"--machine q35 "
        f"{boot_arg} "
        f"{features_arg}"
        f"{clock_arg}"
        f"--os-variant detect=on,name=generic "
        f"--noautoconsole --wait 0 --import 2>&1"
    )
    if task: task.step_start("virt-install")
    push_log(f"Import {meta['id']} → virt-install ({len(disks_plan)} disk(s))",
             node=home_name, app="bedrock-mgmt", level="info")
    out, rc = ssh_cmd_rc(host, vi_cmd, timeout=120)
    if rc != 0:
        ssh_cmd_rc(host, f"virsh undefine {req.name} --nvram 2>&1", timeout=10)
        for lv in created_lvs:
            ssh_cmd_rc(host, f"lvremove -f {lv}", timeout=30)
        if task: task.step_fail("virt-install", (out or "")[-300:])
        raise HTTPException(500, "virt-install failed:\n" + (out or "(no output)"))
    if task: task.step_done("virt-install")

    # Priority
    shares = PRIORITY_CPU_SHARES[req.priority]
    ssh_cmd_rc(host, f"virsh schedinfo {req.name} --live --config cpu_shares={shares}",
               timeout=10)

    # Inventory
    inv = load_inventory()
    inv[req.name] = {
        "priority": req.priority, "vcpus": req.vcpus, "ram_mb": req.ram_mb,
        "disk_gb": disks_plan[0]["size_gb"],   # primary disk size
        "disks": [
            {"index": d["index"], "lv": d["lv_name"], "size_gb": d["size_gb"]}
            for d in disks_plan
        ],
        "iso": None,
        "home_node": home_name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "import",
        "imported_from": meta.get("original_name", meta["id"]),
    }
    save_inventory(inv)

    # Mark import as consumed
    d = _import_dir(meta["id"])
    meta["status"] = "consumed"
    meta["consumed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["consumed_as"] = req.name
    _write_import_meta(d, meta)

    disk_summary = ", ".join(f"disk{d['index']}={d['size_gb']}G" for d in disks_plan)
    push_log(f"Imported VM {req.name} on {home_name} (vcpus={req.vcpus}, "
             f"ram={req.ram_mb}MB, {disk_summary}, "
             f"from {meta.get('original_name')})",
             node=home_name, app="bedrock-mgmt", level="info")
    return {"status": "created", "name": req.name, "node": home_name,
            "disks": [d["lv_name"] for d in disks_plan]}


def _vm_set_resources(vm_name: str, req) -> dict:
    running, host, resource = _vm_host(vm_name)
    result = {}

    if req.vcpus is not None:
        if req.vcpus < 1 or req.vcpus > 32:
            raise HTTPException(400, "vcpus must be 1-32")
        # --config applies on next boot; also setvcpus-max to the new count so
        # both the current and max declarations stay coherent.
        ssh_cmd(host, f"virsh setvcpus {vm_name} {req.vcpus} --config --maximum", timeout=10)
        ssh_cmd(host, f"virsh setvcpus {vm_name} {req.vcpus} --config", timeout=10)
        result["vcpus"] = {"applied": True, "requires_reboot": True,
                          "note": f"queued for next boot ({req.vcpus} vCPUs)"}
        push_log(f"VM {vm_name}: vcpus → {req.vcpus} (reboot required)",
                 node=running, app="bedrock-mgmt", level="info")

    if req.ram_mb is not None:
        if req.ram_mb < 128 or req.ram_mb > 131072:
            raise HTTPException(400, "ram_mb must be 128-131072")
        kib = req.ram_mb * 1024
        ssh_cmd(host, f"virsh setmaxmem {vm_name} {kib} --config", timeout=10)
        ssh_cmd(host, f"virsh setmem   {vm_name} {kib} --config", timeout=10)
        result["ram_mb"] = {"applied": True, "requires_reboot": True,
                           "note": f"queued for next boot ({req.ram_mb} MB)"}
        push_log(f"VM {vm_name}: ram → {req.ram_mb} MB (reboot required)",
                 node=running, app="bedrock-mgmt", level="info")

    if req.disk_gb is not None:
        # Grow the data LV (and DRBD if this VM is pet/ViPet), then tell QEMU.
        cur = _vm_get_settings(vm_name)
        cur_gb = cur["disk_gb"]
        if req.disk_gb < cur_gb:
            raise HTTPException(400, f"disk shrink not supported ({cur_gb}G → {req.disk_gb}G)")
        if req.disk_gb == cur_gb:
            result["disk_gb"] = {"applied": False, "requires_reboot": False, "note": "unchanged"}
        else:
            delta = req.disk_gb - cur_gb
            nodes_cfg = get_nodes()
            # If DRBD: grow data + meta LVs on every peer first
            if resource:
                existing = _parse_drbd_res(host, resource)
                for n in existing["peers"]:
                    ssh_cmd(nodes_cfg[n]["host"],
                        f"lvextend -L +{delta}G {existing['lv_path']} 2>&1", timeout=30)
                # drbdadm resize on primary propagates to peers
                ssh_cmd(host, f"drbdadm resize {resource}", timeout=30)
            else:
                ssh_cmd(host, f"lvextend -L +{delta}G {cur['disk_path']} 2>&1", timeout=30)
            # Tell QEMU the new size (live)
            new_bytes = req.disk_gb * 1024 * 1024  # KiB units for blockresize
            ssh_cmd(host,
                f"virsh blockresize {vm_name} {cur['disk_target']} {new_bytes}K",
                timeout=15)
            # Inventory
            inv = load_inventory()
            if vm_name in inv:
                inv[vm_name]["disk_gb"] = req.disk_gb
                save_inventory(inv)
            result["disk_gb"] = {"applied": True, "requires_reboot": False,
                                 "note": f"live-grown {cur_gb}G → {req.disk_gb}G "
                                         "(guest may need rescan)"}
            push_log(f"VM {vm_name}: disk grown {cur_gb}G → {req.disk_gb}G (live)",
                     node=running, app="bedrock-mgmt", level="info")

    return result


def _vm_set_priority(vm_name: str, priority: str) -> dict:
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(400, f"priority must be one of {_VALID_PRIORITIES}")
    running, host, _ = _vm_host(vm_name)
    shares = PRIORITY_CPU_SHARES[priority]
    ssh_cmd(host, f"virsh schedinfo {vm_name} --live --config cpu_shares={shares}",
            timeout=10)
    inv = load_inventory()
    inv.setdefault(vm_name, {})["priority"] = priority
    save_inventory(inv)
    # Mirror to rqlite so the cluster-wide self-heal repair loop orders
    # replica restoration by the operator's current choice (SG-05).
    try:
        _bs.vm_set_priority(name=vm_name, priority=priority)
    except Exception as e:
        log.warning(f"vm priority rqlite-mirror skipped: {e}")
    push_log(f"VM {vm_name}: priority → {priority} (cpu_shares={shares}, live)",
             node=running, app="bedrock-mgmt", level="info")
    return {"applied": True, "requires_reboot": False,
            "priority": priority, "cpu_shares": shares}


def _vm_set_cdrom(vm_name: str, action: str, iso: Optional[str]) -> dict:
    if action not in ("eject", "insert"):
        raise HTTPException(400, "action must be 'eject' or 'insert'")
    running, host, _ = _vm_host(vm_name)
    settings = _vm_get_settings(vm_name)
    slot = settings.get("cdrom_slot")
    if not slot:
        raise HTTPException(400, "This VM has no CDROM device (was it created "
                            "without an ISO?). Recreate with an ISO to get a "
                            "CDROM slot.")
    if action == "eject":
        ssh_cmd(host, f"virsh change-media {vm_name} {slot} --eject --live --force",
                timeout=10)
        push_log(f"VM {vm_name}: ejected CDROM",
                 node=running, app="bedrock-mgmt", level="info")
        return {"applied": True, "requires_reboot": False, "note": "ejected"}
    # insert
    if not iso:
        raise HTTPException(400, "iso filename required for insert")
    iso_name = Path(iso).name
    if not (ISO_DIR / iso_name).exists():
        raise HTTPException(400, f"ISO not found: {iso_name}")
    target = f"{ISO_MOUNT_DIR}/{iso_name}"
    ssh_cmd(host,
        f"virsh change-media {vm_name} {slot} {target} --insert --live --force",
        timeout=10)
    push_log(f"VM {vm_name}: inserted {iso_name}",
             node=running, app="bedrock-mgmt", level="info")
    return {"applied": True, "requires_reboot": False, "note": f"inserted {iso_name}"}


# ── Metrics API (queries VictoriaMetrics) ───────────────────────────────────

from victoria import query_range, query_instant, query_logs
from victoria import push_log as _vl_push_log

# Metrics + logs read endpoints live in mgmt/routes_obs.py.
from routes_obs import register_routes as _register_obs_routes
_register_obs_routes(app)

# Generic saga submission API — POST /api/operations + the read-side.
# This is the surface the CLI (and any external automation) uses to submit
# vm_create / destroy / grow / migrate / cluster_init / node_join /
# node_leave sagas.
from routes_operations import register_routes as _register_operations_routes
_register_operations_routes(app, require_operator=require_operator)


# ── Supportability checks ─────────────────────────────────────────────────
# Endpoint lives in mgmt/routes_support.py. Pure read-only diagnostic;
# see that file for the per-check details.
from routes_support import register_routes as _register_support_routes
_register_support_routes(
    app,
    load_cluster=load_cluster,
    get_nodes=get_nodes,
    ssh_cmd_rc=ssh_cmd_rc,
)


# ── Console redirect + VNC WebSocket → raw-TCP proxy ───────────────────────
# Implementation lives in mgmt/routes_console.py.
from routes_console import register_routes as _register_console_routes
_register_console_routes(
    app,
    build_cluster_state=build_cluster_state,
    get_nodes=get_nodes,
    get_vm_vnc_port=get_vm_vnc_port,
)

# ── routes_iso (deferred from earlier — needs push_log) ──────────────────
from routes_iso import register_routes as _register_iso_routes
_register_iso_routes(app, push_log=push_log)


# ── Static files (Svelte build + noVNC) ────────────────────────────────────
from fastapi.responses import FileResponse

novnc_dir = Path(__file__).parent / "novnc"
if novnc_dir.exists():
    app.mount("/novnc", StaticFiles(directory=str(novnc_dir)), name="novnc")

ui_build = Path(__file__).parent / "ui" / "build"

# Serve static assets from Svelte build
if ui_build.exists():
    # Mount _app directory for JS/CSS bundles
    app_dir = ui_build / "_app"
    if app_dir.exists():
        app.mount("/_app", StaticFiles(directory=str(app_dir)), name="svelte_app")

    # SPA fallback: any unmatched route serves index.html
    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # Try serving the exact file first
        file_path = ui_build / path
        if file_path.is_file():
            return FileResponse(str(file_path))
        # Otherwise serve index.html (SPA routing)
        return FileResponse(str(ui_build / "index.html"))

# ── Main ────────────────────────────────────────────────────────────────────

def serve_main():
    """Bind uvicorn to the operator/CLI ports and block until SIGTERM.
    The bedrock-d entrypoint calls this after wiring shared state +
    starting the netd thread.

    Listeners:
      * 8443 HTTPS — operator dashboard + LAN-reachable mgmt API.
        Browser-trusted via the local-ip.co wildcard cert (refresh
        timer keeps it ≤30 days from expiry). Bound only when a cert
        is present; until then we fall back to the open-LAN bootstrap
        port below.
      * 127.0.0.1:8001 HTTP — local CLI / intra-process endpoint. The
        ``bedrock`` CLI dials this; rqlite_client, view_builder, etc.
        also point here. **Loopback-only, no LAN exposure.**
      * 8444 LAN HTTP — bootstrap-only, used when no TLS cert exists
        yet so a joiner can fetch ``/api/cluster``. As soon as the
        cert-refresh timer drops the first cert (~2 min after install)
        the next restart switches to the safe layout above.

    Port 8080 is reserved for ``weed-volume`` (see
    docs/storage-architecture.md); the local mgmt API is on
    ``http://127.0.0.1:8001``.

    The bootstrap listener must NOT reuse 8080: weed-volume binds
    ``0.0.0.0:8080`` (every node), and 0.0.0.0 already covers loopback,
    so a 127.0.0.1:8080 bootstrap bind would EADDRINUSE. With bedrock-d
    owning boot (quorum-aware), weed-volume comes up after the
    orchestrator establishes role/quorum — but the bootstrap branch runs
    on a fresh cert-less node where ordering can't be relied on, so we
    bind a dedicated bootstrap port (8444) clear of the whole map.
    (finding T-05.)
    """
    import threading
    import uvicorn
    cert = Path("/etc/bedrock/tls/cert.pem")
    key  = Path("/etc/bedrock/tls/key.pem")
    # Always bind 127.0.0.1:8001 — the local CLI dials this regardless
    # of cert state. Running in a daemon thread so the main thread can
    # bring up the LAN listener (8443 with cert, 8080 without).
    threading.Thread(
        target=lambda: uvicorn.run(app, host="127.0.0.1", port=8001,
                                   log_level="warning"),
        daemon=True,
    ).start()
    if cert.exists() and key.exists():
        uvicorn.run(app, host="0.0.0.0", port=8443,
                    ssl_keyfile=str(key), ssl_certfile=str(cert))
    else:
        # No cert yet — bind a LAN-reachable bootstrap HTTP port so a
        # joiner can fetch /api/cluster before the first cert exists.
        # NOT 8080: weed-volume binds 0.0.0.0:8080 on every node and
        # 0.0.0.0 already covers loopback, so any 8080 bind here would
        # EADDRINUSE. 8444 is dedicated to this bootstrap window and
        # clear of the whole port map (docs/storage-architecture.md).
        # When the cert-refresh timer drops the first cert, the next
        # bedrock-d restart flips to 8443. (finding T-05.)
        uvicorn.run(app, host="0.0.0.0", port=8444)


if __name__ == "__main__":
    serve_main()
