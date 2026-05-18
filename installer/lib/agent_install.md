# `agent_install.py`

**Module purpose.** The big `install()` function run on a JOINING
node by `bedrock join <master-host>`. Walks the joiner from a
post-bootstrap fresh state to a fully-attached cluster member.

Steps:

1. **Discovery** — talk to `bedrock-redirect` on the master's
   `:80/cluster-info` (or `--witness` flag) to get cluster_uuid,
   cluster_name, mgmt_url.
2. **Register intent** — `POST /api/join/request` with this
   node's hardware inventory + Ed25519 pubkey. Master writes a
   `join_requests` row with `state=pending`.
3. **Wait for approval** — poll `GET /api/join/status?id=<rid>`
   until `state=approved` (operator clicks Approve on dashboard
   OR runs `bedrock node approve <id>` on the master). On
   approval, master returns the allocated loopback_ip + the
   cluster_key_hex.
4. **Write local state** — `/etc/bedrock/state.json` with
   cluster_uuid + node_name + loopback_ip; `/etc/bedrock/cluster.key`
   from the master's hex; `/etc/bedrock/installer.env` repo URL.
   Pre-scan the master's SSH host key into `~/.ssh/known_hosts`
   so the joiner can SSH out without prompts.
5. **Tier-N=1 storage setup** via `tier_storage.setup_n1()`.
6. **bedrock-net** start.
7. **rqlite join** — render env with `BEDROCK_RQLITED_JOIN_FLAG=
   "-join <master-loopback>:4002,..."`, start the unit, wait for
   Raft to add this node as Voter.
8. **SeaweedFS** install + start. On this node:
   - `weed-master` only runs if this node is in the
     odd-subset of nodes by loopback-octet (see
     `seaweedfs.py`).
   - `weed-volume` always runs.
   - `weed-filer` / `weed-s3` only on the master, gated by
     `cluster_arbiter.converge()`.
9. **Dashboard** via `dashboard_install.install_dashboard()`
   so this node can become mgmt master on failover.
10. **Observability agents** via `observability.bootstrap_agent()`
    (vmagent + vlagent, shipping to the obs_backends list).

## Functions

- `install(repo, *, witness, name=None, yes=False)` — entry point
  invoked by `bedrock join`. Walks the steps above. Top-level
  try/except so retries are possible after partial install.

- `_register(witness_url, node_state, pubkey_hex, name) -> dict` —
  POST `/api/join/request`, returns the master's response
  including `request_id`. Retries through transient 503s
  (master mgmt may be restarting).

- `_poll_status(witness_url, request_id, deadline_s=900)` —
  GET `/api/join/status?id=<rid>` every 2 s. Retries through
  `URLError`/`TimeoutError`/`ConnectionError`/`OSError` (so a
  master mgmt restart mid-poll doesn't abort the join). Returns
  the final approval dict on `state=approved`.

- `_request_join(witness_url, request_id, pubkey_hex) -> dict` —
  legacy alias for the auto-approve fast path; kept for the
  `--yes` flag and CLI scripts.

- `_setup_rqlited_join(state, master_loopback_ip)` — render env,
  enable + start the unit, poll `/status` until Raft adds this
  node as Voter.

- `_install_dashboard_local(repo, state)` — wrapper around
  `dashboard_install.install_dashboard(..., with_metrics=False)`
  (metrics already installed by observability.bootstrap_agent).

- `_pull_master_ssh_host_key(master_host)` — scan + add to
  `~/.ssh/known_hosts` so subsequent `tier_storage._peer-promote`
  SSH callouts work without interactive y/n.
