# mgmt/routes_console.py

Browser-console routes for the mgmt dashboard: a redirect endpoint that opens
noVNC in the browser, and a WebSocket endpoint that proxies that noVNC session
through to a VM's VNC port. The proxy runs on the mgmt node and bridges the
browser's WebSocket to a raw TCP connection to `host:vnc_port`, so cluster nodes
need no websockify of their own. `mgmt/app.py` imports `register_routes` and
calls it once, after the FastAPI `app` is built and before `uvicorn.run`, passing
in the three lookups it needs as callables (dependency injection keeps this a
leaf module with no import back into `mgmt/app.py`).

## Functions / Classes

### `register_routes(app, *, build_cluster_state, get_nodes, get_vm_vnc_port) -> None`
Attaches `GET /console/{vm_name}` and `WS /vnc/{vm_name}` to the FastAPI app.
- **In:**
  - `app` — the FastAPI application to register routes on.
  - `build_cluster_state` — callable returning the cluster-state dict; routes read
    `state["vms"][vm_name]` (fields used: `vnc_ws_url`, `state`, `running_on`).
  - `get_nodes` — callable returning the node config map; routes read
    `nodes[running_on]["host"]`.
  - `get_vm_vnc_port` — callable `(host, vm_name) -> int`; the VM's VNC TCP port,
    `<= 0` meaning none.
- **Out:** returns `None`. Side effect: defines and registers the two endpoint
  handlers below on `app`. No files, services, rqlite, or subprocess; the WS
  handler opens an outbound TCP connection to `host:port` per session.

The two registered handlers (closures over the injected callables):

- **`GET /console/{vm_name}` → `console_page(vm_name)`** — 404s if the VM is
  unknown; returns an `HTMLResponse` saying the VM is not running / has no VNC if
  `vnc_ws_url` is absent; otherwise returns a `RedirectResponse` to
  `/novnc/vnc.html?path=vnc/{vm_name}&autoconnect=true&resize=scale&reconnect=true`.
- **`WS /vnc/{vm_name}` → `vnc_proxy(ws, vm_name)`** — accepts the WebSocket, opens
  a TCP connection to the VM host's VNC port, and pumps bytes both ways until
  either side closes.

## How it works

`/console/{vm_name}` is a thin gate. It reads cluster state, rejects unknown VMs
with `404`, and for a VM with no `vnc_ws_url` returns a small HTML notice rather
than a broken redirect. For a live VM it redirects the browser to the bundled
noVNC page with a relative `path=vnc/{vm_name}`. The empty host/port makes noVNC
derive the WebSocket origin from `window.location`, so the session flows back
through the mgmt node's own `/vnc/{vm_name}` endpoint.

`/vnc/{vm_name}` is the proxy. The handshake is subprotocol-sensitive: it echoes
`binary` only if the client listed it in `sec-websocket-protocol`, because
Starlette rejects the handshake if the server names a subprotocol the client did
not offer, and modern noVNC often sends none.

After accepting, it re-checks state: the VM must exist, be `state == "running"`,
and have a `running_on`; otherwise it closes with code `1011`. It resolves the
host from `get_nodes()[running_on]["host"]`, then the VNC port from
`get_vm_vnc_port(host, vm_name)`; a non-positive port closes `1011`. It opens the
TCP connection with `asyncio.open_connection(host, port)`, and a connect failure
closes `1011` with the error in the reason.

Once connected, two coroutines run concurrently under `asyncio.gather`, forming a
full-duplex byte pump:

```
  browser                 mgmt node (this proxy)              VM host
 (noVNC)                                                     (qemu VNC)
    │  WS frames    ┌──────────── ws_to_tcp ───────────┐  raw TCP
    │ ─────────────►│ ws.receive_bytes → writer.write   │ ──────────►
    │               │                    + writer.drain │
    │  WS frames    ┌──────────── tcp_to_ws ───────────┐
    │ ◄─────────────│ reader.read(16384) → ws.send_bytes│ ◄──────────
                    └───────────────────────────────────┘
```

- `ws_to_tcp` loops on `ws.receive_bytes()`, writes to the TCP `writer`, and
  drains; on any exception it logs the byte count sent and, in `finally`, closes
  the `writer`.
- `tcp_to_ws` loops reading up to 16384 bytes from the TCP `reader`, stops on EOF
  (empty read), sends each chunk over the WebSocket, and in `finally` closes the
  WebSocket.

Because both directions close their far end in `finally`, the death of either
half (browser disconnect or VNC socket close) tears down both legs and
`asyncio.gather` returns, ending the proxy. Running byte totals are tracked only
for the log line emitted when a direction ends.

## Why

The proxy lives on the mgmt node so the browser only ever needs to reach the
mgmt endpoint, and cluster nodes run no websockify. The `binary` subprotocol is
conditional purely to satisfy Starlette's handshake check against clients that
offer no subprotocol.
