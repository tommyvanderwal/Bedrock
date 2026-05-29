# installer/lib/dashboard_install.py

Install-time helper that stages the management web stack onto a node so the
Svelte UI + FastAPI mgmt API is reachable at `https://<node>:8443` (operator
browser, TLS via the local-ip.co wildcard cert) and `http://127.0.0.1:8001`
(per-node CLI). It fetches the `mgmt` source tarball into `/opt/bedrock/mgmt`
and brings up the unified `bedrock-d` daemon, which imports that source and
serves the dashboard. Called from the install/join paths; every node runs it so
any node can serve the dashboard. The master also hosts the metrics + logs stack
(VictoriaMetrics, VictoriaLogs).

## Functions / Classes

### `install_dashboard(repo: str, with_metrics: bool = False) -> None`
Stage `/opt/bedrock/mgmt` from the repo's `mgmt.tar.gz` and enable + start
`bedrock-d`.
- **In:** `repo` — base URL the `mgmt.tar.gz` artifact hangs off
  (`{repo}/mgmt.tar.gz`). `with_metrics` — flag marking a master node that also
  runs the metrics/logs stack; accepted but the function's branches do not vary
  on it.
- **Out:** returns `None`. Side effects: creates `/opt/bedrock/mgmt`; downloads
  `/tmp/mgmt.tar.gz` via `curl` and, on success, extracts it into
  `/opt/bedrock/mgmt` (`--strip-components=1`); runs `systemctl daemon-reload`,
  resets `bedrock-d` failure state, enables + starts `bedrock-d`, and disables
  any `bedrock-mgmt.service`. All via shell subprocesses.

### `_run(cmd: str, check: bool = False) -> int` (private)
Run a shell command via `subprocess.run(..., shell=True)` and return its exit
code. `check` is accepted but unused — no exception is raised on failure.

## How it works

Module constants pin the layout: `BEDROCK_BASE=/opt/bedrock`,
`MGMT=/opt/bedrock/mgmt`, `SYSTEMD_DIR=/etc/systemd/system` (`SYSTEMD_DIR` is
defined but unused).

`install_dashboard` runs in order:

```
1. mkdir -p /opt/bedrock/mgmt
2. curl -fsSL {repo}/mgmt.tar.gz -o /tmp/mgmt.tar.gz
      └─ on exit 0 → tar xzf /tmp/mgmt.tar.gz -C /opt/bedrock/mgmt
                        --strip-components=1
3. systemctl daemon-reload
4. systemctl reset-failed bedrock-d.service       (errors ignored)
5. systemctl enable --now bedrock-d
6. systemctl disable --now bedrock-mgmt.service    (errors ignored)
```

The extract is guarded on the `curl` exit code: only a successful download
(exit 0) triggers `tar`, so a fetch failure leaves the existing
`/opt/bedrock/mgmt` untouched rather than unpacking a partial file. No step
raises on a non-zero exit — `_run` returns the code and the caller ignores it —
so the helper is best-effort end to end.

The dashboard is served by the `bedrock-d` process, which imports the extracted
`mgmt/` source; this helper only stages that source and starts the daemon, and
writes no systemd unit of its own (`bedrock-d.service` ships with the base
install). `reset-failed` precedes the enable+start because `bedrock-d` may sit
in a rate-limited failure state from an earlier boot attempt; clearing it lets
the start take effect. The trailing `disable --now bedrock-mgmt.service`
(errors suppressed) ensures no separate `bedrock-mgmt` unit can bind the mgmt
ports ahead of `bedrock-d`.

## Why
The mgmt source ships as a tarball extracted in place (rather than baked into
the daemon binary) so the dashboard/API code is updatable independently of the
`bedrock-d` install, and the identical extract+start path runs on master and
followers alike.
