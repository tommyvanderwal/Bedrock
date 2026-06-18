# installer/lib/observability.py

Metrics + logs HA: two designated single-binary backends per signal plus shipping agents on every node. The module generates the systemd unit files for VictoriaMetrics/VictoriaLogs and their agents, and converges each node's systemd state to match the cluster snapshot's `obs_backends` map. The reactor on every node calls `reconcile()` on each log fold; mgmt (`mgmt/app.py:join_approve()`, `observability_promote`) calls `seed_backend()` when a node fills a second backend slot, so the snapshot never advertises an empty backend.

The desired state lives in the cluster snapshot under `obs_backends`, set via the `OBS_BACKENDS_SET` log entry:

    {"metrics": [<node_name>, <node_name>],
     "logs":    [<node_name>, <node_name>]}

Every node always runs `bedrock-vmagent` + `bedrock-vlagent` (forwarders). A node named in `obs_backends.metrics` also runs `bedrock-vm`; a node named in `obs_backends.logs` also runs `bedrock-vl`.

## Functions / Classes

### `reconcile(snapshot: dict, self_name: str) -> None`
Converge this node's observability systemd units to match the snapshot.
- **In:** `snapshot` — cluster snapshot dict (reads `obs_backends.metrics`, `obs_backends.logs`, and `nodes[name].{loopback_ip,host}` for backend URLs); `self_name` — this node's name, matched against the backend lists.
- **Out:** `None`. Side effects: writes `/etc/systemd/system/bedrock-vmagent.service`, `bedrock-vlagent.service`, and (when this node is a backend) `bedrock-vm.service` / `bedrock-vl.service`; runs `systemctl daemon-reload` on any unit-file change; enables/starts, restarts, or disables those units via `systemctl`. No-op when both backend lists are empty, or on a repeat call with unchanged inputs.

### `seed_backend(source_host, target_host, ssh_runner, sftp_runner, *, force=False) -> dict`
Copy an existing backend's VM (and, where possible, VL) data dir to a new backend before its daemons start. Synchronous; the caller blocks on it.
- **In:** `source_host` — host with populated data; `target_host` — new backend host; `ssh_runner(host, cmd, timeout=...) -> (output, rc)` and `sftp_runner(host, local, remote)` — injected runners (so paramiko is not imported here); `force` — wipe any existing target VM data first (the `--replace` path).
- **Out:** report dict `{"metrics": <status>, "logs": <status>}` where each status is `"skipped"`, `"ok"`, an error string (`vmbackup rc=...`, `tar-over-ssh rc=...`, `vmrestore rc=...`), or `"skipped (VL has no online snapshot; starts fresh)"`. Side effects (all via `ssh_runner`): runs `vmbackup` on source, tar-over-ssh ships a `/tmp` stage dir to target, `vmrestore` on target into a cleaned `VM_DATA`, removes stage dirs on both hosts; with `force`, stops `bedrock-vm` and wipes `VM_DATA` on the target first.

### Module constants
Absolute paths owned here: binaries `VMAGENT_BIN`, `VLAGENT_BIN`, `VM_BIN`, `VL_BIN`, `VMBACKUP_BIN`, `VMRESTORE_BIN` under `/opt/bedrock/bin`; agent disk queues `VMAGENT_QUEUE` / `VLAGENT_QUEUE` under `/var/lib/bedrock`; backend data dirs `VM_DATA` (`/opt/bedrock/data/vm`) / `VL_DATA` (`/opt/bedrock/data/vl`); `SCRAPE_FILE` (`/opt/bedrock/scrape.yml`); unit names `UNIT_VMAGENT`, `UNIT_VLAGENT`, `UNIT_VM`, `UNIT_VL`.

### Private helpers
- `_write_if_changed(path, content, mode=0o644) -> bool` — atomic tmp+rename write; returns `True` only if the file content actually changed.
- `_run(cmd) -> None` — `subprocess.run(shell=True, check=False, timeout=30)`, output captured and dropped.
- `_systemd_want(unit, want_running, restart_if_running=False) -> None` — drive a unit toward running/stopped via `systemctl enable --now` / `disable --now` / `restart`; idempotent (no-op when state already matches); restarts only when running and `restart_if_running` is set.
- `_backend_url(snapshot, node_name, port) -> str` — resolve a node name to `http://<addr>:<port>`, preferring `nodes[name].loopback_ip` over `host`; empty string if neither is set.
- `_vmagent_unit(metrics_backends, snapshot) -> str` / `_vlagent_unit(logs_backends, snapshot) -> str` — render the agent unit text, one `-remoteWrite.url` per reachable backend (`:8428/api/v1/write` for metrics, `:9428/internal/insert` for logs). vmagent reads `SCRAPE_FILE` via `-promscrape.config`; vlagent listens on TCP 5140 syslog (`After=rsyslog.service`). Both set `-remoteWrite.maxDiskUsagePerURL=8GB`.
- `reconcile_journal_forward() -> bool` — write `/etc/systemd/journald.conf.d/50-bedrock-forward.conf` (`ForwardToSyslog=yes`) and `/etc/rsyslog.d/50-bedrock-vlagent.conf` (TCP forward to `127.0.0.1:5140`); enable rsyslog; restart journald/rsyslog only when drop-ins change. Called from `reconcile()` whenever logs backends are configured.
- `_vm_unit() -> str` / `_vl_unit() -> str` — render the single-binary backend units (VM on `:8428`, 90d retention; VL on `:9428` plus syslog TCP 5141).
- `_data_dir_seeded(data_dir) -> bool` — `True` if the dir contains a `values.bin` or `items.bin` part anywhere beneath it (real TSDB data vs. empty scaffolding).
- `_can_start_vm_backend(snapshot, self_name) -> bool` — the metrics-backend start gate (see How it works).

## How it works

`reconcile()` is the per-node convergence step, called on every log fold, so it is fast and idempotent — running it twice writes nothing the second time.

    reconcile(snapshot, self_name)
      │
      ├─ both backend lists empty? ──► return (no-op)
      │
      ├─ metrics_backends non-empty:
      │     write bedrock-vmagent.service (dual-write to each metrics :8428)
      │     if changed → daemon-reload
      │     ensure running, restart_if_running = changed
      │
      ├─ logs_backends non-empty:
      │     reconcile_journal_forward()  (journald + rsyslog drop-ins)
      │     same for bedrock-vlagent.service (dual-write to each logs :9428)
      │
      ├─ want_vm = self_name in metrics_backends
      │     if want_vm:
      │        write bedrock-vm.service; daemon-reload on change
      │        if _can_start_vm_backend(...): ensure running
      │        else: leave it (do NOT start, do NOT disable)
      │     else: ensure stopped/disabled
      │
      └─ want_vl = self_name in logs_backends
            if want_vl: write bedrock-vl.service; ensure running
            else: ensure stopped/disabled

Agent units always carry the full set of reachable backend URLs, so each agent dual-writes to both backends. The persistent disk queue at `-remoteWrite.tmpDataPath` (`maxDiskUsagePerURL=8GB`) survives reboots and replays on backend recovery — that disk queue is the only convergence mechanism between the two backends of a signal.

The metrics-backend start gate (`_can_start_vm_backend`) decides whether a node named as a metrics backend actually starts `bedrock-vm`:

    self_name not in metrics_backends         → False (and reconcile disables it)
    len(metrics_backends) <= 1 (solo backend) → True  (start fresh, be the source)
    multiple backends, VM_DATA seeded          → True
    multiple backends, VM_DATA empty           → False (wait for mgmt's seed)

The empty-but-named case is deliberately left untouched (not started, not disabled): a node just added to the snapshot as the second metrics backend keeps `bedrock-vm` stopped while agents buffer their writes for it in their disk queues. Mgmt seeds `VM_DATA`, then runs `systemctl start bedrock-vm` directly; the next reactor cycle sees it running and the buffered agent writes drain into it — no gap. VL has no seed gate: `bedrock-vl` starts as soon as the node is named a logs backend, and vlagent dual-write keeps both VL backends identical from that point on; historical data starts fresh on the new one.

`seed_backend()` runs the metrics seed and reports the logs seed as skipped:

    metrics:  force or target VM_DATA empty?
                ├─ force & target not empty → stop bedrock-vm + rm -rf VM_DATA
                └─ _vmbackup_seed(source, target, VM_DATA, 8428):
                     1. ssh source: vmbackup -snapshot.createURL=.../snapshot/create
                                    -dst=fs:///tmp/vmbackup-seed-<ts>
                     2. ssh source: tar -czf - <stage> | ssh root@target 'tar -xzf -'
                     3. ssh target: rm -rf VM_DATA && mkdir -p && vmrestore -src=fs://...
                     4. rm -rf stage dirs on both hosts (always)
                   → "ok" or "vmbackup rc=N"/"tar-over-ssh rc=N"/"vmrestore rc=N"
    logs:     target VL_DATA empty?
                └─ "skipped (VL has no online snapshot; starts fresh)"

The seed is idempotent on a non-empty target VM data dir (it skips), with the known edge that a half-finished seed also looks non-empty. vmbackup writes real file copies into the `fs://` stage, so the symlinks inside VM's `snapshots/` tree never enter the tar archive. Any non-zero `rc` short-circuits with an error string, cleaning up the stage on tar/restore failure.

## Why

The agents see the snapshot name the new backend and start buffering for it before its daemon accepts writes; mgmt seeds the data dir, then starts the daemon. This ordering avoids a window where agents dual-write to an empty backend whose data dir is then overwritten by the seed. Backend URLs prefer the loopback `/32` (cluster mesh, multi-path failover via bedrock-net) over the mgmt host, so a NIC change never strands the agent on a dead address.
