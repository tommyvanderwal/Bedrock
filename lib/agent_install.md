# installer/lib/agent_install.py

Joiner-side install entry point for `bedrock join`. `install` runs the
`node_join` saga; this module also holds the join-handshake helpers the saga
reuses to register a new node with an existing cluster's mgmt API and exchange
SSH/peer keys. The full step-by-step join flow lives in
[`docs/sagas/node_join.md`](../../docs/sagas/node_join.md).

## Functions

### `install(witness, cluster_info, repo)`
Entry point for `bedrock join`; runs the node_join saga.
- **In:** `witness` — IP/hostname the CLI dialled to fetch cluster info (any
  current cluster node); `cluster_info` — that node's discovery dict (`mgmt_url`,
  `cluster_name`, `cluster_uuid`, existing `nodes`); `repo` — payload/repo URL.
- **Out:** returns `bedrock_d.install.node_join.run_node_join(witness,
  cluster_info, repo)` (the saga drives registration, the approval handshake,
  local state, rqlited join, SeaweedFS, and the dashboard).

### `_http_json(method, url, body=None, timeout=10.0)`
JSON GET/POST helper.
- **In:** HTTP method, URL, optional body dict, timeout.
- **Out:** parsed JSON (`{}` if empty). For HTTPS it uses `_INSECURE_CTX` so a
  bare-IP dial accepts the cluster's `<dashed-ip>.my.local-ip.co` cert.

### `_request_join(mgmt_url, node_name, host, bedrock_pubkey, x25519_eph_pub_b64, ssh_pubkey)`
POST `/api/join/request`.
- **Out:** response dict (carries `request_id`). Retries transient connection
  errors for a 30 s budget, then raises `RuntimeError`.

### `_poll_status(mgmt_url, request_id, *, timeout_s=600, interval_s=2.0)`
Block until the operator approves/rejects the join.
- **Out:** approval dict on `approved`; raises `RuntimeError` on `rejected`,
  `TimeoutError` after `timeout_s`. Swallows HTTP 404 (request not yet
  replicated) and transient connect errors; re-raises other HTTP errors.

### `_install_peer_pubkeys(pubkeys)`
Append each peer SSH pubkey to `/root/.ssh/authorized_keys` (dedup, dir 0o700,
file 0o600). No-op on an empty list.

### Module-level
- `_INSECURE_CTX` — shared SSL context with hostname/cert verification disabled,
  reused by `_http_json` for every HTTPS dial.

## How it works

`install` puts the source-tree root and `/usr/local/lib/bedrock` on `sys.path`,
imports `run_node_join`, and hands the whole join to the saga. The saga calls
`_request_join` / `_poll_status` for the approval handshake (the operator
compares the printed Ed25519 fingerprint and clicks Approve on the dashboard)
and `_install_peer_pubkeys` to trust peers' SSH keys.

```
bedrock join ──> install ──> run_node_join  (ordered, resumable saga)
                    │
                    └── _request_join → _poll_status → _install_peer_pubkeys
                          (approval handshake + peer-key exchange, reused by the saga)
```

## Why

Cert verification is disabled on the joiner's dials because the cluster cert is
issued for `<dashed-ip>.my.local-ip.co`, not the bare IP being dialled; peer
trust comes from the operator-confirmed Ed25519 fingerprint at the approval
popup, not from TLS PKI.
