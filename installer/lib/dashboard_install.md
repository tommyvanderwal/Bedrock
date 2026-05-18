# `dashboard_install.py`

**Module purpose.** Install the `bedrock-mgmt.service` systemd unit
+ the FastAPI app + the Svelte UI on a node. Called from both
`mgmt_install.install_full` (initial master) and
`agent_install.install` (joiner) so every node has the dashboard
+ orchestrator and can take over the master role on failover
without runtime pip installs.

## Functions

- `install_dashboard(repo, *, with_metrics=True)` — entry point.
  Steps:
  1. Untar `mgmt.tar.gz` (staged by install.sh from the ISO
     payload) into `/opt/bedrock/mgmt/`. Contains `app.py`,
     `orchestrator.py`, `backup.py`, `tasks.py`, `cron.py`,
     `victoria.py`, plus the Svelte UI under `ui/build/` and
     the noVNC bundle under `novnc/`.
  2. Write `/etc/systemd/system/bedrock-mgmt.service` —
     `ExecStart=/usr/bin/python3 /opt/bedrock/mgmt/app.py`,
     run as root (needs CAP_NET_BIND_SERVICE for :443, virsh
     for VM ops, drbdadm for cluster_arbiter, etc.).
  3. `systemctl daemon-reload && systemctl enable --now
     bedrock-mgmt.service`. (`agent_install` passes
     `--no-block` via Popen rather than blocking the join
     handshake; mgmt's startup can take a few seconds.)
  4. If `with_metrics=True`: install victoria-metrics +
     victoria-logs + node_exporter + vm_exporter (only on the
     initial master path; joiners get these via
     `observability.bootstrap_agent` separately).
- `_extract_mgmt_tarball(repo)` — `tar xzf` from local payload.
- `_write_unit_file()` — render the systemd unit. Includes
  `LimitNOFILE`, journald std{out,err}, dependency on
  `network-online.target` and `bedrock-rqlited.service`.

## Lifecycle

Every node ends up with `bedrock-mgmt.service` enabled. On a
follower this is mostly idle — the orchestrator subscribes to
rqlite, projects cluster.json, and pages through reactor
events. `_is_leader()` gates the master-only tasks
(`backup_scheduler`).

On failover, the elected new master's mgmt service is already
running; nothing needs to start. The role flip happens via
`state.json["role"]` → `cluster_arbiter.converge()` →
`promote_to_arbiter_host` → `weed-filer/s3 + arbiter rqlite
+ .254 VIP`.
