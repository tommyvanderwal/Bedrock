# installer/lib/agent_install.py

Joiner-side install path for a secondary node running `bedrock join`. Its job is
to register a fresh node with an existing cluster's mgmt API, complete the
approval handshake, and stand up the local stack (exporters, storage tiers,
`bedrock-d`, rqlited, SeaweedFS, dashboard). By default `install()` delegates to
the `node_join` saga; the procedural body in this module is the
`BEDROCK_INIT_SAGA=0` opt-out. Called by the `bedrock join` CLI flow.

## Functions / Classes

### `install(witness, cluster_info, repo)`
Joiner-side install entry point.
- **In:** `witness` — IP/hostname the CLI dialled to fetch cluster info (any
  current cluster node, not necessarily the master or the Echo witness host;
  also stored as `witness_host`); `cluster_info` — that node's discovery dict
  (`mgmt_url`, `cluster_name`, `cluster_uuid`, existing `nodes`); `repo` —
  payload/repo location passed to the exporter and dashboard installers.
- **Out:** returns the result of `bedrock_d.install.node_join.run_node_join(...)`
  unless `BEDROCK_INIT_SAGA=0`, in which case it runs the procedural body and
  returns `None`. Procedural side effects: writes `/etc/bedrock/cluster.key`
  (0o600), `/etc/bedrock/cluster.json`, updates `/etc/bedrock/state.json`, adds
  peer pubkeys to `/root/.ssh/authorized_keys`, scans peer host keys into
  `/root/.ssh/known_hosts`, creates `/mnt/bedrock`; installs exporters, sets up
  N=1 storage tiers, writes the cluster HMAC key, pre-extracts
  `/opt/bedrock/mgmt`, enables/starts `bedrock-d.service`, sets rp_filter/forward
  sysctls, renders the rqlited env and restarts `bedrock-rqlited.service`,
  installs+starts SeaweedFS master/volume, FUSE-mounts the ISO library, installs
  the dashboard.

### Private helpers
- `_http_json(method, url, body=None, timeout=10.0)` — JSON GET/POST helper;
  uses an insecure SSL context (`check_hostname=False`, `CERT_NONE`) for HTTPS so
  the bare-IP dial accepts the cluster's `<dashed-ip>.my.local-ip.co` cert.
  Returns the parsed JSON body (or `{}` if empty).
- `_request_join(mgmt_url, node_name, host, bedrock_pubkey, x25519_eph_pub_b64,
  ssh_pubkey)` — POSTs `/api/join/request`; retries transient connection errors
  for a 30 s budget, then raises `RuntimeError`. Returns the response dict
  (carries `request_id`).
- `_poll_status(mgmt_url, request_id, *, timeout_s=600, interval_s=2.0)` — polls
  `/api/join/status?id=…` every `interval_s` until `approved` (returns dict),
  `rejected` (raises `RuntimeError`), or `timeout_s` (raises `TimeoutError`).
  Swallows HTTP 404 (request not yet replicated) and transient connect errors;
  re-raises other HTTP errors.
- `_install_peer_pubkeys(pubkeys)` — appends each peer SSH pubkey to
  `/root/.ssh/authorized_keys` (dedup, dir 0o700, file 0o600); no-op on empty
  list.

### Module-level
- `_INSECURE_CTX` — shared SSL context with hostname/cert verification disabled,
  reused by `_http_json` for every HTTPS dial.

## How it works

By default `install()` prepends the repo root and `/usr/local/lib/bedrock` to
`sys.path`, imports `run_node_join`, and hands the whole join off to the saga.
The procedural path below runs only when `BEDROCK_INIT_SAGA=0`.

The procedural join is an ordered sequence with the approval handshake gating
everything after it:

```
load state.json ──► pick mgmt_ip (br0 UP first, else first UP non-169.254)
      │
      ▼
install exporters (mgmt rewrites scrape.yml to include us)
      │
      ▼
read /root/.ssh/id_ed25519.pub  +  peer_auth.pubkey_hex()  (Ed25519 identity)
      │
      ▼
APPROVAL HANDSHAKE
   gen_ephemeral() ──► _request_join() ──► request_id
   print fingerprint; operator compares + clicks Approve on dashboard
   _poll_status() ── blocks ──► approval{master_eph_pubkey, ciphertext, nonce, …}
   open_seal(ECDH) ──► cluster.key  ──► /etc/bedrock/cluster.key (0o600)
      │
      ▼  (only now is it safe to commit local state)
state.save: role=compute, node_id, node_name, witness_host, mgmt_url,
            mgmt_ip, loopback_ip
      │
      ▼
bootstrap /etc/bedrock/cluster.json (node_map + self entry + empty sections,
            log_index=0) — lets rqlite_setup render its env before the
            canonical fold lands
      │
      ▼
_install_peer_pubkeys()  +  ssh-keyscan peer_ips ──► known_hosts (sort -u)
mkdir /mnt/bedrock ;  tier_storage.setup_n1()  (local LVs only)
daemon_setup.write_cluster_key(cluster_key_hex)  (witness HMAC secret)
pre-extract mgmt.tar.gz ──► /opt/bedrock
      │
      ▼
systemctl enable --now bedrock-d.service ;  sysctl rp_filter=2, ip_forward=1
      │
      ▼
render rqlited env ; restart bedrock-rqlited.service ; poll :4001/status (mTLS)
   (waits up to ~15 s for master loopback ping, then for local rqlite to answer)
      │
      ▼
SeaweedFS: ensure_install, write configs, promote_to_master_volume_host,
           ensure_iso_library_mount
      │
      ▼
dashboard_install.install_dashboard(repo, with_metrics=False)
```

Ordering that matters:
- The approval handshake is the gate. State is committed only after the master
  returns the sealed `cluster.key`, so a rejected or unreachable join leaves no
  half-written local identity.
- `cluster.json` is bootstrapped before rqlited starts because `rqlite_setup`
  needs the nodes dict (peer loopbacks + node-id) to render its env file; the
  mgmt snapshot watcher later overwrites it with the canonical fold.
- `bedrock-d` is started before rqlited so the mesh thread installs the `/32`
  routes rqlited needs to `-join` the leader at `100.X.Y.1:4002`; rqlited starts
  via `Requires=bedrock-d.service`. The unit is `restart`ed (not `enable`d)
  because its `WantedBy=` is empty by design — the saga executor in `bedrock-d`
  owns its lifecycle.
- The rqlited start first pings the master's loopback up to ~15 s (30×0.5 s) so
  the `-join` target is reachable, then polls local `:4001/status` over mTLS up
  to ~15 s for readiness.
- SeaweedFS master+volume run on every node; filer/s3 stay stopped on followers
  and are promoted only if/when this node becomes mgmt master
  (`cluster_arbiter`).

Failure handling: every post-handshake step (`tier_storage.setup_n1`, cluster
key write, mgmt pre-extract, `bedrock-d` start, rqlited start, SeaweedFS,
dashboard) is wrapped so a failure prints a `WARN` and continues rather than
aborting the join. `_request_join` and `_poll_status` tolerate transient errors
(retry within their deadlines) since the master's mgmt API may still be warming
up or the mesh path may be settling.

## Why

Cert verification is disabled on the joiner's dials because the cluster cert is
issued for `<dashed-ip>.my.local-ip.co`, not the bare IP being dialed; peer trust
comes from the operator-confirmed Ed25519 fingerprint at the approval popup, not
from TLS PKI. State is committed only after approval so an unapproved node never
leaves behind a partial cluster identity.
