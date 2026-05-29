# mgmt-dashboard (FastAPI + Svelte)

The mgmt half of `bedrock-d` serves the operator UI, answers
REST/WebSocket API calls, proxies noVNC, and reads cluster state via
parallel SSH fan-out. Mutating VM lifecycle ops (create/destroy/migrate/
grow/convert) run through the orchestrator's saga executor
(`bedrock_d/orchestrator/sagas`); the API writes intent to rqlite and the
reactor converges each node, rather than the dashboard SSHing nodes to
change state directly.

**Source:** `mgmt/app.py`, plus `mgmt/ws.py` (WS hub),
`mgmt/victoria.py` (VM/VL query client), `mgmt/vm_exporter.py` (also
shipped to compute nodes), and the Svelte build under
`mgmt/ui/build/`.

**Runs as:** part of `bedrock-d.service`, the unified Bedrock daemon.
`bedrock-d` wires the shared cluster state into `mgmt/app.py`'s FastAPI app
and calls `serve_main()`, which starts the dashboard on **two uvicorn
listeners**:

- **`0.0.0.0:8443` HTTPS** — operator dashboard + LAN-reachable mgmt API,
  operator-authenticated. Port 80 redirects here via `bedrock-redirect`.
- **`127.0.0.1:8001` HTTP** — local CLI / intra-process endpoint, loopback
  only and auth-exempt (local root is already privileged).

(When no TLS cert exists yet, the LAN listener falls back to bootstrap
HTTP on `:8444`; a `bedrock-d` restart flips to `:8443` once the cert
lands. `:8080` is **not** the dashboard — that's the SeaweedFS volume
server, bound on every node.)

## Responsibilities

```
  HTTP / WebSocket server
    - Static Svelte SPA at /
    - /api/* REST endpoints (see reference/api.md)
    - /ws multiplexed WebSocket
    - /vnc/{name} VNC TCP proxy
    - /console/{name} → redirect to /novnc/vnc.html?path=vnc/<name>

  Cluster orchestrator
    - Fans out SSH via paramiko from the mgmt node
    - Reads cluster topology from rqlite (cluster_state.load_cluster())
    - Regenerates /opt/bedrock/scrape.yml on cluster-state change
    - Restarts bedrock-vmagent so it re-reads the scrape config

  State aggregator
    - state_push_loop: every 3 s, SSH to every node in parallel
      (ThreadPoolExecutor), assemble {nodes, vms, witness}, broadcast
      on WS 'cluster' channel
    - _last_state cache: served as-is to HTTP /api/cluster for
      instant response (7 ms vs 650 ms uncached)

  Log fan-out
    - push_log() wrapper broadcasts on WS 'event' first, then inserts
      into VictoriaLogs (so UI reacts instantly even if VL is slow)
```

## Key functions

| Function | Purpose |
|---|---|
| `build_cluster_state()` | The hot path. Parallel SSH to every node, assembles full cluster snapshot. |
| `get_node_info(name, cfg)` | SSHes one node; returns load/mem/VMs/DRBD status. |
| `get_vm_drbd_resource(host, vm)` | parses `virsh dumpxml` + `drbdsetup status --json` to find the resource name. |
| `get_vm_vnc_port(host, vm)` | `virsh vncdisplay` → 5900+n. Used by /vnc/{vm} proxy. |
| `_vm_migrate(vm, target)` | Orchestrates live migration (see actions/vm-migrate.md). |
| `_vm_convert_upgrade` / `_downgrade` | Cattle↔pet↔vipet state machine (see actions/vm-convert.md). |
| `write_scrape_config(cluster)` | Regenerates scrape.yml, restarts `bedrock-vmagent` to re-read it. |
| `load_cluster()` | Cluster-wide topology from rqlite via `cluster_state.load_cluster()` (read-level `none`, works without quorum). |
| `push_log(msg, ...)` | Both WS broadcast and VL insert — the only way app-level events reach the dashboard. |
| `vnc_proxy` (WS handler) | TCP proxy browser ↔ VNC on the VM host. Subprotocol-aware for older noVNC clients. |

## SSH model

All cross-node calls go through a single pooled helper. Connections are
cached per host (`_SSH_POOL`, guarded by `_SSH_POOL_LOCK`) and reused
across calls; stale entries are dropped and reopened:

```python
def _ssh_connect(host):
    # return a live pooled client if one exists, else open a new one
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=SSH_USER, timeout=5,
              allow_agent=True, look_for_keys=True)
    c.get_transport().set_keepalive(20)   # survive NAT/firewall idle
    return c
```

Auth is key-only: agent + keys via `allow_agent=True, look_for_keys=True`,
using the `root@host` key mesh that `agent_install` fans out (every node
holds every other node's pubkey). There is no password fallback. The
`timeout=5` and `set_keepalive(20)` keep a single slow/dead node from
stalling the parallel fan-out.

## Startup sequence

```
  @app.on_event("startup")          (guarded by _STARTUP_LOCK — both the
                                     8443 and 8001 uvicorn threads fire it)
      _main_loop = asyncio.get_running_loop()
      _last_state = seed from load_cluster() (rqlite; nodes online=false)
      asyncio.create_task(state_push_loop())
      write_scrape_config(load_cluster())
        - rewrites scrape.yml with current topology
        - restarts bedrock-vmagent
      orchestrator.start_all()       (rqlite_subscriber, no_quorum_responder,
                                      boot_orchestrator, reactor, ...)
```

Both uvicorn listeners share one `app`, so the startup hook runs on each
thread; `_STARTUP_LOCK` + `_STARTUP_DONE` make it run exactly once.
Seeding `_last_state` before any SSH happens means the dashboard renders
instantly even if nodes are unreachable — tiles show "Offline" until
the first state push repopulates them.

## Concurrency

- **Main event loop**: FastAPI + WebSocket hub + state_push_loop
  (`await asyncio.sleep(3)`).
- **Per-request threads** (Starlette's `run_in_threadpool`): REST
  handlers that do blocking I/O (paramiko SSH). Required because the
  paramiko socket read blocks — serving this on the main loop would
  freeze every other client.
- **ThreadPoolExecutor in `build_cluster_state`**: parallelises SSH
  to all nodes + all VMs. Each node costs ~250 ms to query
  (virsh + drbdadm + lvs); sequential = 3 nodes × 250 ms + 3 VMs ×
  150 ms ≈ 1.2 s; parallel = max(node, vm) ≈ 0.3 s. 3-node cluster
  went from ~3 s to ~0.7 s on the wall clock.
- **`asyncio.run_coroutine_threadsafe`** in `push_log`: the
  orchestrator runs in worker threads (paramiko blocks them), but
  `hub.broadcast` is async. The helper marshals the coroutine onto
  the captured main loop so workers can push events without
  blocking or touching the loop directly.
  ```python
  # order matters — WS first so browsers react while VL absorbs the insert
  entry = {"_msg": msg, "hostname": node, ..., "_time": strftime(...)}
  if _main_loop is not None:
      asyncio.run_coroutine_threadsafe(
          hub.broadcast("event", entry), _main_loop)
  _vl_push_log(msg, node=node, app=app, level=level)   # 20 ms HTTP POST
  ```

## Client subscriptions (how the Svelte side consumes this)

```
  layout.svelte (onMount, once per browser session)
     ws.connect()  →  ws://<host>/ws
     ws.on('cluster', msg)  → nodes/vms/witness stores update
     ws.on('event',   msg)  → events store prepends
     ws.on('vm.state', msg) → vm-level store patches (reserved)

  Each page derives from the stores:
     $nodes, $vms, $events, $witness

     Recent Logs = seeded once via /api/logs + live from $events
     Tiles       = reactive on $vms / $nodes
     VM metrics  = fetched every 15 s from /api/metrics/vms
```

One important Svelte 5 quirk is documented in the project memory: reading
a store via `$storeName` inside `$derived(...)` does **not** track the
store as a dependency. Use an explicit `events.subscribe(...)` in
`onMount` that writes to a local `$state`, then derive from that.

## Extending

- **New action**: add the endpoint in app.py, push_log around it, add
  a button in the Svelte page. The WS event lands live by virtue of
  push_log; state updates follow on the next 3 s tick.
- **New periodic metric**: extend `vm_exporter.py` (runs on every
  compute node, auto-scraped). No VM scrape config change needed —
  existing scrape job pulls `/metrics` from :9177.
- **New sidebar section**: add a route under `mgmt/ui/src/routes/` and
  a corresponding tree-header link in the layout. The layout's
  `$nodes` / `$vms` are already reactive.
