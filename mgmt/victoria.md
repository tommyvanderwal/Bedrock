# mgmt/victoria.py

Query client the Bedrock dashboard uses to read metrics from VictoriaMetrics
(port 8428) and logs from VictoriaLogs (port 9428), and to push its own log
lines. It resolves the cluster's designated observability backends from
`cluster.json`, tries them in order, and returns the first 2xx response. It is
not a merging proxy: vmagent/vlagent dual-write make both backends carry
identical data, so any single backend's answer is correct.

## Functions / Classes

### `query_range(promql, start=None, end=None, step="15s") -> dict`
Range query against VictoriaMetrics `/api/v1/query_range`.
- **In:** `promql` PromQL string; `start`/`end` unix seconds (default last hour: `end=now`, `start=now-3600`); `step` resolution string.
- **Out:** `{label: [[ts, float_val], ...]}` keyed by `_label_key`. On any error returns `{"error": str(e)}`. Issues an HTTP GET to a metrics backend; no other side effects.

### `query_instant(promql) -> dict`
Instant query against VictoriaMetrics `/api/v1/query`.
- **In:** `promql` PromQL string.
- **Out:** `{label: float_val}` keyed by `_label_key`. On any error returns `{}`. HTTP GET to a metrics backend.

### `query_logs(logsql, limit=50, start=None, end=None) -> list[dict]`
LogsQL query against VictoriaLogs `/select/logsql/query`.
- **In:** `logsql` query string; `limit` max rows; `start`/`end` unix seconds (passed only when set/truthy).
- **Out:** list of parsed JSON-line log entries. On any error returns `[{"error": str(e)}]`. HTTP GET to a logs backend.

### `push_log(msg, node="mgmt", app="bedrock", level="info") -> None`
Dual-write a structured log line to **every** logs backend.
- **In:** `msg` message text; `node` hostname label; `app` app label; `level` severity label.
- **Out:** nothing. POSTs a JSON object (`_msg`, `_time` local-time `%Y-%m-%dT%H:%M:%S`, `hostname`, `app`, `level`) to `/insert/jsonline` on each VL backend with a 2 s timeout. Best-effort: every failure is swallowed silently.

### Private helpers
- `_backend_hosts(kind)` — reads `obs_backends[kind]` and `nodes` from `cluster_state.load_cluster()` (loaded via `/usr/local/lib/bedrock`); maps each backend node to an address (`loopback_ip` → `drbd_ip` → `host`, skipping empties), substituting `127.0.0.1` and ordering it first when the backend is this node (`socket.gethostname()`); returns `[]` on any failure.
- `_vm_urls()` / `_vl_urls()` — wrap the resolved hosts into `http://<addr>:8428` / `http://<addr>:9428`, falling back to `["127.0.0.1"]` when none resolve.
- `_try(urls, path, params, data, headers, timeout=5.0)` — GET/POST each URL in turn, returning the first successful body bytes; re-raises the last exception (or `RuntimeError("no backends configured")`) if all fail.
- `_label_key(metric)` — derives a display label: `vm` → `resource` → `instance` (last dotted/colon-split component rendered `node<oct>` for `141`/`142`, else the raw instance) → `__name__` → `"unknown"`.

## How it works

Backend selection is data-driven, then ordered for locality:

```
cluster.json
  obs_backends.metrics = [nodeA, nodeB]   obs_backends.logs = [...]
  nodes[name].loopback_ip | drbd_ip | host
        │
        ▼
_backend_hosts(kind):
  for name in backends:
      addr = loopback_ip or drbd_ip or host   (skip if empty)
      if name == this host:  addrs.insert(0, "127.0.0.1")   # local first
      else:                  addrs.append(addr)
        │
        ▼
_vm_urls/_vl_urls → ["http://<addr>:8428|9428", ...]   (or ["127.0.0.1"])
        │
        ▼
_try(urls, ...): walk URLs, return first 2xx body; raise last error if all fail
```

Local `127.0.0.1` is tried first when this node is itself a backend — it saves a
LAN hop and is the only path that still works during a brief LAN blip.

The three read functions share the same shape: build params, call `_try` over
the resolved URLs (5 s default timeout per URL), parse the response, and on
**any** exception return an empty result wrapped in the function's normal return
type (dict, dict, list). `_try` appends a urlencoded query string to the path and
catches each per-URL error, continuing the loop, so a single backend being down
just falls through to the next. VL log responses arrive as newline-delimited
JSON, so `query_logs` splits and parses line by line.

`push_log` does not fall through on first success: it POSTs to *every* logs
backend so mgmt's own lines are replicated the same way agent-scraped syslog is,
and it never raises (each backend attempt is independently best-effort).

## Why

Single-backend reads are correct because both backends hold identical data, so
there is no need to merge or quorum across them; the client only needs the
nearest reachable one.
