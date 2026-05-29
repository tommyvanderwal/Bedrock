# mgmt-dashboard (FastAPI + Svelte)

The mgmt half of `bedrock-d` serves the operator UI, answers REST/WebSocket
API calls, proxies noVNC, and aggregates live cluster state by SSH fan-out to
every node. Mutating VM lifecycle ops (create / destroy / grow / migrate, and
the cattle↔pet↔vipet HA-level change) go through the orchestrator: the API
submits a saga or writes intent to rqlite, and the reactor converges each node.
Operators never SSH a node to change state.

**Source:** `mgmt/app.py` (core), with route modules `mgmt/routes_console.py`
(VNC proxy + console redirect), `mgmt/routes_obs.py` (metrics/logs reads),
`mgmt/routes_operations.py` (saga submit/retry/read), `mgmt/routes_iso.py`,
`mgmt/routes_support.py`; plus `mgmt/ws.py` (WS hub), `mgmt/victoria.py`
(VictoriaMetrics/VictoriaLogs query client), `mgmt/vm_exporter.py` (the
`:9177` exporter that also runs on every compute node), and the Svelte build
under `mgmt/ui/build/`.

## Listeners

`bedrock-d` wires the shared cluster state into `mgmt/app.py`'s FastAPI app and
calls `serve_main()`, which binds two uvicorn listeners on one shared `app`:

- **`0.0.0.0:8443` HTTPS** — operator dashboard + LAN-reachable mgmt API,
  operator-authenticated (`lib/operator_auth.py`). Cert is the browser-trusted
  `local-ip.co` wildcard, kept ≤30 days from expiry by the refresh timer.
  Port 80 redirects here via `bedrock-redirect`.
- **`127.0.0.1:8001` HTTP** — local CLI / intra-process endpoint, loopback
  only and auth-exempt (local root is already privileged). The `bedrock` CLI,
  rqlite_client, view_builder, etc. dial this.

When no TLS cert exists yet, the LAN listener binds bootstrap HTTP on
`0.0.0.0:8444` so a joiner can fetch `/api/cluster` before the first cert; the
next `bedrock-d` restart binds `:8443` once the cert lands. The bootstrap port
is `:8444`, not `:8080`, because `weed-volume` owns `0.0.0.0:8080` on every
node (which also covers loopback).

## Responsibilities

```
  HTTP / WebSocket server
    - Static Svelte SPA at /  (+ /novnc static for the noVNC client)
    - /api/* REST endpoints (see reference/api.md)
    - /ws multiplexed WebSocket (operator-token in query param)
    - WS /vnc/{name} VNC TCP proxy           (routes_console.py)
    - GET /console/{name} → redirect to /novnc/vnc.html?path=vnc/<name>

  State aggregator
    - state_push_loop: every 3 s, run build_cluster_state() in a worker
      thread, store as _last_state, broadcast on WS 'cluster' channel
    - _last_state cache: served as-is to GET /api/cluster for instant
      response (no SSH on the request path)

  Cluster scrape config
    - load_cluster() reads topology from the local rqlite replica
    - write_scrape_config() regenerates /opt/bedrock/scrape.yml and
      restarts bedrock-vmagent (best-effort, --no-block) so it re-reads

  Log fan-out
    - push_log() broadcasts on WS 'event' first, then inserts into
      VictoriaLogs (UI reacts instantly even if VL is slow)
```

## Key functions

| Function (file) | Purpose |
|---|---|
| `build_cluster_state()` | Hot path. Parallel SSH fan-out to every node + VM, assembles `{nodes, vms, witness, topology}`. |
| `get_node_info(name, cfg)` | One SSH call per node: VMs, DRBD status, load/mem/uptime/kernel, thinpools, switch + mesh neighbours. |
| `get_vm_disks(host, vm)` | Parses `virsh dumpxml` to enumerate a VM's disks + backing LV / DRBD resource. |
| `get_vm_vnc_port(host, vm)` | `virsh vncdisplay` → 5900+n. Used by the `/vnc/{name}` proxy. |
| `api_vm_migrate(vm, req)` | Live-migrate via the `vm_migrate` saga (resolves source/target from rqlite, cycles dual-primary, records post-promote UUID). |
| `_vm_set_ha_level(vm, type, ...)` | `POST /api/vms/{name}/ha-level`. Dispatches to `_vm_set_ha_level_up` / `_down` (cattle↔pet↔vipet, online via blockcopy or offline via XML rewrite). |
| `load_cluster()` | Cluster topology from the local rqlite replica via `cluster_state.load_cluster()` (read level `none`, works without quorum). |
| `get_witness_status()` | Witness panel data — configured `witnesses` map from cluster state. |
| `write_scrape_config(cluster)` | Regenerates `scrape.yml`, restarts `bedrock-vmagent`. |
| `push_log(msg, ...)` (`victoria.py`) | WS `event` broadcast + VL insert — the path app-level events reach the dashboard. |
| `vnc_proxy` (`routes_console.py`) | WS↔raw-TCP proxy browser ↔ VNC on the VM host. Subprotocol-aware (`binary`) for noVNC clients. |

Saga lifecycle ops (`vm_create`, `vm_destroy`, `vm_grow`, `vm_migrate`,
`node_join`, `node_leave`, `cluster_init`, `cluster_tier_*`) are submitted via
`POST /api/operations` (`routes_operations.py`) and run by the executor in
`bedrock_d/orchestrator/sagas`.

## SSH model

State reads fan out over SSH from the serving node; all cross-node calls go
through one pooled helper. Connections are cached per host (`_SSH_POOL`,
guarded by `_SSH_POOL_LOCK`) and reused; stale entries are dropped and
reopened:

```python
def _ssh_connect(host):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=SSH_USER, timeout=5,
              allow_agent=True, look_for_keys=True)
    c.get_transport().set_keepalive(20)   # survive NAT/firewall idle
    return c
```

Auth is key-only (`allow_agent`, `look_for_keys`) over the `root@host` key
mesh `agent_install` fans out (every node holds every other node's pubkey); no
password fallback. Pooling reuses one Transport per peer and opens channels on
demand — without it the every-3-second probe loop across N nodes × mgmt
processes overruns sshd's pre-auth queue and nodes flap Online/Offline.
`timeout=5` + `set_keepalive(20)` keep one slow/dead node from stalling the
parallel fan-out.

## Startup sequence

```
  @app.on_event("startup")          (guarded by _STARTUP_LOCK — both the
                                     8443 and 8001 uvicorn threads fire it)
      _main_loop = asyncio.get_running_loop()
      _last_state = seed from load_cluster()  (nodes online=false)
      task_registry().wire(_main_loop, hub.broadcast)
      asyncio.create_task(state_push_loop())
      write_scrape_config(load_cluster())
      orchestrator.start_all()       (rqlite_subscriber, boot_orchestrator,
                                      no_quorum_responder, reactor)
```

Both listeners share one `app`, so the hook runs on each thread;
`_STARTUP_LOCK` + `_STARTUP_DONE` make it run exactly once. Seeding
`_last_state` from rqlite before any SSH means the sidebar renders host names
instantly; tiles show "Offline" until the first 3 s state push repopulates them.

## Concurrency

- **Main event loop**: FastAPI + WebSocket hub + `state_push_loop`
  (`await asyncio.sleep(3)`).
- **Worker threads** (`run_in_executor` / Starlette `run_in_threadpool`):
  anything that does blocking paramiko SSH. The paramiko socket read blocks, so
  serving it on the main loop would freeze every other client.
- **`ThreadPoolExecutor` in `build_cluster_state`**: parallelises SSH to all
  nodes and all VMs; wall time is `max(node, vm)` instead of the sum.
- **`asyncio.run_coroutine_threadsafe` in `push_log`**: the orchestrator and
  request handlers run in worker threads, but `hub.broadcast` is async. The
  helper marshals the coroutine onto the captured `_main_loop`:
  ```python
  # WS first so browsers react while VL absorbs the insert
  entry = {"_msg": msg, "hostname": node, ..., "_time": strftime(...)}
  if _main_loop is not None:
      asyncio.run_coroutine_threadsafe(
          hub.broadcast("event", entry), _main_loop)
  _vl_push_log(msg, node=node, app=app, level=level)
  ```

## Client subscriptions (Svelte side)

```
  layout.svelte (onMount, once per browser session)
     ws.connect()  →  wss://<host>/ws?token=<operator-token>
     ws.on('cluster', msg)  → nodes/vms/witness stores update
     ws.on('event',   msg)  → events store prepends
     ws.on('vm.state', msg) → vm-level store patches (reserved)

  Each page derives from $nodes, $vms, $events, $witness
     Recent Logs = seeded via /api/logs + live from $events
     Tiles       = reactive on $vms / $nodes
     VM metrics  = fetched every 15 s from /api/metrics/vms
```

Svelte 5 quirk (project memory): reading a store via `$storeName` inside
`$derived(...)` does **not** track it as a dependency. Use an explicit
`events.subscribe(...)` in `onMount` that writes a local `$state`, then derive
from that.

## Extending

- **New action**: add the endpoint in `app.py` (or the relevant `routes_*`
  module), wrap `push_log` around it, add a button in the Svelte page. The WS
  event lands live; state follows on the next 3 s tick.
- **New periodic metric**: extend `vm_exporter.py` (runs on every compute
  node, auto-scraped at `:9177/metrics`). No scrape-config change needed.
- **New sidebar section**: add a route under `mgmt/ui/src/routes/` and a
  tree-header link in the layout; `$nodes` / `$vms` are already reactive.
```
