"""Browser console + VNC proxy routes.

Extracted from mgmt/app.py as Stage 9 PR #1 of the rewrite plan —
leaf endpoints, no cross-imports outside the app module, smallest
possible first move in the package split.

Two endpoints:

- ``GET /console/{vm_name}`` — HTTP redirect to noVNC, pointing at
  the WebSocket proxy below.
- ``WS /vnc/{vm_name}`` — WebSocket on the mgmt node that proxies
  to the VM's host:VNC-port via a raw TCP connection. No
  websockify needed on the cluster nodes.

# Wiring

mgmt/app.py imports ``register_routes`` and calls it after the
FastAPI ``app`` object is constructed but before uvicorn.run.
Both routes use ``build_cluster_state`` + ``get_nodes`` +
``get_vm_vnc_port`` from app.py; we receive them as injected
callables to avoid a circular import.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import HTMLResponse, RedirectResponse

log = logging.getLogger(__name__)


def register_routes(
    app: FastAPI,
    *,
    build_cluster_state: Callable,
    get_nodes: Callable,
    get_vm_vnc_port: Callable,
) -> None:
    """Attach /console/{vm_name} + /vnc/{vm_name} to ``app``.

    Dependency injection (the three callables) keeps this module
    standalone — no `from mgmt.app import ...` cycle. mgmt/app.py
    calls this once at startup.
    """

    @app.get("/console/{vm_name}")
    def console_page(vm_name: str):
        state = build_cluster_state()
        vm = state["vms"].get(vm_name)
        if not vm:
            raise HTTPException(404)
        if not vm.get("vnc_ws_url"):
            return HTMLResponse("<h2>VM not running or no VNC</h2>")
        # Direct noVNC at the mgmt-hosted proxy. An empty host+port
        # tells noVNC to use window.location; path routes to
        # /vnc/<vm>.
        return RedirectResponse(
            f"/novnc/vnc.html?path=vnc/{vm_name}"
            f"&autoconnect=true&resize=scale&reconnect=true"
        )

    @app.websocket("/vnc/{vm_name}")
    async def vnc_proxy(ws: WebSocket, vm_name: str):
        # Only echo back "binary" if the client offered it. Modern
        # noVNC often sends no subprotocol; Starlette rejects the
        # handshake if we reply with one the client didn't list.
        offered = (ws.headers.get("sec-websocket-protocol") or "").split(",")
        offered = [o.strip() for o in offered if o.strip()]
        if "binary" in offered:
            await ws.accept(subprotocol="binary")
        else:
            await ws.accept()
        nodes_cfg = get_nodes()
        state = build_cluster_state()
        vm = state["vms"].get(vm_name)
        if not vm or vm.get("state") != "running" or not vm.get("running_on"):
            await ws.close(code=1011, reason="VM not running")
            return
        host = nodes_cfg[vm["running_on"]]["host"]
        port = get_vm_vnc_port(host, vm_name)
        if port <= 0:
            await ws.close(code=1011, reason="no VNC port")
            return
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except Exception as e:
            await ws.close(code=1011, reason=f"connect: {e}")
            return

        total_ws_to_tcp = 0
        total_tcp_to_ws = 0

        async def ws_to_tcp():
            nonlocal total_ws_to_tcp
            try:
                while True:
                    data = await ws.receive_bytes()
                    total_ws_to_tcp += len(data)
                    writer.write(data)
                    await writer.drain()
            except Exception as e:
                log.info("vnc_proxy ws->tcp ended: %s (sent=%d)",
                         e, total_ws_to_tcp)
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        async def tcp_to_ws():
            nonlocal total_tcp_to_ws
            try:
                while True:
                    data = await reader.read(16384)
                    if not data:
                        break
                    total_tcp_to_ws += len(data)
                    await ws.send_bytes(data)
            except Exception as e:
                log.info("vnc_proxy tcp->ws ended: %s (sent=%d)",
                         e, total_tcp_to_ws)
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass

        await asyncio.gather(ws_to_tcp(), tcp_to_ws())
