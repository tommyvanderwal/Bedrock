# HTTP and WebSocket API

The mgmt API runs inside `bedrock-d` on every node (`mgmt/app.py` plus the
`mgmt/routes_*.py` modules). It serves the operator dashboard, a REST API for
actions, and a single WebSocket for real-time state, on three listeners:

- **`0.0.0.0:8443` (HTTPS)** — operator dashboard + LAN-reachable API.
  Operator-authenticated (see [Authentication](#authentication)). Bound only
  when a TLS cert/key exist under `/etc/bedrock/tls/`.
- **`127.0.0.1:8001` (HTTP)** — local CLI / intra-process endpoint that the
  `bedrock` CLI dials. Loopback-only, auth-exempt (local root is privileged).
- **`0.0.0.0:8444` (HTTP)** — bootstrap-only, bound instead of 8443 while no
  TLS cert exists yet, so a joiner can fetch `/cluster-info` / `/api/cluster`
  before the first cert lands. The next restart after a cert appears uses 8443.

The same `app` runs on every listener; the auth middleware distinguishes them
by client IP (loopback is exempt).

## Discovery / state

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/cluster-info` | `{cluster_name, cluster_uuid, nodes: [names], mgmt_url, witness_host}` | Discovery for `bedrock join`. `cluster_name`/`nodes` from `load_cluster()` (rqlite); `cluster_uuid`/`mgmt_url`/`witness_host` overlaid from local `state.json`. Public. |
| GET | `/api/cluster` | cached full state `{nodes, vms, witness, topology}` | Served from in-memory `_last_state` — instant, refreshed every 3 s by the state push loop. |
| GET | `/api/topology` | `{switches, links, node_count, switch_count, link_count, computed_at}` | Physical L2 rollup (LLDP/CDP/MNDP per NIC, mesh node-to-node links), grouped by device MAC. View-only; never folded into cluster state. |
| GET | `/api/nodes` | the cluster `nodes` dict | From rqlite via `load_cluster()`. |
| GET | `/api/tasks` | active + recently-finished tasks | Snapshot for a fresh page load; live updates arrive on the WS `task` channel. |
| GET | `/api/tasks/{task_id}` | one task | 404 once aged out. |

`load_cluster()` reads the local rqlite replica at level `none`, so discovery
and state work without quorum.

## Node join

Joining runs through the signed join-handshake flow (`bedrock join` → mgmt
master). The joiner has no identity yet, so `/api/join/request` is unauth; the
request id is an unguessable 192-bit handle the joiner polls. The operator
visually verifies the Ed25519 fingerprint and approves. On approval the master
seals `cluster.key` under an X25519-ECDH session key (AEAD), records the node
into rqlite (`node_register` + `node_loopback`), and fans the joiner's SSH
pubkey out to every node's `authorized_keys`. No topology is written to a local
file.

| Method | Path | Body | Returns | Auth |
|---|---|---|---|---|
| POST | `/api/join/request` | `{node_name, host, bedrock_pubkey, x25519_eph_pubkey, ssh_pubkey}` | `{request_id, fingerprint}` | public (joiner has no identity yet) |
| GET | `/api/join/status?id=...` | — | `{state}`; when `approved`, the ECDH bundle + cluster membership the joiner needs (`master_eph_pubkey, ciphertext, nonce, cluster_name, cluster_uuid, node_map, node_cert_pem, ca_cert_pem, …`) | public |
| GET | `/api/join/pending` | — | `{pending: [...]}` | operator |
| POST | `/api/join/approve` | `{request_id}` | `{state: "approved", loopback_ip}` | operator |
| POST | `/api/join/reject` | `{request_id}` | `{state: "rejected"}` | operator |

## Operators / auth

| Method | Path | Body | Returns | Auth |
|---|---|---|---|---|
| POST | `/api/login` | `{username, password}` | `{token, exp, user}` | public; per-IP rate-limited (5 fails/min) |
| GET | `/api/whoami` | — | `{user}` | operator |
| GET | `/api/operators` | — | `{operators: [names]}` (hashes never exposed) | operator |
| POST | `/api/operators/set` | `{username, password}` | `{username, status: "set"}` | operator (upsert; min 4-char password) |
| POST | `/api/operators/remove` | `{username}` | `{username, status: "removed"}` | operator (refuses self or last operator) |
| GET | `/api/peer-test` | — | `{verified_caller}` | peer (Ed25519) — smoke test for inter-node signing |

## ISO library

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/isos` | — | `[{name, size_bytes}, ...]` (`.iso`, case-insensitive) |
| POST | `/api/isos` | multipart, `file` field | `{status: "uploaded", name, size_bytes}` |
| DELETE | `/api/isos/{name}` | — | `{status: "deleted", name}` — 404 if absent |

Uploads stream in 1 MB chunks straight to the SeaweedFS FUSE mount at
`/mnt/bedrock/iso/<name>.iso` (extension normalised to lowercase `.iso`),
replicated per the `/iso/` collection policy. Writing through the FUSE mount
means every node sees the file, so `virt-install --cdrom` works from anywhere.
Path traversal is blocked by `Path(name).name`.

## Import library  (VMware / Hyper-V / qcow2 → Bedrock)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/imports` | — | `[{id, original_name, input_format, status, ...}, ...]`, newest first |
| GET | `/api/imports/{id}` | — | one job + `log_tail` (last 4 KB of convert output) |
| POST | `/api/imports/upload` | multipart, `file` field | `{id, original_name, input_format, input_path, input_size_bytes, status: "uploaded", created_at}` |
| POST | `/api/imports/{id}/convert` | `{inject_drivers?: bool}` | `{status: "converting", id, inject_drivers}` |
| POST | `/api/imports/{id}/create-vm` | `{name, vcpus=2, ram_mb=2048, priority="normal"}` | `{status: "accepted", task_id, name, import_id}` |
| DELETE | `/api/imports/{id}` | — | `{status: "deleted", id}` — wipes the whole `<id>/` dir |

Upload accepts `.ova/.ovf/.vmdk/.vhd/.vhdx/.qcow2/.raw/.img` and inspects the
guest OS (virt-inspector, with a Hyper-V→Windows fallback) so the UI can show
the detected OS and pre-select driver injection.

`inject_drivers` omitted ⇒ auto-selected from the detected OS (Windows ⇒ true).
`true` takes the virt-v2v path (inject viostor + NetKVM, edit Windows registry);
`false` uses `qemu-img convert` — format conversion only, ~seconds for Linux
guests. `create-vm` is fire-and-forget (returns a `task_id`); watch progress on
the WS `task` channel.

## Export library

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/exports` | — | `[{id, vm, format, status, src_host, src_path, dst_path, created_at, size_bytes?, error?}, ...]`, newest first |
| POST | `/api/vms/{name}/export` | `{format: "qcow2"\|"vmdk"\|"vhdx"\|"raw"}` | the meta blob (status `"converting"`) |
| GET | `/api/exports/{id}/download` | — | streaming `application/octet-stream` (400 if status != ready) |
| DELETE | `/api/exports/{id}` | — | `{status: "deleted", id}` |

Local source → `qemu-img convert -p -f raw -O <fmt> <src> <dst>`.
Cross-node source → `ssh host dd … > fifo & qemu-img convert fifo dst`.
Conversion runs live (QEMU reads the raw/DRBD LV consistently).

## VM actions

All take `{vm_name}` in the path.

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| POST | `/api/vms` | `{name, vcpus=2, ram_mb=2048, disk_gb=20, priority="normal", iso?, vm_type="cattle", extra_disks?}` | `{status: "accepted", task_id, name, intent_revision}` | Create. Fire-and-forget; validates synchronously (name, type vs cluster size, ISO) then runs the `vm_create` saga. `vm_type` ∈ cattle/pet/vipet (pet ≥2 nodes, vipet ≥3). |
| POST | `/api/vms/{name}/start` | — | `{status: "started", node}` | Promotes DRBD if pet/vipet, then `virsh start`. |
| POST | `/api/vms/{name}/stop` | — | `{status: "shutdown sent"}` | ACPI shutdown (`virsh shutdown`). |
| POST | `/api/vms/{name}/force-stop` | — | `{status: "powered off"}` | `virsh destroy`. |
| POST | `/api/vms/{name}/migrate` | `{target_node?}` | saga result `{op_id, state, last_step}` | Live-migrate via `vm_migrate` saga; default target is the VM's backup peer. Records post-promote UUID so HA survives the move. |
| POST | `/api/vms/{name}/ha-level` | `{vm_type: "cattle"\|"pet"\|"vipet", peer_nodes?}` | `{status: "accepted", task_id, from, to}` or `{status: "no-op", current}` | Change replication level. Validates peer availability up front; runs async. |
| DELETE | `/api/vms/{name}` | — | `{status: "accepted", task_id, name}` | `vm_destroy` saga: DRBD teardown + `lvremove`. |
| GET | `/api/vms/{name}/settings` | — | full config blob (below) | — |
| POST | `/api/vms/{name}/compute` | `{vcpus?, ram_mb?, disk_gb?}` | per-field `{applied, requires_reboot, note}` | vCPU/RAM queue for next boot; disk grows live (no shrink). |
| POST | `/api/vms/{name}/priority` | `{priority: "low"\|"normal"\|"high"}` | `{applied, requires_reboot, priority, cpu_shares}` | Sets cpu_shares live + mirrors to rqlite for self-heal ordering. |
| POST | `/api/vms/{name}/cdrom` | `{action: "eject"\|"insert", iso?}` | `{applied, requires_reboot, note}` | — |
| POST | `/api/vms/{name}/disks` | `{size_gb}` | `{status: "attached", target, lv, size_gb}` | Live-attach a new thin LV as the next `vd*` (local LV only; not auto-DRBD). |

`extra_disks` is a list of `{size_gb}` for additional `vdb, vdc, …` disks.

`GET /api/vms/{name}/settings` returns:

```json
{
  "name": "...", "host": "...",
  "vcpus": 2, "ram_mb": 2048, "disk_gb": 20,
  "disk_path": "/dev/...", "disk_target": "vda",
  "drbd_resource": "vm-...-disk0",
  "cdrom_slot": "sda", "cdrom_iso": "alma.iso",
  "priority": "normal", "cpu_shares": 1024
}
```

Create / migrate / delete all run the `bedrock_d/vm/*` sagas on the mgmt master
(this process holds DRBD/arbiter authority). The CLI is a thin HTTP client.

## Generic operations (sagas)

The single surface for submitting any saga directly (the `bedrock` CLI uses it).

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| POST | `/api/operations` | `{kind, target_node?, params?, wait=true}` | `{op_id, kind, state, last_step, error}`; `wait=false` ⇒ `{op_id, state: "pending"}` (202-style) |
| POST | `/api/operations/{op_id}/retry` | — | `{op_id, state, last_step, error}` — re-runs from the first not-`done` step |
| GET | `/api/operations/{op_id}` | — | `{op, steps}` (op + per-step log) |
| GET | `/api/operations` | `?kind=&state=&limit=50` | `[op, ...]`, newest first |

`kind` ∈ the registered sagas (`vm_create`, `vm_destroy`, `vm_grow`,
`vm_migrate`, `cluster_init`, `node_join`, `node_leave`, `cluster_tier`,
`cluster_rename`, `replica_repair`, …); unknown kind ⇒ 400. All operator-gated.

## Console

| Method | Path | Returns |
|---|---|---|
| GET | `/console/{vm_name}` | 307 redirect to `/novnc/vnc.html?path=vnc/<vm>&autoconnect=true&resize=scale&reconnect=true` (or an HTML notice if the VM isn't running) |
| GET | `/novnc/*` | Static noVNC bundle |
| WS | `/vnc/{vm_name}` | Bi-directional TCP proxy to the VM host's VNC server |

The `/vnc/{name}` WebSocket looks up the running host + VNC port from the
current state, opens a raw TCP socket to that host's VNC server, and proxies
bytes both ways (client RFB ↔ server framebuffer). No websockify on cluster
nodes. Echoes the `binary` subprotocol only if the client offered it.

## Metrics queries (thin wrappers over VictoriaMetrics)

| Method | Path | Returns |
|---|---|---|
| GET | `/api/metrics/nodes?hours=H&step=S` | `{cpu, mem, net_rx, net_tx}` |
| GET | `/api/metrics/vms?hours=H&step=S` | `{cpu, disk_rd_iops, disk_wr_iops, disk_wr_lat}` (from `bedrock_vm_*`) |
| GET | `/api/metrics/drbd?hours=H&step=S` | `{sent, received, out_of_sync}` |

Defaults: `hours=1`, `step=30s`. Each map value is
`{ "<series-label>": [[ts, val], ...] }`. On backend failure the map is
`{"error": "<msg>"}`.

## Log queries (thin wrappers over VictoriaLogs)

| Method | Path | LogsQL used |
|---|---|---|
| GET | `/api/logs?query=...&limit=L&hours=H` | passthrough (`query` defaults to `*`) |
| GET | `/api/logs/node/{name}?limit=L&hours=H` | `hostname:"<name>"` |
| GET | `/api/logs/vm/{name}?limit=L&hours=H` | `"<name>"` (free text in `_msg`) |

Defaults: `limit=50`, `hours=1`. Response: JSON array of VL entries
(`{_time, _msg, hostname, app, level}`), VL-native order — sort client-side for
newest-first.

## WebSocket `/ws`

The dashboard opens one WebSocket per browser tab. Auth is the operator token
passed as a `?token=` query param (the browser WebSocket API can't set
headers); on failure the server closes with code 1008. The first frame after
`accept` carries the cached cluster state.

**Incoming (server → client):**

| `channel` | Payload |
|---|---|
| `cluster` | Full state `{nodes, vms, witness, topology}`. Sent once on connect (from `_last_state`), then every 3 s from the push loop. |
| `event` | Log event `{_msg, _time, hostname, app, level}`. Pushed immediately by `push_log()` (before the VL insert, so the UI stays responsive). |
| `task` | `{event, task}` where `event` ∈ `task.create` / `task.update`. Drives live progress for fire-and-forget actions (create, delete, ha-level, import). |
| `rpc.response` | `{id, result}` or `{id, error}` for an earlier `rpc` request. |

**Outgoing (client → server):**

| `channel` | Payload |
|---|---|
| `rpc` | `{id, method, params}`; methods: `vm.start`, `vm.shutdown`, `vm.poweroff`, `vm.migrate`. Mirrors the REST handlers. |

The dashboard drives most actions over REST + the `task` channel; the `rpc`
path is wired for low-latency single-call actions.

## Error shapes

REST endpoints return:

- `200 OK` with a JSON body on success (fire-and-forget actions return
  `{status: "accepted", task_id, ...}`).
- `400 Bad Request` — precondition failed (e.g. "cattle cannot migrate",
  "pet requires ≥1 peer", invalid name/format).
- `404 Not Found` — unknown VM / job / request.
- `409 Conflict` — VM name already exists.
- `429 Too Many Requests` — login rate limit.
- `503 Service Unavailable` — rqlite / saga executor unreachable.
- `500 Internal Server Error` — `{detail: "<message>"}` with the first line of
  the underlying failure (e.g. a saga that failed at a named step).

WebSocket frames don't carry errors — the server closes with a close code on a
fatal fault; the client auto-reconnects.

## Authentication

The LAN listener (`0.0.0.0:8443`) is **operator-authenticated**. Auth is
Ed25519-based (`installer/lib/operator_auth.py`); `_auth_middleware` enforces it
on every `/api/*` and `/ws` route, and `require_operator` is the per-route
FastAPI dependency. A request may instead carry a peer Ed25519 signature
(`Authorization: Bedrock-Ed25519 …`) for inter-node calls; `require_peer` /
`require_operator_or_peer` cover those.

The loopback listener (`127.0.0.1:8001`) is **auth-exempt** — the middleware
short-circuits when the client IP is `127.0.0.1`/`::1`. Local root is already
privileged, and the `bedrock` CLI dials it directly. Spoofed-loopback from a
real NIC is dropped by rp_filter/martian filtering.

Public on every listener: `/`, `/login`, `/cluster-info`, `/health`,
`/api/login`, `/api/join/request`, `/api/join/status`, the static dashboard
assets (`/_app/`, `/static/`, `/assets/`, `/favicon`), and any non-`/api/`,
non-`/ws` path (SvelteKit pages — the route guard redirects to `/login`).

## Content types

All JSON bodies: `application/json`. Uploads (ISO / import): `multipart/form-data`.
