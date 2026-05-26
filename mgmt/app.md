# `mgmt/app.py`

**Module purpose.** The FastAPI process every node runs as
`bedrock-mgmt.service`. Three concerns:

1. **`/api/*` endpoints** — operator login, cluster status,
   VM CRUD, ISO library, backup targets, join handshake,
   metrics queries.
2. **`/api/peer/*` endpoints** — inter-node authenticated
   queries (drbd state, witness state, mesh state). Signed via
   `peer_auth.sign_request` on the caller; verified by
   `peer_auth.verify_request` on the receiver.
3. **Embeds the orchestrator** — `start_all()` from
   `mgmt/orchestrator.py` is invoked from the FastAPI startup
   hook, which spawns the rqlite_subscriber + no_quorum_responder
   + boot_orchestrator + backup_scheduler + converge_retry
   tasks on the running event loop.

The HTTP server listens on loopback:8080 (HTTP, internal) AND
`<mgmt_ip>:8443` (HTTPS via per-node leaf cert from
`cert_manager`). All cross-node calls go through HTTPS.

This file is BIG (~5400 lines) because it concentrates every
mgmt endpoint. The functions below are grouped by concern.

## Lifecycle helpers

- `load_cluster() / save_cluster(cluster)` — short
  cluster.json IO.
- `get_nodes() -> dict` — cluster.json["nodes"].
- `write_scrape_config(cluster)` — write
  `/opt/bedrock/scrape.yml` for VictoriaMetrics (re-rendered on
  every revision tick that adds/removes a node).
- `build_cluster_state() -> dict` — composes the full cluster
  status dict consumed by the dashboard. Walks rqlite +
  per-node /api/peer queries.
- `build_physical_topology(nodes_data)` — composes the
  topology view used by the dashboard's mesh-network diagram.

## SSH pool

- `_ssh_connect(host)` — paramiko connect with kerberos / key
  fallback. Cached connections by host so the dashboard's
  per-node queries don't open a fresh socket every time.
- `_ssh_pool_drop(host)` — drop on join-leave so a renamed
  joiner doesn't hit a stale cache.
- `ssh_cmd(host, cmd, timeout=10) -> str` — convenience.
- `ssh_cmd_rc(host, cmd, timeout=30) -> (out, rc)` — same with
  exit code.

## DRBD + per-VM helpers

- `parse_drbd_status(raw) -> dict` — parse `drbdadm status`
  output into a structured tree.
- `get_node_info(name, cfg) -> dict` — composite per-node
  view: DRBD state, libvirt VMs, mesh state, observability
  agent heartbeats.
- `get_vm_drbd_resource(host, vm_name) -> str` — match a VM
  back to its `tier-<name>` DRBD resource.
- `get_vm_disks(host, vm_name) -> list[dict]` — virsh dumpxml
  → list of `{target, source, size}`.
- `get_vm_vnc_port(host, vm_name) -> int` — for the dashboard's
  novnc embed.
- `get_witness_status() -> dict` — short HTTP query against the
  Echo's last reply (held in netd's witness state file written
  to `/run/bedrock/witness_state.json` for the dashboard).

## Endpoints (selected)

### Auth

- `POST /api/login` — body `{username, password}`. Runs
  `operator_auth.verify_password`, issues 24 h JWT, sets cookie
  + returns token.
- `GET /api/whoami` — requires Bearer token; returns `{username}`.
- `GET /api/operators` / `POST /api/operators/set` /
  `POST /api/operators/remove` — operator management.

### Join handshake

- `POST /api/join/request` — joiner posts hardware inventory +
  Ed25519 pubkey + cluster_uuid fingerprint. Master writes a
  pending `join_requests` row.
- `GET /api/join/status?id=<rid>` — joiner polls; on approval
  the response includes loopback_ip + cluster_key_hex.
- `POST /api/join/approve` — operator-authenticated. Wraps
  `join_handshake.approve_request`.

### Peer (inter-node)

- `GET /api/peer/test` (auth: `require_peer`) — sanity ping
  used by mgmt's per-node status walk.
- `GET /api/peer/witness` — return this node's `WitnessState`
  snapshot for the dashboard's witness panel.
- `GET /api/peer/drbd` — `drbdadm status --json`.
- `GET /api/peer/mesh` — `/run/bedrock/mesh_neighbors.json`.

### Cluster status

- `GET /api/cluster` — composite from `build_cluster_state()`.
  Powers the dashboard's home page.
- `GET /api/nodes/{name}` — per-node breakdown.

### VMs

- `GET /api/vms` — list.
- `POST /api/vms` — create. Delegates to `installer/lib/vm.py`.
- `DELETE /api/vms/{name}` — delete.
- `POST /api/vms/{name}/migrate` — live migrate.
- WebSocket `/api/vms/{name}/console` — proxies to novnc.

### ISOs

- `POST /api/isos` — upload (multipart); writes to the
  SeaweedFS filer's `/isos/` via the local S3 gateway.
- `GET /api/isos` — list from filer.
- `DELETE /api/isos/{name}`.

### Backups

- `POST /api/backups/targets` — register a kopia target;
  delegates to `mgmt/backup.py`.
- `GET /api/backups/jobs` — recent + scheduled.
- `POST /api/vms/{name}/backup` — trigger ad-hoc backup.
- `POST /api/vms/{name}/restore` — restore from snapshot id.

### Metrics + logs

- `GET /api/metrics/query?q=…` — proxy to VictoriaMetrics on
  the master.
- `GET /api/logs/query?q=…` — proxy to VictoriaLogs.

### Startup

- `@app.on_event("startup")` async — calls
  `orchestrator.start_all()` to spawn the asyncio tasks.

## TLS

- Started by uvicorn with `--ssl-keyfile /etc/bedrock/tls/server.key
  --ssl-certfile /etc/bedrock/tls/server.crt` on :8443.
- A separate plain-HTTP listener on loopback:8080 lets local CLI
  helpers (e.g. peer_auth signing via curl) skip the cert dance.
EOF
echo done