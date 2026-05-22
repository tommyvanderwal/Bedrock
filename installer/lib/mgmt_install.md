# `mgmt_install.py`

**Module purpose.** The big `install_full()` function run on
the FIRST node by `bedrock init` — seeds a fresh cluster from
nothing. Steps, in order:

1. **Hardware inventory** → write `state.json` with this node's
   cluster_uuid (freshly generated), cluster_name, node_name,
   mgmt_ip, loopback_ip (`.1` from the CGNAT prefix derived from
   cluster_uuid).
2. **Tier-N=1 storage setup** via `tier_storage.setup_n1()`.
3. **Cluster HMAC key** via `daemon_setup.write_cluster_key()`
   — generates 32 random bytes if `/etc/bedrock/cluster.key`
   doesn't exist. Joiners pull this through the join handshake.
4. **rqlite bootstrap** — writes a minimal cluster.json with
   just this node, renders `/etc/bedrock/rqlited.env`, starts
   the per-node `bedrock-rqlited.service`, waits for Raft to
   elect (single-node "Leader"), applies
   `bedrock_schema.sql`, seeds initial rows: `cluster_info`,
   `nodes` (this node), `operator_set` (default
   `root`/`admin`), `obs_backends` (this node hosts the only
   metric/log store), `set_mgmt_master(self)`.
5. **bedrock-net** daemon start + sysctls for routing.
6. **SeaweedFS** install + master + volume start + filer + s3
   start. At N=1 the master subset is just `self`.
7. **ISO library FUSE mount** at `/mnt/bedrock` (filer namespace
   surfaces ISOs at `/mnt/bedrock/iso/`).
8. **Dashboard** via `dashboard_install.install_dashboard()`
   (mgmt service unit + Svelte UI).
9. **Observability** via `observability.bootstrap_master()`
   (VictoriaMetrics + VictoriaLogs single-binary on this node).
10. **Optional witness**: if `--witness HOST` was given, register
    it via `bs.witness_register()`.

The function is BIG and largely sequential — each step's failure
mode is logged but doesn't unwind; the operator can re-run
`bedrock init` to retry the failed step (most are idempotent).

## Functions

- `install_full(*, cluster_name, witness_host=None, repo)` —
  the entry point. Read top of module for the full sequence.
  Most of the function body is wrapped in nested try/except so a
  failed step prints a WARN and the next step can still run.

  Key invariants:
  - Order matters: tier setup before cluster.key (so DRBD meta
    LV can be allocated later), cluster.key before rqlite (so
    witness HMAC works), rqlite before bedrock-net (so the
    election layer has rqlite to write set_mgmt_master to),
    SeaweedFS after rqlite (master subset reads cluster.json),
    observability last (depends on rqlite for obs_backends).
  - Idempotent: every step's underlying helper (`setup_n1`,
    `write_cluster_key`, `render_env_file`, etc.) handles the
    already-done case gracefully.

- `_pull_binary_if_missing(name, repo)` — helper used to pull
  things like `weed`, `rqlited`, victoria-metrics etc. from
  `BEDROCK_REPO/binaries/<name>` if `/opt/bedrock/bin/<name>` is
  missing.

The module also imports + re-uses helpers from `state.py`,
`hardware.py`, `cluster_addr.py`, `peer_auth.py`, `operator_auth.py`,
`tier_storage.py`, `daemon_setup.py`, `rqlite_setup.py`,
`bedrock_state.py`, `view_builder.py`, `seaweedfs.py`,
`dashboard_install.py`, `observability.py`.
