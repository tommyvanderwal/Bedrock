# mgmt/routes_obs.py

Observability read-API routes for the dashboard. Registers a handful of `GET`
endpoints on the mgmt FastAPI app that return pre-canned VictoriaMetrics and
VictoriaLogs queries — node/VM/DRBD time-series plus log search (global,
per-node, per-VM). It holds no state and runs no auth gate of its own; it is
read-only and shares the dashboard read API's security model. `app.py` calls
`register_routes(app)` once at startup, and every handler delegates to the peer
module `victoria.py` (`query_range`, `query_logs`).

## Functions / Classes

### `register_routes(app: FastAPI) -> None`
Attach the metrics and logs read endpoints to the given app.
- **In:** `app` — the mgmt FastAPI application to mount routes on.
- **Out:** `None`. Side effect: defines and registers six `GET` routes on
  `app` (closures over `time`, `query_range`, `query_logs`). No files, services,
  rqlite rows, or subprocesses of its own; at call time each endpoint performs
  HTTP reads against the configured Victoria backends via `victoria.py`.

Registered endpoints (the query string is fixed in code; only the time window
and step are caller-tunable):

- `GET /api/metrics/nodes?hours=1&step=30s` → `{cpu, mem, net_rx, net_tx}`, each
  a `query_range` result. CPU = `100 - idle%`; mem = used fraction; net_rx/tx =
  `node_network_*_bytes_total{device="br0"}` rate.
- `GET /api/metrics/vms?hours=1&step=30s` → `{cpu, disk_rd_iops, disk_wr_iops,
  disk_wr_lat}` over `bedrock_vm_*` series (`disk="0"`); write latency is
  write-time-ns / write-reqs / 1e6 (ms).
- `GET /api/metrics/drbd?hours=1&step=30s` → `{sent, received, out_of_sync}` from
  `bedrock_drbd_*` series.
- `GET /api/logs?query=*&limit=50&hours=1` → log entries for the raw LogsQL
  `query`, passed straight through.
- `GET /api/logs/node/{node_name}?limit=50&hours=1` → entries filtered with
  LogsQL `hostname:"<node_name>"`.
- `GET /api/logs/vm/{vm_name}?limit=50&hours=1` → entries matching the bare
  quoted term `"<vm_name>"` (no field).

In every endpoint `end = now`, `start = end - hours*3600`. Return shapes come
straight from `victoria.py`: `query_range` yields `{label: [[ts, val], ...]}`
(or `{"error": <str>}` on failure); `query_logs` yields a list of log-entry
dicts (or `[{"error": <str>}]` on failure). The metrics endpoints bundle several
`query_range` calls into one dict, so a single failed sub-query surfaces as an
`error`-keyed value while its siblings still return data.

## How it works

Each handler computes its window the same way (`end = now`,
`start = end - hours*3600`), hands a hardcoded query string plus `(start, end,
step)` for metrics or `(query, limit, start, end)` for logs to `victoria.py`,
and returns the result verbatim as the JSON body. The only client-supplied
inputs are `hours`, `step`, `limit`, and the path/query parameters that select
scope; the PromQL/LogsQL is baked in. There is no post-processing, caching,
merging, or auth check here.

```
HTTP GET (dashboard)
   │
   ▼
routes_obs handler        ── builds window + canned query string
   │
   ▼
victoria.query_range / query_logs
   │  picks obs_backends from cluster.json (local 127.0.0.1 first
   │  if this node is a backend), tries each until first 2xx
   ▼
VictoriaMetrics :8428  /  VictoriaLogs :9428
```

Failure handling lives entirely in `victoria.py`, which returns an
`error`-keyed payload rather than raising, so an endpoint never 500s on a
backend outage and always returns a JSON-serializable body. The log endpoints
differ only in the LogsQL they build: `/api/logs` passes `query` through
unchanged, `/api/logs/node/{name}` wraps it as a `hostname:"…"` field match, and
`/api/logs/vm/{name}` does a bare quoted-term match so any line mentioning the
VM name surfaces.
