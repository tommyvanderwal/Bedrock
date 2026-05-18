# `mgmt/victoria.py`

**Module purpose.** Query helpers for VictoriaMetrics (PromQL) +
VictoriaLogs (LogsQL). Used by the dashboard's per-node
"recent CPU / memory / disk" tiles and the log-tail view.

## Functions

- `query_promql(expr: str, *, start=None, end=None, step="60s")
  -> dict` — POST to vm's `/api/v1/query_range`. Returns the
  parsed result.
- `query_logsql(expr: str, *, start=None, end=None, limit=200)
  -> list[dict]` — POST to vl's `/select/logsql/query`. Returns
  the parsed log lines.
- `vm_backends() -> list[str]` — read cluster.json's
  `obs_backends.metrics`, return the list of loopback IPs we
  can dial.
- `vl_backends() -> list[str]` — same for `obs_backends.logs`.
- `pick_backend(backends) -> str` — round-robin pick, with a
  short cache so repeated dashboard queries within a window go
  to the same backend.

Falls back to the local node if the backends list is empty (true
at N=1 before observability is bootstrapped).
