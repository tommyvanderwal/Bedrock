# installer/lib/mgmt_install.py

Brings up the full management stack on the first node of a cluster — the work behind `bedrock init`. It is the entry point that turns a bare host into a one-node `mgmt+compute` master: cluster identity, the rqlite cluster-state store, storage tiers, the dashboard, metrics/logs, the unified `bedrock-d` daemon, and SeaweedFS. The public `install_full()` is the only caller-facing function; it either drives the `cluster_init` saga (default) or runs an in-line procedural bring-up when opted in via `BEDROCK_INIT_SAGA=0`. The module's path constants and small helpers are also reused by that saga (`bedrock_d.install.cluster_init`).

## Module constants

Path roots every caller (this module and the saga) builds the install layout from:

```
BEDROCK_BASE = /opt/bedrock
BINARIES     = /opt/bedrock/bin     (victoria-metrics, victoria-logs)
DATA         = /opt/bedrock/data    (data/vm, data/vl)
MGMT         = /opt/bedrock/mgmt
```

## Functions / Classes

### `install_full(cluster_name, repo)`
Stands up the management stack on this node and records it as the cluster master.
- **In:** `cluster_name` — human label for the cluster; `repo` — base URL of the artifact repo used for downloads (VictoriaMetrics, VictoriaLogs, exporters, virtio-win ISO).
- **Out:** On the saga path, returns the result of `bedrock_d.install.cluster_init.run_cluster_init(cluster_name, repo)`. On the procedural path, returns nothing meaningful; its effect is side effects. Procedural side effects: creates `/opt/bedrock/{bin,data/vm,data/vl,iso,mgmt}` and `scrape.yml`; writes `/etc/bedrock/state.json` (via `state.save`) and `/etc/bedrock/cluster.json`; writes + starts systemd units `bedrock-vm` and `bedrock-vl`; starts the dashboard, `bedrock-rqlited.service`, and `bedrock-d.service`; seeds rqlite (schema + `cluster_info`, master `node_register`, master `node_loopback`, `root` operator, `obs_backends`, `mgmt_master`) and mirrors tier state in; sets up N=1 storage tiers and SeaweedFS (master/volume/filer/s3 + ISO FUSE mount); sets `rp_filter=2` and `ip_forward=1`. Raises (or `SystemExit(1)`) on hard failure — see **How it works**.

### `run(cmd, check=True) -> str`
Thin shell wrapper.
- **In:** `cmd` — shell string; `check` — raise on non-zero exit.
- **Out:** stripped stdout. Side effect: the subprocess. Raises `RuntimeError` carrying stderr when `check` and the command fails.

### Helpers (also reused by the `cluster_init` saga)
- `_pick_mgmt_ip(hw) -> str` — picks this node's management IP from the hardware dict: first an UP `br0` with an IP, else any UP non-`10.` IP (LAN), else any UP IP, else `""`. No side effects.
- `_download(url, dest)` — `curl -fsSL` a file to `dest`, printing the basename. Side effect: the subprocess.
- `_write_systemd(name, content)` — writes `/etc/systemd/system/<name>.service` then runs `systemctl daemon-reload`.

## How it works

`install_full` branches on the `BEDROCK_INIT_SAGA` environment variable:

```
            BEDROCK_INIT_SAGA != "0"  (default)
install_full ──────────────────────────────► run_cluster_init(cluster_name, repo)
            │                                  (ordered idempotent saga; progress at
            │                                   /var/lib/bedrock/init-progress.json; crash-resumable)
            │
            │  BEDROCK_INIT_SAGA == "0"
            └──────────────────────────► in-line procedural bring-up (below)
```

On the saga path, `sys.path` is augmented with the repo root (two parents up from this file) and `/usr/local/lib/bedrock` so the `bedrock_d` package imports both in-repo and on a deployed node, then `run_cluster_init` is called and its return propagated. The saga reuses this module's path constants and the `_download` / `_write_systemd` / `_pick_mgmt_ip` helpers, so they remain load-bearing.

The procedural path runs as one ordered sequence; the ordering and guards are load-bearing:

1. **Directories + binaries.** Creates `/opt/bedrock/{bin,data/vm,data/vl,mgmt}`. Downloads `victoria-metrics` and `victoria-logs` into `bin/` only when absent, chmod 0755.
2. **ISO library staging.** Creates `/opt/bedrock/iso` with a `README.md`. Pre-fetches `virtio-win.iso` only when absent — tries `<repo>/virtio-win.iso` first, then the upstream fedorapeople URL, writing to a `.tmp` and renaming on success; a failure only WARNs (Windows installs then need the driver ISO attached by hand).
3. **Dashboard files.** Imports `dashboard_install`; the dashboard install runs on every node so the dashboard is reachable from any node.
4. **Scrape config.** Resolves `mgmt_ip` via `_pick_mgmt_ip` and writes `/opt/bedrock/scrape.yml` seeding only this node's `:9100` (node) and `:9177` (libvirt) targets, labelled with `cluster_name`.
5. **Exporters.** `exporters.install(repo)`.
6. **Metrics/logs units.** Writes and `enable --now`s `bedrock-vm` (VictoriaMetrics on `:8428`, 90d retention, scraping `scrape.yml`) and `bedrock-vl` (VictoriaLogs on `:9428`, syslog tcp `:5140`), then `dashboard_install.install_dashboard(repo, with_metrics=True)`.
7. **Local identity state.** Loads `state`; sets `cluster_name`, `cluster_uuid` (generated if absent), `role=mgmt+compute`, `node_id=0`, `node_name` (hostname), `mgmt_ip`, `mgmt_url=https://<mgmt_ip>:8443`, and `loopback_ip` from `cluster_addr.node_loopback_ip(cluster_uuid, 1)`; saves it. Writes an initial `/etc/bedrock/cluster.json` registering this one node (host, role, loopback, cockpit URL).
8. **Storage tiers (N=1).** `tier_storage.setup_n1(write_rqlite=False)` — local thin-LV only, no rqlite write yet (rqlite isn't up); failure WARNs and points at `bedrock storage init`.
9. **Cluster HMAC key.** `daemon_setup.write_cluster_key()` so every joiner's witness heartbeats verify against the same secret; failure WARNs.
10. **rqlite bootstrap + seed** — the load-bearing block. It resolves a chicken-and-egg: `rqlite_setup.render_env_file()` derives `node_id` from `cluster.json`, but rqlite isn't running to regenerate it, so a minimal bootstrap `cluster.json` (just the master entry, empty collections) is written first. Then:

```
write bootstrap cluster.json (master only)
        │
render_env_file()  ── fail ─► WARN + raise
        │
systemctl reset-failed + restart bedrock-rqlited.service
        │
poll https://127.0.0.1:4001/status (mTLS via node cert/key/ca, 60 × 0.5s)
   wait for store.raft.state == "Leader"   ◄── not just HTTP-up; /db/execute 503s
        │                                       ("leader not found") until Raft elects
   not Leader in window ─► RuntimeError(last raft state)
        │
RqliteClient(): apply bedrock_schema.sql, then
   cluster_init → node_register(master, +ssh pubkey, +bedrock_pubkey)
   → node_loopback → operator_set(root/admin) → obs_backends_set(this node)
   → set_mgmt_master(this node)
        │
tier_storage.mirror_tier_state_to_rqlite()   (own short-lived client; failure here raises)
```

   The initial operator is `root` with password `admin` (salted+hashed via `operator_auth.hash_password`) so the dashboard is immediately usable. The master's SSH pubkey (`/root/.ssh/id_ed25519.pub`, empty string if unreadable) and Bedrock Ed25519 pubkey (`peer_auth.pubkey_hex()`) are recorded with `node_register`. Any exception in this whole block prints an ERROR plus a "half-initialised; remediate" message and exits with `SystemExit(1)` — the seed is all-or-nothing because a partially seeded cluster is silently broken.
11. **bedrock-d daemon.** `daemon-reload`, `reset-failed bedrock-d.service`, `enable --now bedrock-d.service`, and sets `net.ipv4.conf.{all,default}.rp_filter=2` and `net.ipv4.ip_forward=1` via `sysctl`. Failure WARNs. This one process owns mesh discovery, election, witness IO, the rqlite subscriber, the no-quorum responder, the boot orchestrator, the dashboard, and cert refresh.
12. **SeaweedFS.** `ensure_install`, write env/master/filer/s3 configs, `promote_to_master_volume_host`, then `promote_to_filer_host` in-line (so init returns with filer+s3 up rather than waiting for the orchestrator's first converge tick). Polls `127.0.0.1:8333` (30 × 0.5s) until the S3 gateway binds, then `ensure_iso_library_mount` and `seed_iso_library(/opt/bedrock/iso)`. The whole block WARNs on failure.

The polling guards (rqlite `Leader`, S3 bind) exist because a 200 on `/status` or a started unit does not mean the service is usably ready; seeding or PUTting too early fails silently or with ECONNREFUSED.

## Why

The default saga path makes `bedrock init` crash-resumable from `init-progress.json`; the `BEDROCK_INIT_SAGA=0` body is the procedural opt-out. The loopback `/32` is derived from `cluster_uuid` inside RFC 6598 (100.64.0.0/10) so it can't collide with operator LANs, and the endpoint advertised to joiners is HTTPS `:8443` because the mgmt HTTP port (`:8001`) binds loopback-only. The rqlite seed waits for Raft `Leader` (not HTTP-up) so the schema and initial rows actually commit instead of being lost against a 503 store.
