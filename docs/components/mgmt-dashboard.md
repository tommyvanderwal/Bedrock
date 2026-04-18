# mgmt-dashboard (FastAPI + Svelte)

Single process on the mgmt node that serves the operator UI, answers
REST/WebSocket API calls, proxies noVNC, and orchestrates cluster-wide
actions by SSH fan-out.

**Source:** `mgmt/app.py` (~1000 lines), plus `mgmt/ws.py` (WS hub),
`mgmt/victoria.py` (VM/VL query client), `mgmt/vm_exporter.py` (also
shipped to compute nodes), and the Svelte build under
`mgmt/ui/build/`.

**Runs as:** `bedrock-mgmt.service` under systemd, uvicorn:

```ini
ExecStart=/usr/bin/python3 /opt/bedrock/mgmt/app.py
WorkingDirectory=/opt/bedrock/mgmt
Restart=always
```

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
    - Holds cluster topology in /etc/bedrock/cluster.json
    - Regenerates /opt/bedrock/scrape.yml on node register
    - Triggers VictoriaMetrics /-/reload

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
| `write_scrape_config(cluster)` | Regenerates scrape.yml, hits VM `/-/reload`. |
| `push_log(msg, ...)` | Both WS broadcast and VL insert — the only way app-level events reach the dashboard. |
| `vnc_proxy` (WS handler) | TCP proxy browser ↔ VNC on the VM host. Subprotocol-aware for older noVNC clients. |

## SSH model

All cross-node calls go through a single helper:

```python
def _ssh_connect(host):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(host, username='root', allow_agent=True, look_for_keys=True)
    except paramiko.AuthenticationException:
        if not SSH_PASS: raise
        c.connect(host, username='root', password=SSH_PASS)
    return c
```

Production path: agent + keys via `allow_agent=True, look_for_keys=True`
(picks up `/root/.ssh/id_ed25519`). Dev fallback: `BEDROCK_SSH_PASS`
env var (off by default).

## Startup sequence

```
  @app.on_event("startup")
      _main_loop = asyncio.get_running_loop()
      _last_state = seed from cluster.json (nodes with online=false)
      asyncio.create_task(state_push_loop())
      write_scrape_config(load_cluster())
        - rewrites scrape.yml with current topology
        - POSTs VM /-/reload
```

Seeding `_last_state` before any SSH happens means the dashboard renders
instantly even if nodes are unreachable — tiles show "Offline" until
the first state push repopulates them.

## Concurrency

- **Main event loop**: FastAPI + WebSocket hub + state_push_loop
  (`await asyncio.sleep(3)`).
- **Per-request threads** (Starlette's `run_in_threadpool`): REST
  handlers that do blocking I/O (paramiko SSH).
- **ThreadPoolExecutor in `build_cluster_state`**: parallelises SSH
  to all nodes + all VMs (3-node cluster went from ~3 s sequential
  to ~0.7 s).
- **`asyncio.run_coroutine_threadsafe`** in `push_log`: schedules the
  WS broadcast from a worker thread onto the main loop safely.

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
