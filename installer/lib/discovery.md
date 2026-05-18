# `discovery.py`

**Module purpose.** When the operator types `bedrock join
<witness-host-or-master-host>`, the joiner needs to find the
cluster's actual mgmt URL (https://...:8443). `discovery.py`
takes a discovery hint (IP, hostname, or witness device address)
and returns `{cluster_uuid, cluster_name, mgmt_url}`.

The default flow walks:

1. **bedrock-redirect** on port 80 — every Bedrock node runs
   `bedrock-redirect` (a tiny Python HTTP server) that responds
   to `GET /cluster-info` with a JSON blob `{cluster_uuid,
   cluster_name, mgmt_url}`. The joiner curls the hint host on
   :80 first.
2. **mDNS** (deferred for v1.0) — the cluster's mDNS responder
   advertises `_bedrock._tcp` records pointing at the mgmt URL.
3. **Witness** — if hint is a BedRock Echo, ask it for the
   last-known mgmt URL via a separate UDP message type. Not yet
   implemented; placeholder.

## Functions

- `discover_cluster(hint: str) -> dict` — main entry point.
  Tries each mechanism in order, returns the first successful
  result. Raises if all fail.
- `_probe_redirect(host: str) -> dict | None` — HTTP
  `GET http://<host>:80/cluster-info` with 5 s timeout. Returns
  the JSON dict or None on any failure.
- `_probe_mdns(name: str) -> dict | None` — placeholder for
  `_bedrock._tcp` mDNS-SD lookup; currently returns None.
