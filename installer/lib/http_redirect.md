# `http_redirect.py`

**Module purpose.** Tiny HTTP server on `:80` that serves two
endpoints:

- `GET /cluster-info` — JSON blob `{cluster_uuid,
  cluster_name, mgmt_url, mgmt_master, master_loopback_ip}`.
  Used by `discovery.py` (the joiner's `bedrock join` calls
  this on the discovery hint host).
- `GET /` — `302 Location: https://<this-host>:8443/`. Lets a
  human typing `http://<node>/` reach the dashboard.

Runs as `bedrock-redirect.service` (separate from
`bedrock-mgmt.service` so it can bind :80 with minimal
privileges and isn't blocked by mgmt restarts).

## Functions

- `main()` — entry point. Reads cluster.json + state.json,
  serves the two endpoints. Re-reads files on every request so
  failover changes (mgmt_master moved) are reflected
  immediately.
- `_cluster_info_response() -> bytes` — JSON serialiser.
- `_redirect_response() -> bytes` — 302 with the Location
  header.
