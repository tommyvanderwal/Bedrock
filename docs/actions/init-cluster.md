# Start a new cluster (`bedrock init`)

Turns a bootstrapped node into the first node of a new cluster. Downloads
and starts the full mgmt stack (FastAPI + Svelte + VM + VL + exporters),
generates a cluster UUID, writes `/etc/bedrock/cluster.json` with this
node pre-registered, and prints the dashboard URL.

**Triggered by:** operator on a bootstrapped node:

```bash
bedrock init [--name CLUSTER_NAME] [--witness HOST]
```

**Source:** `installer/bedrock:cmd_init`, `installer/lib/mgmt_install.py`,
`installer/lib/exporters.py`.

## Preconditions

- `bedrock bootstrap` ran successfully (`/etc/bedrock/state.json` has
  `bootstrap_done: true`).
- This node is **not** already a member of a cluster (`cluster_uuid` not
  set in state).
- Repo (the one in `/etc/bedrock/installer.env`) is still reachable — init
  fetches binaries and the mgmt tarball.

## Sequence

```
  T=0    bedrock init --name <cluster_name>
         │
         │ (state.load, guards)
         │
  T+0.1  mgmt_install.install_full(cluster_name, witness=None, repo)
         │
         │  1. mkdir /opt/bedrock/{bin,data/vm,data/vl,mgmt}
         │
         │  2. IF /opt/bedrock/bin/victoria-metrics absent:
         │       curl <repo>/binaries/victoria-metrics  → bin/
         │       chmod 755
         │     (same for victoria-logs)
         │
         │  3. curl <repo>/mgmt.tar.gz → /tmp, extract into /opt/bedrock/mgmt
         │     pip3 install -q fastapi uvicorn paramiko websockets pydantic
         │
         │  4. write /opt/bedrock/scrape.yml  (this node only, by IP)
         │
         │  5. exporters.install(repo)  ──────────────────┐
         │       curl <repo>/binaries/node_exporter       │
         │       curl <repo>/binaries/vm_exporter.py      │
         │       write /etc/systemd/system/{node,vm}-exporter.service
         │       systemctl daemon-reload                  │
         │       systemctl enable --now node-exporter vm-exporter
         │                                                 │
         │  6. write /etc/systemd/system/bedrock-{mgmt,vm,vl}.service
         │     systemctl enable --now bedrock-vm bedrock-vl bedrock-mgmt
         │
         │  7. update state.json:                           │
         │       cluster_name, cluster_uuid (random uuid4), │
         │       role=mgmt+compute, node_id=0, mgmt_ip,     │
         │       mgmt_url=http://<ip>:8080, witness_host=self│
         │                                                   │
         │  8. write /etc/bedrock/cluster.json:              │
         │     { cluster_name, cluster_uuid,                 │
         │       nodes: { <hostname>: { host, drbd_ip, ... }}}│
         │                                                   │
  T+~30s print "Dashboard: http://<ip>:8080"
         │
  T+~32s (bedrock-mgmt service starts)
         │    on_event('startup'):
         │      _main_loop = asyncio.get_running_loop()
         │      _last_state seeded from cluster.json
         │      state_push_loop scheduled (3s interval)
         │      write_scrape_config(load_cluster())  →  /-/reload
         │
  T+~35s first state push loop tick:
         │    - SSH to localhost (self) via key-auth
         │    - virsh list --all/--running  →  "no VMs"
         │    - drbdadm status  →  "no resources defined!"  (expected)
         │    - loadavg, meminfo, uptime
         │    - broadcast("cluster", {...})  → no subscribers yet
         │    - _last_state ← this snapshot
```

## Log lines emitted

**stdout during init:**

```
=== Bedrock Init (new cluster) ===

Creating cluster: <name>
  Fetching victoria-metrics...
  Fetching victoria-logs...
  Installing dashboard application...
  Fetching node_exporter...
  Fetching vm_exporter...
  Starting services...
  Cluster UUID: <uuid>
  Mgmt URL:     http://<ip>:8080

Cluster <name> initialised.
Dashboard: http://<ip>:8080
```

**Systemd journals (`journalctl -u <service>`):**

- `bedrock-vm`: `reading scrape configs from "/opt/bedrock/scrape.yml"`
- `bedrock-vl`: `started VictoriaLogs at :9428`
- `bedrock-mgmt`: uvicorn startup + `INFO: Application startup complete.`
- `node-exporter`, `vm-exporter`: listening on 9100 / 9177

**VictoriaLogs:** no entries yet — `push_log` has nothing to say during init.
First entry arrives when the first node joins (see
[`join-cluster.md`](join-cluster.md)) or a VM action fires.

## Why this order

1. **Binaries before systemd units**: the units `ExecStart=` the binary
   paths; missing binary = unit fails on first start.
2. **scrape.yml before `bedrock-vm` unit**: VM reads the config on startup
   (there is no "wait and retry" loop in VM for missing config; it would
   start with an empty scrape set and you'd miss early samples).
3. **exporters before the first state push**: the push loop asks VM for
   "up" presence of each node; a scrape slot with no exporter = `up=0`,
   which the dashboard renders as "offline".
4. **cluster.json last**: it gates `load_cluster()` — anything that runs
   before that falls back to the hardcoded `FALLBACK_NODES` (the physical
   lab IPs), which would confuse the dashboard for a few seconds.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `Already a member of cluster <x>` | state.json has `cluster_uuid` from a previous run | `rm /etc/bedrock/state.json /etc/bedrock/cluster.json` and re-init (only safe if no VMs exist yet). |
| `curl <repo>/binaries/victoria-metrics` 404 | Repo missing artefacts | `ls installer/binaries/` on repo host; rebuild if empty. |
| `systemctl enable --now bedrock-mgmt` fails | FastAPI deps missing (pip in air-gap) | Install manually: `pip3 install fastapi uvicorn paramiko websockets pydantic`, then `systemctl restart bedrock-mgmt`. |
| Dashboard 200s but `/api/cluster` returns `{"nodes": {}}` | `_last_state` still empty because SSH to self failed | Check `/root/.ssh/authorized_keys` contains `/root/.ssh/id_ed25519.pub` for self-auth (the testbed setup script adds this; bare-metal `init` should add it too — see [`components/mgmt-dashboard.md`](../components/mgmt-dashboard.md)). |

## Post-init state

```
  Node state:
    /etc/bedrock/state.json   cluster_uuid, role=mgmt+compute, node_id=0
    /etc/bedrock/cluster.json { nodes: { this-node: {...} } }

  Services running:
    bedrock-vm    VictoriaMetrics   :8428
    bedrock-vl    VictoriaLogs      :9428, syslog :5140
    bedrock-mgmt  FastAPI+Svelte    :8080
    node-exporter                   :9100
    vm-exporter                     :9177
    libvirtd                        (local socket)
    cockpit.socket                  :9090

  Dashboard URL:
    http://<node-ip>:8080
    → sidebar shows 1 host, 0 VMs
```

## What's next

- Create a cattle VM (`bedrock vm create foo --type cattle`) — works on 1 node.
- Or add a second node with [`join-cluster.md`](join-cluster.md) to unlock pet.
