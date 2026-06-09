# installer/lib/exporters.py

Lays down the per-node telemetry exporters and stages the observability binaries on the local disk. It fetches `node_exporter` and `vm_exporter.py`, writes their systemd units, and starts them; it also pulls the full VictoriaMetrics/VictoriaLogs binary set so any node can be promoted to a metrics/logs backend without a missing-binary surprise. Called from the install path during node setup. It owns only the two exporter units; the systemd units for the observability binaries it merely stages are owned by `installer/lib/observability.py`.

## Functions / Classes

### `install(repo: str)`
Fetch the exporter and observability binaries, install the two exporter systemd units, and enable+start them.
- **In:** `repo` → base URL of the install repo; binaries are fetched from `{repo}/binaries/<name>` via `curl -fsSL`.
- **Out:** returns nothing. Side effects:
  - Creates `/opt/bedrock/bin/`.
  - Downloads `/opt/bedrock/bin/node_exporter` (only if absent) and `/opt/bedrock/bin/vm_exporter.py` (always re-fetched), each `chmod 0755`.
  - Downloads each observability binary into `/opt/bedrock/bin/` (`vmagent`, `vlagent`, `vmbackup`, `vmrestore`, `victoria-metrics`, `victoria-logs`), `chmod 0755`.
  - Writes `/etc/systemd/system/node-exporter.service` (runs `node_exporter --web.listen-address=:9100`) and `/etc/systemd/system/vm-exporter.service` (runs `python3 /opt/bedrock/bin/vm_exporter.py`, ordered After/Wants `libvirtd.service`); both `Restart=always`, `RestartSec=3`, `WantedBy=multi-user.target`.
  - Runs `systemctl daemon-reload` then `systemctl enable --now node-exporter vm-exporter` via subprocess.

### `_run(cmd, check=True)` (private)
Run a shell command via `subprocess.run(..., shell=True, capture_output=True, text=True)`; raises `RuntimeError` with stderr if `check` is set and the exit code is nonzero.

## How it works

Three fetch policies, all keyed on what is already on disk:

```
node_exporter      → fetch only if file is absent          (chmod 755 always)
vm_exporter.py     → fetch every call (overwrite)          (chmod 755 always)
OBS_BINS (each)    → skip if present AND size > 1,000,000 B (else fetch)
```

The 1 MB size gate on the observability binaries treats a too-small file as not-really-there, so a truncated or stub download is re-fetched on the next run rather than silently kept. Each observability fetch is wrapped in a `try`/`except RuntimeError`: if a binary is missing from the repo, it prints a `WARN` and continues — a backend binary being unavailable does not abort exporter installation.

Sequence within `install`:

```
mkdir /opt/bedrock/bin
fetch node_exporter (if absent) → chmod
fetch vm_exporter.py            → chmod
for b in OBS_BINS: fetch-if-needed (warn-and-continue on failure)
write node-exporter.service
write vm-exporter.service
systemctl daemon-reload
systemctl enable --now node-exporter vm-exporter
```

The required exporter fetches (`node_exporter`, `vm_exporter.py`) and the systemctl calls go through `_run` with `check=True`, so a failure there raises `RuntimeError` and aborts. The unit files are written unconditionally before the reload, so they always reflect the current template text.

## Why

Every node carries the full observability binary set because any node may be promoted into a metrics/logs backend slot; staging the bytes everywhere is cheaper than scrambling to download them at promotion time. This module stages and starts only the two exporters and deliberately does not create units for the staged observability binaries — `observability.py` reconciles those — so the two modules don't both claim the same units.
