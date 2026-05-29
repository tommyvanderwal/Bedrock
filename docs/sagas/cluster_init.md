# Saga: `cluster_init`

**Module:** `bedrock_d/install/cluster_init.py`  
**Class:** `ClusterInit`  
**Entry:** `run_cluster_init(cluster_name, repo)`

## Purpose

First-time bring-up of a Bedrock cluster. Runs on a single node that
becomes the founding master at N=1. Idempotent end-to-end — re-running
``bedrock init`` after a crash resumes from the first incomplete step.

## Trigger

`bedrock init [--name <cluster>]` CLI. The CLI invokes
`run_cluster_init()` directly; there is no HTTP submission because
rqlite isn't up yet — this saga IS what brings it up.

No witness is configured here. The `witnesses` rqlite table starts
empty; a 2-node cluster without an entry runs in "stay put" mode
(current master holds `.254`, no auto-failover). Witnesses are
added later via dashboard / API by writing a row into the
`witnesses` table — no saga needed.

The backend is the **file-based** `FileSagaBackend` at
`/var/lib/bedrock/init-progress.json` (not rqlite); the
backend switches to `RqliteSagaBackend` after this saga's
`start_rqlited` step.

## Inputs (`ctx` keys set by the entry point)

| key | required | type | meaning |
|-----|----------|------|---------|
| `cluster_name` | optional | str | Display tag (defaults to `bedrock-<hostname>`). The real identity is `cluster_uuid`, allocated by `allocate_identity`; the name is just for the dashboard, the mDNS TXT record, and `bedrock status`. Renamable later — see `bedrock cluster rename`. |
| `repo` | required | str | URL or `file://` path of the install repo for fetching binaries. The CLI auto-fills this from the install location the bootstrap ran from (`get_repo()`); operators never type it. |

## Outputs (`ctx` keys filled by the saga's own steps)

| key | filled by | meaning |
|-----|-----------|---------|
| `cluster_uuid` | `allocate_identity` | UUID4 generated at init time, immutable for the cluster's life |
| `node_name` | `allocate_identity` | This node's bedrock-XXXXXX name |
| `loopback_ip` | `allocate_identity` | This node's `100.X.Y.Z/32` on `lo` |
| `cluster_key` | `allocate_identity` | 32-byte symmetric key (AEAD for witness slots, peer-auth gate) |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`prepare_dirs`](#prepare_dirs) | Create `/etc/bedrock`, `/var/lib/bedrock`, `/opt/bedrock` |
| 2 | [`allocate_identity`](#allocate_identity) | Pick cluster_uuid + node_name + loopback_ip + cluster_key |
| 3 | [`write_cluster_key`](#write_cluster_key) | Atomic-write `cluster.key` (0600) |
| 4 | [`write_bootstrap_cluster_json`](#write_bootstrap_cluster_json) | Minimal cluster.json scaffold so rqlited's env can render before rqlite is up |
| 5 | [`install_obs_binaries`](#install_obs_binaries) | Fetch victoria-{metrics,logs}, exporters, vmagent, vlagent |
| 6 | [`install_exporters`](#install_exporters) | Install node_exporter + vm_exporter |
| 7 | [`write_obs_services`](#write_obs_services) | Render systemd units for the observability stack |
| 8 | [`start_obs_services`](#start_obs_services) | Enable + start the exporters/agents |
| 9 | [`provision_storage_n1`](#provision_storage_n1) | `tier_storage.setup_n1()` — thinpool + weed-volume LV + cluster-singleton dir |
| 10 | [`bootstrap_cluster_ca`](#bootstrap_cluster_ca) | Generate the cluster TLS CA + sign this master's node cert + the arbiter cert |
| 11 | [`render_rqlited_env`](#render_rqlited_env) | Write `/etc/bedrock/rqlited.env` |
| 12 | [`start_rqlited`](#start_rqlited) | Enable + start `bedrock-rqlited.service` (single-node Raft) |
| 13 | [`apply_schema`](#apply_schema) | Apply `bedrock_schema.sql` to the fresh rqlite |
| 14 | [`seed_cluster_state`](#seed_cluster_state) | Insert cluster_info, this-node row, default operator |
| 15 | [`mirror_tier_state`](#mirror_tier_state) | Push local tier_state rows into rqlite |
| 16 | [`start_bedrock_d`](#start_bedrock_d) | Enable + start the unified daemon |
| 17 | [`seaweedfs_install`](#seaweedfs_install) | Confirm `/usr/local/bin/weed` is present |
| 18 | [`seaweedfs_configs`](#seaweedfs_configs) | Render seaweed env + master/filer/s3 configs |
| 19 | [`seaweedfs_start_local`](#seaweedfs_start_local) | Start weed-master + weed-volume + weed-s3 |
| 20 | [`seaweedfs_start_filer`](#seaweedfs_start_filer) | Start weed-filer on `.254` (cluster singleton) |
| 21 | [`seaweedfs_init_collections`](#seaweedfs_init_collections) | Create scratch/standard/critical collections + buckets |
| 22 | [`seed_iso_library`](#seed_iso_library) | Seed `/mnt/bedrock/iso/` with the bundled Alpine cloud image |

## Revert

There is no `cluster_init`-inverse saga. A node that wants to
abandon its cluster identity runs `bedrock node reset` (see
`tier_storage.node_reset_local()`) which tears down DRBD, removes
the LVs, unmounts, wipes `/etc/bedrock`, and restores the box to a
pre-init state. Use this only on the node being reset — it doesn't
touch peers.

## Idempotency / resume

Re-running `bedrock init` after a crash mid-step resumes from the
first not-`done` step in `init-progress.json`. Each step's body
opens with the idempotency check appropriate to its effect (e.g.
`apply_schema` is `CREATE TABLE IF NOT EXISTS`; `start_rqlited`
checks `systemctl is-active`). The saga can be safely re-run any
number of times on the same node — the only thing that's
"single-use" is the cluster_uuid + cluster_key allocated by
`allocate_identity`, and that step persists them into state.json
on first run, so re-runs read them back.

## Step details

### `prepare_dirs`

Creates the directory layout under `/etc/bedrock`, `/var/lib/bedrock`,
and `/opt/bedrock` with the right modes. No-op if the dirs already
exist with the right modes; otherwise `mkdir -p` + `chmod`.

### `allocate_identity`

Generates a fresh `cluster_uuid` (UUID4), derives the cluster's
`100.X.Y.0/24` loopback prefix from the UUID, picks this node's
`node_name` as the system hostname, and assigns it `loopback_ip =
100.X.Y.1/32` (the master always gets octet 1 at init). Generates a
32-byte `cluster_key` for AEAD on witness slots + peer-auth gate.
All four values persist into `/etc/bedrock/state.json` so re-runs
read them back instead of regenerating.

### `write_cluster_key`

Atomic-writes `/etc/bedrock/cluster.key` with mode 0600.

### `write_bootstrap_cluster_json`

Writes a minimal `/etc/bedrock/cluster.json` scaffold containing
`cluster_name`, `cluster_uuid`, this node, and empty `tiers` /
`witnesses` / `vms` blocks. This is a **bootstrap-only** file: it
gives `rqlite_setup.render_env_file()` something to read before rqlite
is up. Once `start_rqlited` + `seed_cluster_state` land, rqlite is the
authoritative store and every consumer reads it directly via
`cluster_state.load_cluster()` — there is no steady-state `cluster.json`
projection (that layer was removed 2026-05-26; only `state.json` is
re-projected per revision).

### `install_obs_binaries`

Fetches the observability binaries (victoria-metrics, victoria-logs,
vmagent, vlagent, vmbackup, vmrestore) from the install repo. Skips
files already present at the right size in `/opt/bedrock/bin/`.

### `install_exporters`

Same pattern as `install_obs_binaries`, scoped to `node_exporter`
and `vm_exporter`.

### `write_obs_services`

Renders the systemd units for the obs stack into
`/etc/systemd/system/`. Idempotent — units only re-rendered if
their content would change.

### `start_obs_services`

`systemctl enable --now` for each obs unit. No-op if the unit is
already active.

### `provision_storage_n1`

Calls `tier_storage.setup_n1()`: ensure the LVM thinpool, create the
SeaweedFS volume LV (`bedrock-weed-volume`, 30G, XFS, mounted at
`/var/lib/bedrock/seaweedfs/volumes` — no DRBD), and create the
cluster-singleton directory at `/var/lib/bedrock/cluster`. At N=1 the
cluster singleton (arbiter rqlite + filer leveldb3 + S3 IAM) lives as
a plain directory on the root FS; it flips to a DRBD primary via the
[`cluster_tier_promote_master`](cluster_tier_promote_master.md) saga
when the cluster grows to N=2 (the dir contents are snapshotted and
restored onto the DRBD volume, XFS preserved byte-for-byte by external
metadata). Records `tiers.cluster.mode = "local"` locally;
`write_rqlite=False` here because rqlite isn't up yet — `mirror_tier_state`
pushes it into rqlite later.

### `bootstrap_cluster_ca`

Calls `cluster_ca` + `peer_auth` to stand up the cluster's TLS PKI
before rqlited starts (rqlited reads its cert files at process start —
no hot-reload). Generates the CA (`/var/lib/bedrock/cluster/ca/ca.{key,crt}`,
master-only), signs this master's per-node cert
(`/etc/bedrock/node.crt`, `/etc/bedrock/node.key.pem`, replicated CA at
`/etc/bedrock/ca.crt`), and signs the arbiter cert for the
`.254` loopback. Idempotent — the CA/arbiter generators skip existing
files; the master's node cert is re-signed each run (cheap, deterministic).
The CA lives under `/var/lib/bedrock/cluster`, so it migrates onto the
DRBD volume automatically on the N=1→N=2 promote.

### `render_rqlited_env`

Writes `/etc/bedrock/rqlited.env` with the per-node rqlite config.
The node_id is derived from the loopback's last octet so it stays
stable across reboots and joins.

### `start_rqlited`

`systemctl enable --now bedrock-rqlited` — single-node Raft cluster
on this loopback. Polls `https://127.0.0.1:4001/status` (mTLS, using
the node cert/key/CA from `bootstrap_cluster_ca`) until the Raft state
reads `Leader`, then returns. Fails loud on a 30 s timeout — the seed
step needs a writable leader.

### `apply_schema`

Loads `bedrock_schema.sql` and runs each `CREATE TABLE IF NOT EXISTS`
through the rqlite client. Safe to re-run.

### `seed_cluster_state`

Inserts the singleton `cluster_info` row, registers this node into
`nodes` (with its `bedrock_pubkey` + loopback), seeds the default
operator (`root` / `admin` — to be changed via `bedrock operator
passwd`), sets this node as `metrics`/`logs` obs backend, and sets
`mgmt_master = self`. All upserts (`INSERT … ON CONFLICT DO UPDATE`).

### `mirror_tier_state`

Calls `tier_storage.mirror_tier_state_to_rqlite()` to push every
locally-known tier_state row into the rqlite `tiers` table. Now
followers and future joiners see the same view.

### `start_bedrock_d`

`systemctl enable --now bedrock-d`. This is the moment the
orchestrator's `rqlite_subscriber` task begins running and the
calm loop takes over.

### `seaweedfs_install`

`/usr/local/bin/weed` presence check. Already shipped via the ISO.

### `seaweedfs_configs`

Renders `/etc/bedrock/seaweedfs.env` + `seaweedfs-master.toml` +
`seaweedfs-filer.toml` + `seaweedfs-s3.json`.

### `seaweedfs_start_local`

Starts `bedrock-weed-master`, `bedrock-weed-volume`, `bedrock-weed-s3`
on this node. Filer comes next.

### `seaweedfs_start_filer`

Starts `bedrock-weed-filer`. The filer binds `.254:8888` — at N=1
that means binding the master's own loopback alias.

### `seaweedfs_init_collections`

Runs `weed shell` commands to create the `scratch`, `standard`, and
`critical` collections and their buckets (`scratch`, `iso`,
`templates`, `snapshots`, `backups`). Idempotent — `weed shell` no-ops
on already-existing collections.

### `seed_iso_library`

Copies the bundled Alpine cloud image into `/mnt/bedrock/iso/` so
fresh installs have one ISO available for cattle/pet VMs without
external dependencies.
