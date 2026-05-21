# Saga: `cluster_init`

**Module:** `bedrock_d/install/cluster_init.py`  
**Class:** `ClusterInit`  
**Entry:** `run_cluster_init(cluster_name, repo)`

## Purpose

First-time bring-up of a Bedrock cluster. Runs on a single node that
becomes the founding master at N=1. Idempotent end-to-end — re-running
``bedrock init`` after a crash resumes from the first incomplete step.

## Trigger

`bedrock init --name <cluster>` CLI. The CLI invokes
`run_cluster_init()` directly; there is no HTTP submission because
rqlite isn't up yet — this saga IS what brings it up.

### Why no `--witness` flag at init

At N=1 the cluster has no quorum problem to solve — the witness is
a tiebreaker that only becomes load-bearing on the **N=1 → N=2**
transition. Configuring a witness during `bedrock init` would force
the operator to answer a question they can't reasonably answer yet
(the BedRock-Echo box may not even be deployed; the operator is
just standing up the master).

Without a witness configured, a 2-node cluster runs in **"stay
put" mode**: neither side will auto-failover. The current master
keeps `.254` and singletons; a surviving peer doesn't attempt a
takeover. The cluster is functional but can't survive the master
dying without operator intervention. That's the documented and
intentional trade-off for cattle-only 2-node deployments.

Configuring a witness later happens at the dashboard level — at
the moment the operator clicks "accept" on the first joiner is a
good UX hook for "would you like to scan for a witness now?". The
mgmt API supports this via the (yet-to-be-written) witness-CRUD
endpoints; no saga involvement needed — adding a row to the
`witnesses` rqlite table is enough for netd to start probing it
on the next tick.

The backend is the **file-based** `FileSagaBackend` at
`/var/lib/bedrock/init-progress.json` (not rqlite); the
backend switches to `RqliteSagaBackend` after this saga's
`start_rqlited` step.

## Inputs (`ctx` keys set by the entry point)

| key | type | meaning |
|-----|------|---------|
| `cluster_name` | str | Human-readable cluster name (e.g. `bedrock-prod`) |
| `repo` | str | URL or `file://` path of the install repo for fetching binaries |

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
| 4 | [`write_bootstrap_cluster_json`](#write_bootstrap_cluster_json) | Minimal cluster.json so netd can start |
| 5 | [`install_obs_binaries`](#install_obs_binaries) | Fetch victoria-{metrics,logs}, exporters, vmagent, vlagent |
| 6 | [`install_exporters`](#install_exporters) | Install node_exporter + vm_exporter |
| 7 | [`write_obs_services`](#write_obs_services) | Render systemd units for the observability stack |
| 8 | [`start_obs_services`](#start_obs_services) | Enable + start the exporters/agents |
| 9 | [`provision_storage_n1`](#provision_storage_n1) | `tier_storage.setup_n1()` — thinpool + 3 tier LVs + mounts |
| 10 | [`render_rqlited_env`](#render_rqlited_env) | Write `/etc/bedrock/rqlited.env` |
| 11 | [`start_rqlited`](#start_rqlited) | Enable + start `bedrock-rqlited.service` (single-node Raft) |
| 12 | [`apply_schema`](#apply_schema) | Apply `bedrock_schema.sql` to the fresh rqlite |
| 13 | [`seed_cluster_state`](#seed_cluster_state) | Insert cluster_info, this-node row, default operator |
| 14 | [`mirror_tier_state`](#mirror_tier_state) | Push local tier_state rows into rqlite |
| 15 | [`start_bedrock_d`](#start_bedrock_d) | Enable + start the unified daemon |
| 16 | [`seaweedfs_install`](#seaweedfs_install) | Confirm `/usr/local/bin/weed` is present |
| 17 | [`seaweedfs_configs`](#seaweedfs_configs) | Render seaweed env + master/filer/s3 configs |
| 18 | [`seaweedfs_start_local`](#seaweedfs_start_local) | Start weed-master + weed-volume + weed-s3 |
| 19 | [`seaweedfs_start_filer`](#seaweedfs_start_filer) | Start weed-filer on `.254` (cluster singleton) |
| 20 | [`seaweedfs_init_collections`](#seaweedfs_init_collections) | Create scratch/standard/critical collections + buckets |
| 21 | [`seed_iso_library`](#seed_iso_library) | Seed `/mnt/bedrock/iso/` with the bundled Alpine cloud image |

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

Writes a minimal `/etc/bedrock/cluster.json` containing
`cluster_name`, `cluster_uuid`, this node, and an empty `tiers` /
`witnesses` / `vms` block. Enough that netd can start and bedrock-d's
orchestrator can boot — the rqlite-projected version overwrites it
after `start_rqlited` + `seed_cluster_state` land.

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

Calls `tier_storage.setup_n1()`: ensure the thinpool, create the
three tier LVs (`tier-scratch` 20G, `tier-bulk` 30G, `tier-critical`
5G), put XFS on each, mount under `/var/lib/bedrock/local/<tier>`,
and `atomic_symlink` `/bedrock/<tier>` to point at the local mount.
At N=1 every tier stays on the local LV — the `critical` tier flips
to DRBD via the [`cluster_tier_promote_master`](cluster_tier_promote_master.md)
saga when the cluster grows to N=2.

### `render_rqlited_env`

Writes `/etc/bedrock/rqlited.env` with the per-node rqlite config
(node_id derived from the loopback's last octet for stability —
see [`lesson_rqlite_node_id_stability`](../../../.claude/projects/-home-tommy-projects/memory/lesson_rqlite_node_id_stability.md)).

### `start_rqlited`

`systemctl enable --now bedrock-rqlited` — single-node Raft cluster
on this loopback. Polls until rqlited reports it has a leader
(itself) before returning.

### `apply_schema`

Loads `bedrock_schema.sql` and runs each `CREATE TABLE IF NOT EXISTS`
through the rqlite client. Safe to re-run.

### `seed_cluster_state`

Inserts the singleton `cluster_info` row, registers this node into
`nodes`, sets `mgmt_master = self`, seeds the default operator
(`root` / `admin` — to be changed via `bedrock operator passwd`).
All upserts (`INSERT … ON CONFLICT DO UPDATE`).

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
