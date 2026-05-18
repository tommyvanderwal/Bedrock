# `join_handshake.py`

**Module purpose.** Server-side helpers for the
`/api/join/{request,status,approve}` endpoints in `mgmt/app.py`.
Wraps the rqlite writes + the cluster-key encryption with a
clean per-step API so the FastAPI handlers stay small.

## Functions

- `create_join_request(node_state: dict, pubkey_hex: str) -> dict`
  — called by `/api/join/request`. Generates a `request_id`,
  writes a `join_requests` row in state `pending`, builds the
  per-request challenge (a fingerprint the operator sees on the
  dashboard). Returns
  `{request_id, fingerprint, state="pending"}`.
- `poll_status(request_id: str) -> dict` — short read on
  `join_requests` + `nodes`. Returns
  `{state, mgmt_master?, master_loopback_ip?, node_map?,
  cluster_key_hex?}`. The cluster_key_hex is only populated
  once the operator has approved.
- `approve_request(request_id: str, approver_username: str)
  -> dict` — called by `/api/join/approve` (operator-token-
  gated). Allocates the next free loopback index from the
  cluster CGNAT /24, writes `nodes` row + sets
  `join_requests.state = approved`, packages the
  cluster.key as hex into the response. Bumps revision.
- `reject_request(request_id, approver_username, reason="")` —
  state transition pending → rejected.
- `cleanup_stale_requests(max_age_s=86400)` — periodic helper
  used by the orchestrator's reactor; deletes pending requests
  older than 1 day so the join_requests table doesn't grow
  unbounded.

## Wire format (request approval response)

```json
{
  "state": "approved",
  "node_name": "bedrock-d98363",
  "loopback_ip": "100.117.97.2",
  "mgmt_master": "bedrock-ccd477",
  "master_loopback_ip": "100.117.97.1",
  "mgmt_url": "https://192.168.2.38:8443",
  "cluster_uuid": "27d5edb1-…",
  "cluster_name": "test-fresh",
  "cluster_key_hex": "<64 hex chars>",
  "operator_jwt_key_hex": "<64 hex chars>"
}
```

The joiner's `agent_install._poll_status` is what consumes this.
