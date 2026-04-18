# HTTP and WebSocket API

The mgmt process (`bedrock-mgmt.service`, port 8080) exposes both a REST
API for actions and a single WebSocket for real-time state.

## Discovery / state

| Method | Path | Returns | Notes |
|---|---|---|---|
| GET | `/cluster-info` | `{cluster_name, cluster_uuid, nodes: [names], mgmt_url, witness_host}` | Used by `bedrock join` to learn cluster identity |
| GET | `/api/cluster` | full state: `{nodes, vms, witness}` | Served from **cached `_last_state`** — instant, updated every 3 s |
| GET | `/api/nodes` | `cluster.json` nodes object | |

## Node registration

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/nodes/register` | `{name, host, drbd_ip?, role?}` | `{status, cluster, nodes: [...], peer_ips: [...]}` |

Side effects: appends to `/etc/bedrock/cluster.json`, regenerates
`/opt/bedrock/scrape.yml`, POSTs `/-/reload` to VictoriaMetrics,
pushes `Node X (...) registered with cluster` log event.

## VM actions

All take `{vm_name}` in the path; return a JSON status blob.

| Method | Path | Body | Returns | Duration |
|---|---|---|---|---|
| POST | `/api/vms/{name}/start` | — | `{status, ...}` | ~instant |
| POST | `/api/vms/{name}/shutdown` | — | `{status}` | ~instant (guest takes longer) |
| POST | `/api/vms/{name}/poweroff` | — | `{status}` | ~instant |
| POST | `/api/vms/{name}/migrate` | `{target_node?: string}` | `{status, from, to, duration_s}` | ~1 s (testbed), ~3 s (physical) |
| POST | `/api/vms/{name}/convert` | `{target_type: "cattle"|"pet"|"vipet", peer_nodes?: [...]}` | `{status, from, to, duration_s?, resource?, peers?, added_peer?, dropped?}` | 4–15 s |

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

## Content types

All JSON bodies: `application/json`. No authentication today — the
dashboard assumes the mgmt network is trusted. Adding auth is a
hardening follow-up.
