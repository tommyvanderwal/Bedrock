# HTTP and WebSocket API

The mgmt API runs inside `bedrock-d` on every node. It exposes a REST API for
actions and a single WebSocket for real-time state, on two listeners:

- **`0.0.0.0:8443` (HTTPS)** — the operator dashboard + LAN-reachable API.
  Operator-authenticated (see [Authentication](#authentication)).
- **`127.0.0.1:8001` (HTTP)** — the local CLI / intra-process endpoint that
  the `bedrock` CLI dials. Auth-exempt (loopback is trusted local root).

## Discovery / state

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/cluster-info` | `{cluster_name, cluster_uuid, nodes: [names], mgmt_url, witness_host}` | Used by `bedrock join` to learn cluster identity. `nodes`/`cluster_name`/`cluster_uuid` come from `load_cluster()` (rqlite); `mgmt_url`/`witness_host` from local `state.json`. |
| GET | `/api/cluster` | full state: `{nodes, vms, witness}` | Served from **cached `_last_state`** — instant, updated every 3 s |
| GET | `/api/nodes` | the cluster `nodes` object | Sourced from rqlite via `load_cluster()` — same dict shape as the old `cluster.json` projection. |

## Node join

The old `/api/nodes/register` REST endpoint is gone; joining a node now runs
through the signed **join-handshake** flow (`bedrock join` → mgmt master).
On approval the handshake records the new node's identity into rqlite
(`node_register` + `node_loopback`) and ships `cluster.key`; topology is never
written to a local file.

| Method | Path | Body | Returns | Auth |
|---|---|---|---|---|
| POST | `/api/join/request` | `{node_name, host, bedrock_pubkey, x25519_eph_pubkey, ssh_pubkey}` | `{request_id, fingerprint}` | unauth (joiner has no identity yet) |
| GET | `/api/join/status?id=...` | — | approval status for a request | unauth |
| GET | `/api/join/pending` | — | list of pending join requests | operator |
| POST | `/api/join/approve` | `{request_id}` | `{status}` | operator |

## ISO library

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/isos` | — | `[{name, size_bytes}, ...]` |
| POST | `/api/isos` | multipart/form-data with `file` field | `{status, name, size_bytes}` |
| DELETE | `/api/isos/{name}` | — | `{status, name}` — 404 if not found |

Uploads stream in 1 MB chunks through the SeaweedFS FUSE mount to
`/mnt/bedrock/iso/<name>.iso`, replicated per the `/iso/`
collection policy (000 at N=1, 001 at N≥2). Path traversal is
blocked by `Path(name).name`.

## Import library  (VMware / Hyper-V / qcow2 → Bedrock)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/imports` | — | `[{id, original_name, input_format, status, virtual_size_gb?, detected_firmware?, detected_os_type?, ...}, ...]` |
| GET | `/api/imports/{id}` | — | one job + `log_tail` (last 4 KB of convert output) |
| POST | `/api/imports/upload` | multipart, `file` field | `{id, original_name, input_format, input_size_bytes, status: "uploaded"}` |
| POST | `/api/imports/{id}/convert` | `{inject_drivers?: bool}` (default `false`) | `{status: "converting", id, inject_drivers}` |
| POST | `/api/imports/{id}/create-vm` | `{name, vcpus=2, ram_mb=2048, priority="normal"}` | `{status: "created", name, node}` |
| DELETE | `/api/imports/{id}` | — | `{status: "deleted", id}` — wipes the whole `<id>/` dir |

`inject_drivers=true` takes the virt-v2v path (inspect guest, inject
viostor + NetKVM, edit Windows registry). `false` (default) uses
`qemu-img convert` — format conversion only, ~seconds for Linux guests.

## Export library

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/exports` | — | `[{id, vm, format, status, size_bytes?, created_at, error?}, ...]` |
| POST | `/api/vms/{name}/export` | `{format: "qcow2"|"vmdk"|"vhdx"|"raw"}` | `{id, vm, format, src_host, src_path, dst_path, status: "converting", created_at}` |
| GET | `/api/exports/{id}/download` | — | streaming `application/octet-stream` of the disk image (400 if status != ready) |
| DELETE | `/api/exports/{id}` | — | `{status: "deleted", id}` |

Local source → `qemu-img convert -p -f raw -O <fmt> <src_lv> <dst>`.
Cross-node source → `ssh host dd ... > fifo & qemu-img convert fifo dst`.

## VM actions

All take `{vm_name}` in the path; return a JSON status blob.

| Method | Path | Body | Returns | Duration |
|---|---|---|---|---|
| POST | `/api/vms/create` | `{name, vcpus=2, ram_mb=2048, disk_gb=20, priority="normal", iso?}` | `{status, name, node}` | 10–30 s (blank), 5–10 s (ISO boot) |
| POST | `/api/vms/{name}/start` | — | `{status, ...}` | ~instant |
| POST | `/api/vms/{name}/shutdown` | — | `{status}` | ~instant (guest takes longer) |
| POST | `/api/vms/{name}/poweroff` | — | `{status}` | ~instant |
| POST | `/api/vms/{name}/migrate` | `{target_node?: string}` | `{status, from, to, duration_s}` | ~1 s (testbed), ~3 s (physical) |
| POST | `/api/vms/{name}/convert` | `{target_type: "cattle"|"pet"|"vipet", peer_nodes?: [...]}` | `{status, from, to, duration_s?, resource?, peers?, added_peer?, dropped?}` | 4–15 s |
| DELETE | `/api/vms/{name}` | — | `{status, name}` | 2–10 s (includes DRBD teardown + `lvremove`) |
| GET | `/api/vms/{name}/settings` | — | full config blob (vcpus/ram_mb/disk_gb/priority/cdrom…) | ~instant |
| POST | `/api/vms/{name}/resources` | `{vcpus?, ram_mb?, disk_gb?}` | per-field `{applied, requires_reboot, note}` | instant (queue) or ~1 s (disk grow) |
| POST | `/api/vms/{name}/priority` | `{priority: "low"|"normal"|"high"}` | `{applied, priority, cpu_shares}` | instant |
| POST | `/api/vms/{name}/cdrom` | `{action: "eject"|"insert", iso?: string}` | `{applied, note}` | instant |

Typical migrate response:

```json
{
  "status": "migrated",
  "from":   "bedrock-sim-1.bedrock.local",
  "to":     "bedrock-sim-2.bedrock.local",
  "duration_s": 1.08
}
```

Typical convert cattle→pet response:

```json
{
  "status":   "converted",
  "from":     "cattle",
  "to":       "pet",
  "resource": "vm-webapp1-disk0",
  "duration_s": 4.24,
  "peers":    ["bedrock-sim-1.bedrock.local", "bedrock-sim-2.bedrock.local"]
}
```

## Console

| Method | Path | Returns |
|---|---|---|
| GET | `/console/{vm_name}` | 307 redirect to `/novnc/vnc.html?path=vnc/<vm>&autoconnect=true&resize=scale&reconnect=true` |
| GET | `/novnc/*` | Static noVNC HTML/JS bundle |
| WS | `/vnc/{vm_name}` | Bi-directional TCP proxy to `ws://<host>:<vnc-port>` on the VM's host |

The `/vnc/{name}` WebSocket looks up the running host and VNC port from
the current state, opens a TCP socket to the host's VNC server, and
proxies bytes in both directions. Client (noVNC in the browser) sends
RFB, server responds with VNC framebuffer — no websockify on cluster
nodes needed.

## Metrics queries (thin wrappers around VictoriaMetrics)

| Method | Path | Query used on VM |
|---|---|---|
| GET | `/api/metrics/nodes?hours=H&step=S` | `sum by (instance)(rate(node_cpu_seconds_total{mode!="idle"}[$step])) * 100` etc. — returns `{cpu, mem, net_rx, net_tx}` maps |
| GET | `/api/metrics/vms?hours=H&step=S` | libvirt_* metrics from vm_exporter — returns `{cpu, disk_wr_iops, disk_wr_lat, disk_rd_iops}` maps |
| GET | `/api/metrics/drbd?hours=H&step=S` | DRBD per-resource metrics |

Shape per map: `{ "<series-label>": [[ts, val], ...] }`.

## Log queries (thin wrappers around VictoriaLogs)

| Method | Path | LogsQL used |
|---|---|---|
| GET | `/api/logs?query=...&limit=L&hours=H` | passthrough |
| GET | `/api/logs/node/{name}?limit=L&hours=H` | `hostname:"<name>"` |
| GET | `/api/logs/vm/{name}?limit=L&hours=H` | `"<name>"` (free text in _msg) |

Response: JSON array of entries `{_time, _msg, hostname, app, level}`
sorted VL-native (operators should sort client-side for newest-first).

## WebSocket `/ws`

The dashboard opens a single WebSocket per browser tab. The first frame
after `accept` carries the cached cluster state; from there, additional
frames stream on the channels below.

**Incoming (server → client):**

| `channel` | Payload |
|---|---|
| `cluster` | Full state snapshot `{nodes, vms, witness}`. Sent once on connect (from `_last_state`), then every 3 s from the state push loop. |
| `event` | Log event `{_msg, _time, hostname, app, level}`. Pushed **immediately** by `push_log()` (before the VL insert). |
| `vm.state` | Reserved — currently unused by the server; the UI subscribes for future fine-grained VM updates. |
| `rpc.response` | `{id, result}` or `{id, error}` for an earlier `rpc` request. |

**Outgoing (client → server):**

| `channel` | Payload |
|---|---|
| `rpc` | `{id, method, params}`; supported methods: `vm.start`, `vm.shutdown`, `vm.poweroff`, `vm.migrate`. |

RPC over WS mirrors the REST endpoints; the dashboard uses REST today,
the RPC path is wired for low-latency future use (e.g., bulk-action
buttons).

## Error shapes

REST endpoints return:

- `200 OK` with JSON body on success.
- `400 Bad Request` — precondition failed (e.g. "cattle cannot
  migrate", "requires ≥ 2 nodes").
- `404 Not Found` — unknown VM.
- `500 Internal Server Error` — unexpected; body contains `{detail: "<message>"}` with the first line of the underlying failure.

WebSocket frames never "error" — the server closes the connection
with a close code on fatal faults; the client auto-reconnects after 2 s.

## Authentication

The LAN listener (`0.0.0.0:8443`) is **operator-authenticated**. Auth is
Ed25519-based (see `installer/lib/operator_auth.py`); `_auth_middleware` /
`require_operator` in `mgmt/app.py` enforce it on every non-public route.

The loopback listener (`127.0.0.1:8001`) is **auth-exempt** — local root is
already privileged, and the `bedrock` CLI dials it directly. A handful of
routes are public on both listeners (`/`, `/login`, `/api/login`,
`/cluster-info`, `/health`, `/api/join/request`, `/api/join/status`).

## Content types

All JSON bodies: `application/json`.
