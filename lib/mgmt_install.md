# installer/lib/mgmt_install.py

Management-node install entry point — the work behind `bedrock init`. Turns a
bare host into a one-node `mgmt+compute` master by running the `cluster_init`
saga. The module also holds the path constants and small helpers that the
saga's steps reuse, so the on-disk layout and the download/systemd primitives
have one definition.

## Module constants

```
BEDROCK_BASE = /opt/bedrock
BINARIES     = /opt/bedrock/bin      (victoria-metrics, victoria-logs, exporters)
DATA         = /opt/bedrock/data     (data/vm, data/vl)
MGMT         = /opt/bedrock/mgmt
```

## Functions

### `install_full(cluster_name, repo)`
Entry point for `bedrock init`; runs the cluster_init saga.
- **In:** `cluster_name` — human label for the cluster; `repo` — base URL of the
  artifact repo the saga downloads binaries from.
- **Out:** returns `bedrock_d.install.cluster_init.run_cluster_init(cluster_name,
  repo)`. The saga drives every side effect (identity, rqlite, storage tiers,
  dashboard, observability, bedrock-d, SeaweedFS) as ordered, crash-resumable
  steps with progress at `/var/lib/bedrock/init-progress.json`.

### `run(cmd, check=True) -> str`
Run a shell command, capturing output.
- **In:** `cmd` shell string; `check` — raise on non-zero exit.
- **Out:** stripped stdout. Raises `RuntimeError` carrying stderr when `check`
  and the command fails.

### `_pick_mgmt_ip(hw) -> str`
Pick this node's management IP from the hardware dict.
- **In:** `hw` with a `nics` list (`name`/`state`/`ip`).
- **Out:** first UP `br0` IP, else first UP non-`10.` IP (LAN), else any UP IP,
  else `""`. No side effects.

### `_download(url, dest)` · `_write_systemd(name, content)`
- `_download`: `curl -fsSL` the URL to `dest` (a Path), printing the basename.
- `_write_systemd`: write `/etc/systemd/system/<name>.service`, then
  `systemctl daemon-reload`.

## How it works

`install_full` puts both the source-tree root (two parents up) and
`/usr/local/lib/bedrock` on `sys.path` so the saga's `bedrock_d.*` imports
resolve whether running from a checkout or a deployed node, then calls and
returns `run_cluster_init`.

```
bedrock init ──> install_full ──> run_cluster_init  (ordered, resumable saga)
                      │
                      └── BEDROCK_BASE/BINARIES/DATA/MGMT + run / _download /
                          _write_systemd / _pick_mgmt_ip
                                ▲ imported and reused by cluster_init's steps
```

`bedrock_d/install/cluster_init.py` imports this module (`as _m`) for those
constants and helpers, so they stay load-bearing.

## Why

The loopback `/32` the saga assigns is derived from `cluster_uuid` inside RFC
6598 (100.64.0.0/10), so it can't collide with operator LANs; the endpoint
advertised to joiners is HTTPS `:8443` because the mgmt HTTP port (`:8001`)
binds loopback-only.
