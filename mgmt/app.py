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
    _vm_host, _vm_get_settings, _vm_start, _vm_shutdown, _vm_poweroff,
    _import_backup_module,
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
from routers import witnesses as _r_witnesses  # noqa: E402
app.include_router(_r_witnesses.router)
from routers import storage as _r_storage  # noqa: E402
app.include_router(_r_storage.router)
from routers import backup as _r_backup  # noqa: E402
app.include_router(_r_backup.router)
from routers import vm_backup as _r_vm_backup  # noqa: E402
app.include_router(_r_vm_backup.router)
from routers import vms as _r_vms  # noqa: E402
app.include_router(_r_vms.router)


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
        from routers.vms import api_vm_migrate, MigrateRequest
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
