# `observability.py`

**Module purpose.** Run-tier-converge for the VictoriaMetrics +
VictoriaLogs stack. Called from
`mgmt/orchestrator.rqlite_subscriber._apply_revision` on every
revision tick so a change to `obs_backends` (the rqlite-stored
list of "which nodes host the metric/log storage") propagates
to local systemd units.

Every node ALWAYS runs `vmagent` (scrapes node_exporter, vm_exporter
locally, ships to backends) + `vlagent` (collects syslog locally,
ships to backends). Backends run only on nodes named in
`obs_backends.metrics` / `obs_backends.logs`.

## Functions

- `bootstrap_master()` — install + start vmagent + vlagent +
  victoria-metrics + victoria-logs (the initial master is both
  agent AND backend). Called by `mgmt_install.install_full`.
- `bootstrap_agent()` — install + start vmagent + vlagent only.
  Called by `agent_install.install`.
- `reconcile(snapshot, self_name)` — idempotent converge for
  whichever node we're on:
  - Always ensure vmagent + vlagent units are enabled +
    running; re-render their config from `obs_backends.metrics`
    / `obs_backends.logs` lists on each call.
  - If `self_name` is in `obs_backends.metrics`: ensure
    victoria-metrics unit is enabled + running. Else: disable +
    stop.
  - Same for victoria-logs / `obs_backends.logs`.
  - Re-render scrape.yml with all current node loopbacks as
    targets.
- `obs_seed_from_peer(peer_loopback)` — at promote-to-backend
  time, `vmbackup`-style replicate from an existing backend so
  the new backend starts with a populated TSDB rather than
  empty.

For the design rationale (HA shape, vmbackup-seed, why dual
backends instead of cluster-aware VictoriaMetrics) see
`project_victorialogs_ha.md` in the memory store.
