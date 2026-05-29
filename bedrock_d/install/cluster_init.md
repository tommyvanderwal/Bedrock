# bedrock_d/install/cluster_init.py

The `cluster_init` saga: the `bedrock init` flow expressed as an ordered list
of idempotent, crash-resumable steps that bring up the first node of a cluster
from bare install to a running mgmt+compute node. It owns first-node identity
allocation, the cluster TLS CA, the rqlite bootstrap, observability + SeaweedFS
bring-up, and starting `bedrock-d`. Because it is the code that *brings up*
rqlite, it persists its own progress to a JSON file on disk rather than rqlite.
Entry point `run_cluster_init(...)` is the thing `bedrock init` invokes; the
saga steps delegate their bodies to helpers under `installer/lib/*` and
`bedrock_d/state`.

## Functions / Classes

### `run_cluster_init(*, cluster_name: Optional[str] = None, repo: str) -> None`
Entry point for `bedrock init`. Builds the saga `ctx`, opens the file-backed
saga store, then submits a fresh `cluster_init` operation or resumes/retries an
existing one for this node, and runs it to completion.
- **In:** `cluster_name` — display tag; defaults to `bedrock-<hostname>` when
  omitted (the cluster's real identity is the `cluster_uuid` allocated in
  `allocate_identity`). `repo` — install-repo URL used by the binary/exporter
  download steps.
- **Out:** returns `None`. Side effects: creates `/var/lib/bedrock/`; opens
  `FileSagaBackend` at `/var/lib/bedrock/init-progress.json`; submits or
  resumes a `cluster_init` op and runs every step (each step's own side effects
  apply — see steps below). Reads `SUDO_USER`/`USER` for the `requested_by`
  audit field. Raises `RuntimeError` if the saga does not reach `COMPLETED`,
  naming the failed step and the underlying error.

### `class ClusterInit` — `@saga("cluster_init")`
First-node bootstrap saga. Holds the ordered `@step` methods. Each step takes a
`ctx` dict and returns `None`; mutations to `ctx` flow to later steps in the
same run. `ctx` inputs: `cluster_name`, `repo`. `ctx` outputs (set as steps
run): `cluster_uuid`, `node_name`, `loopback_ip`, `mgmt_ip`.

The steps, in execution order, with their side effects:

| # | step | what it does |
|---|------|--------------|
| 1 | `prepare_dirs` | `mkdir -p` the `/opt/bedrock` + `/var/lib/bedrock` tree (binaries, `data/vm`, `data/vl`, mgmt, `iso`). |
| 2 | `allocate_identity` | Loads/saves `state.json`: sets `cluster_name`, allocates `cluster_uuid` (kept if already present), `role=mgmt+compute`, `node_id=0`, `node_name`, `mgmt_ip` + `mgmt_url` (`https://<ip>:8443`), and `loopback_ip` derived from `cluster_uuid` + node index 1. Writes the four outputs into `ctx`. |
| 3 | `write_cluster_key` | `daemon_setup.write_cluster_key()` → `/etc/bedrock/cluster.key` (32-byte AEAD key); respects an existing file. |
| 4 | `write_bootstrap_cluster_json` | Writes `/etc/bedrock/cluster.json` with this node as the sole member, `mgmt_master` = this node, empty collection maps, `log_index=0`. Written verbatim every run. |
| 5 | `install_obs_binaries` | Downloads `victoria-metrics` + `victoria-logs` from `<repo>/binaries/<name>` into the binaries dir (chmod 0755); skips a binary already present. |
| 6 | `install_exporters` | `exporters.install(repo)` — node_exporter + vm_exporter for this node. |
| 7 | `write_obs_services` | Writes `scrape.yml` (node:9100, libvirt:9177 targets, labelled with the cluster name), the `bedrock-vm` (VictoriaMetrics :8428) and `bedrock-vl` (VictoriaLogs :9428 + syslog :5140) systemd units, and installs the dashboard (`with_metrics=True`). |
| 8 | `start_obs_services` | `systemctl enable --now bedrock-vm.service bedrock-vl.service`. |
| 9 | `provision_storage_n1` | `tier_storage.setup_n1(write_rqlite=False)` — LVM thinpool + local tier LVs; no rqlite write (rqlite isn't up). |
| 10 | `bootstrap_cluster_ca` | Generates the cluster TLS CA + this master's per-node cert + the arbiter cert (see "How it works"). |
| 11 | `render_rqlited_env` | `rqlite_setup.render_env_file()` → `/etc/bedrock/rqlited.env` from `cluster.json` + `state.json`. |
| 12 | `start_rqlited` | `reset-failed` + `enable` + `restart` `bedrock-rqlited.service`, then polls `https://127.0.0.1:4001/status` (mTLS) until `store.raft.state == "Leader"`. |
| 13 | `apply_schema` | `state.apply_schema()` against rqlite (every `CREATE` is `IF NOT EXISTS`). |
| 14 | `seed_cluster_state` | Inserts `cluster_info`, this node + its loopback, the `root` operator (password `admin`), `obs_backends` (this node for metrics + logs), and `mgmt_master` = this node. All `INSERT OR REPLACE`. |
| 15 | `mirror_tier_state` | `tier_storage.mirror_tier_state_to_rqlite()` — pushes the local tier state from step 9 into rqlite. |
| 16 | `start_bedrock_d` | `daemon-reload` + `reset-failed` + `enable --now bedrock-d.service`. |
| 17 | `seaweedfs_install` | `seaweedfs.ensure_install()` — confirms `/usr/local/bin/weed` is present. |
| 18 | `seaweedfs_configs` | Renders `seaweedfs.env`, `master.toml`, `filer.toml`, `s3.json`. |
| 19 | `seaweedfs_start_local` | `seaweedfs.promote_to_master_volume_host()` — weed-master (if in the Raft-3 set) + weed-volume + weed-s3 on this node. |
| 20 | `seaweedfs_start_filer` | `seaweedfs.promote_to_filer_host()`, then polls `127.0.0.1:8333` for up to 15 s for the S3 endpoint to bind (logs a warning, does not fail, if it doesn't). |
| 21 | `seaweedfs_init_collections` | `seaweedfs.init_collections()` — scratch (000) / standard (001) / critical (002) collections via `weed shell`. |
| 22 | `seed_iso_library` | Copies ISOs staged at `/opt/bedrock/iso/` into the filer at `/mnt/bedrock/iso/`; a failure here is logged, not fatal. |

### Private helpers
- `_local_node_name() -> str` — best-effort `socket.gethostname()` (falls back
  to `"node1"`); used as the op's `target_node` so resume matches this node's op.
- `_enrich_params_from_state(params) -> None` — mutates `params` in place,
  filling `cluster_uuid` / `node_name` / `loopback_ip` / `mgmt_ip` from
  `state.json` (via `setdefault`) so resumed steps see what earlier steps wrote.
- `_update_op_params(backend, op_id, new_params) -> None` — rewrites the
  on-disk op row's `params` (and `updated_at`) so a resumed run reads current
  durable state. `FileSagaBackend`-only path.
- `ClusterInit._pick_mgmt_ip(hw) -> str` (staticmethod) — delegates to
  `mgmt_install._pick_mgmt_ip`; first non-loopback IPv4 from detected hardware.

## How it works

`run_cluster_init` is a thin wrapper around the saga executor that handles the
new-vs-resume decision before running:

```
run_cluster_init
  ├─ default cluster_name = bedrock-<hostname> if empty
  ├─ mkdir /var/lib/bedrock ; open FileSagaBackend(init-progress.json)
  ├─ scan ops for kind=cluster_init AND target_node=this AND state!=completed
  │     found?  ──► existing_id, existing_state
  ├─ enrich params {cluster_name, repo} with durable identity from state.json
  └─ existing op?
        ├─ yes, state == "failed"      → executor.retry(id)        (resets, re-runs)
        ├─ yes, otherwise (in-flight)  → executor.execute_one(id)  (resume)
        └─ no                          → submit() new op, execute_one(id)
     result.state != COMPLETED  →  raise RuntimeError(last_step, error)
```

Both the in-flight and failed cases land on "pick up where we left off": the
executor skips already-`done` steps, so a re-run of `bedrock init` after a
crash is safe. Before resuming an existing op, `_update_op_params` writes the
freshly-enriched params back to disk so resumed steps read current durable
state — the steps never trust `ctx`-only values across runs; on resume they
rebuild identity from `state.json` / `cluster.json` / rqlite.

The step list itself is the `bedrock init` flow chart. The load-bearing
ordering constraint is the rqlite boundary:

```
steps 1–11  ── pre-rqlite ── only touch local files / disk / systemd units
                              (no rqlite reads or writes — rqlite isn't up)
step  10    bootstrap_cluster_ca  MUST precede start_rqlited:
                                  rqlited reads its cert files at process
                                  start (no hot-reload)
step  12    start_rqlited ─────── enable+restart, then poll /status:
                              ┌─────────────────────────────┐
                              │ for 60 × 0.5s (= 30s):       │
                              │   curl mTLS /status          │
                              │   raft.state == "Leader"? ───┼─► return
                              └──────────── timeout ─────────┘
                                            └─► raise (FAILS LOUD)
steps 13+   ── post-rqlite ── apply schema, seed cluster_info/node/operator/
                              mgmt_master/obs, then bedrock-d + SeaweedFS
```

`start_rqlited` fails loud on a 30 s timeout because every later step needs a
writable leader; nothing downstream can recover from no-leader, so it surfaces
the last observed raft state and points at `journalctl -u bedrock-rqlited`.

`bootstrap_cluster_ca` writes the cluster's trust material before rqlite can
use it:

```
/var/lib/bedrock/cluster/ca/ca.{key,crt}          CA (this master only)
/var/lib/bedrock/cluster/ca/arbiter.{key,key.pem,crt}   arbiter (.254) TLS
/etc/bedrock/ca.crt                               replicated CA cert
/etc/bedrock/node.crt                             this master's node cert
/etc/bedrock/node.key.pem                         PEM of this master's seed
```

It ensures `/var/lib/bedrock/cluster` exists, generates the CA, ensures the
peer_auth keypair exists and signs this master's per-node cert (bound to
`node_name` + `loopback_ip`), then signs the arbiter cert for the `.254`
address (cluster loopback prefix + `cluster_arbiter.ARBITER_OCTET`). The CA and
node cert generators are idempotent; the master's node cert is re-signed each
run (deterministic, cheap).

Every step is idempotent by contract: re-running a done step is a no-op (most
steps check "is this work already done?" first), and no step does a long
blocking call — the only waits (`start_rqlited`, the S3-bind poll in
`seaweedfs_start_filer`) are bounded loops with explicit timeouts. A step that
cannot proceed raises; the executor records the failure and the operator
decides whether to retry.

## Why

Progress persists to a JSON file (`/var/lib/bedrock/init-progress.json`), not
rqlite, because this saga is the thing that brings rqlite up — steps before
`start_rqlited` have nowhere else to record their state, and the file lets a
half-finished `bedrock init` resume cleanly on re-run. The arbiter cert is
signed at init even though `/var/lib/bedrock/cluster` is a plain root-FS dir at
N=1: the storage promote snapshots that directory onto the DRBD volume as the
cluster grows, so the same cert paths hold before and after.
