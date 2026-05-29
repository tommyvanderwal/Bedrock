# Saga: `cluster_init`

**Module:** `bedrock_d/install/cluster_init.py` — class `ClusterInit`
**Entry:** `run_cluster_init(*, cluster_name=None, repo)`

## Summary

**What:** first-time bring-up of a Bedrock cluster. Runs every step needed to
turn a freshly-bootstrapped box into a founding master at N=1: identity, TLS PKI,
local storage, single-node rqlite, schema + seed rows, the `bedrock-d` daemon,
and the SeaweedFS object store.

**When:** the operator runs `bedrock init [--name <cluster>]` (`cmd_init` in
`installer/bedrock`). The CLI calls `run_cluster_init()` in-process — there is no
HTTP submission because rqlite, which the mgmt API needs, is the thing this saga
brings up.

**Where:** on the single node being initialised. Identity and progress live in
local files until `start_rqlited`.

**Backend:** `FileSagaBackend` at `/var/lib/bedrock/init-progress.json` (one
operation row + per-step state). Steps 1-12 run before rqlite exists, so progress
can't go to rqlite; the file is read on re-run so a half-completed init resumes
from the first not-`done` step.

**End state:** N=1 cluster — this node is `mgmt_master`, holds loopback octet 1,
runs `bedrock-d` + single-node rqlite + the full SeaweedFS stack, and is ready to
accept joins. The `witnesses` table is empty: a 2-node cluster with no witness
stays put (current master holds `.254`, no auto-failover) until a witness row is
written via dashboard/API.

### Inputs (`ctx`)

| key | required | meaning |
|-----|----------|---------|
| `cluster_name` | optional | Display tag; defaults to `bedrock-<hostname>`. The real identity is `cluster_uuid`. Used by the dashboard, the mDNS TXT record, and `bedrock status`. Changed later via `bedrock cluster rename`. |
| `repo` | required | URL or `file://` of the install repo for fetching binaries. The CLI auto-fills this from where bootstrap ran; operators never type it. |

### Outputs (`ctx` keys set by the saga, also persisted to `state.json`)

| key | set by | meaning |
|-----|--------|---------|
| `cluster_uuid` | `allocate_identity` | UUID4, immutable for the cluster's life |
| `node_name` | `allocate_identity` | this node's name (the system hostname) |
| `loopback_ip` | `allocate_identity` | `100.X.Y.1/32` — master always gets octet 1 |
| `mgmt_ip` | `allocate_identity` | this node's LAN IPv4 (first non-loopback) |

### Steps

| # | Step | What it does |
|---|------|--------------|
| 1 | prepare_dirs | mkdir `/opt/bedrock/{bin,iso}`, `/var/lib/bedrock/{vm,vl}`, `/opt/bedrock/mgmt` |
| 2 | allocate_identity | pick `cluster_uuid`, `node_name`, `loopback_ip` (.1), `mgmt_ip`; write `state.json` |
| 3 | write_cluster_key | write `/etc/bedrock/cluster.key` (32 random bytes, 0600) |
| 4 | write_bootstrap_cluster_json | minimal `cluster.json` scaffold so `rqlited.env` can render pre-rqlite |
| 5 | install_obs_binaries | fetch `victoria-metrics` + `victoria-logs` |
| 6 | install_exporters | install `node_exporter` + `vm_exporter` |
| 7 | write_obs_services | render `bedrock-vm` + `bedrock-vl` units, `scrape.yml`, dashboard |
| 8 | start_obs_services | `enable --now` `bedrock-vm` + `bedrock-vl` |
| 9 | provision_storage_n1 | thinpool + weed-volume LV + cluster-singleton dir |
| 10 | bootstrap_cluster_ca | cluster TLS CA + master node cert + arbiter cert |
| 11 | render_rqlited_env | write `/etc/bedrock/rqlited.env` |
| 12 | start_rqlited | `enable --now bedrock-rqlited`; poll until raft=Leader |
| 13 | apply_schema | apply `bedrock_schema.sql` (`CREATE TABLE IF NOT EXISTS`) |
| 14 | seed_cluster_state | seed `cluster_info`, this node, default operator, obs backends, `mgmt_master` |
| 15 | mirror_tier_state | push the cluster singleton's tier row into rqlite |
| 16 | start_bedrock_d | `enable --now bedrock-d` (orchestrator + netd take over) |
| 17 | seaweedfs_install | confirm `/usr/local/bin/weed` is present |
| 18 | seaweedfs_configs | render seaweed env + master/filer/s3 configs |
| 19 | seaweedfs_start_local | start weed-master (if in Raft-3 set) + weed-volume + weed-s3 |
| 20 | seaweedfs_start_filer | start weed-filer on `.254`; wait for s3 on :8333 |
| 21 | seaweedfs_init_collections | configure path→collection→replication policies |
| 22 | seed_iso_library | copy ISOs staged in `/opt/bedrock/iso/` into the filer's `/iso/` |

## Idempotency / resume

Each step's first lines are its idempotency check (file exists, `systemctl
is-active`, `CREATE … IF NOT EXISTS`, `INSERT OR REPLACE`). Re-running
`bedrock init` after a crash skips every `done` step and continues from the first
not-`done` one. A re-run on an already-complete init is a no-op. The only
single-use values are `cluster_uuid` + `cluster.key`, persisted on first run so
re-runs read them back.

`ctx` is not persisted between runs. On resume the executor rebuilds it from the
operation's `params`; `run_cluster_init` enriches `params` from `state.json`
(`cluster_uuid`, `node_name`, `loopback_ip`, `mgmt_ip`) so resumed steps see what
`allocate_identity` already wrote.

## Revert

There is no inverse saga. To return a node to its post-`bedrock bootstrap` state,
`tier_storage.node_reset_local()` runs (over SSH from `bedrock storage
remove-peer`, or directly via the hidden `bedrock storage _local-reset`): stop
bedrock services, tear down DRBD + its `.res` files, unmount FUSE/DRBD/local LVs,
drop fstab entries, remove the cluster + weed-volume LVs, remove
`/opt/bedrock/{mgmt,iso,data}`, and truncate `state.json` to `{hardware,
bootstrap_done}`. It preserves OS packages, the DRBD kernel module, `br0` + ring
NICs, SSH keys, and the VG + thinpool. After it the box can `bedrock init` again
or `bedrock join`. Idempotent; affects only the node it runs on.

## Step details

### 1. `prepare_dirs`
Creates `/opt/bedrock/bin`, `/var/lib/bedrock/{vm,vl}`, `/opt/bedrock/mgmt`, and
`/opt/bedrock/iso`. **Revert:** dirs removed by `node_reset_local`. **Idempotent:**
`mkdir -p`.

### 2. `allocate_identity`
Loads `state.json`, generates `cluster_uuid` (UUID4) if absent, sets
`node_name` (the detected hostname), derives the cluster's `100.X.Y.0/24` from the
UUID and assigns `loopback_ip = .1` (`cluster_addr.node_loopback_ip(uuid, 1)`),
picks `mgmt_ip` (first non-loopback IPv4) and sets `mgmt_url =
https://<mgmt_ip>:8443`, then saves `state.json`. **Revert:** identity cleared by
`node_reset_local`. **Idempotent:** re-uses existing `cluster_uuid`/`node_name`.

### 3. `write_cluster_key`
`daemon_setup.write_cluster_key()` writes `/etc/bedrock/cluster.key` (32 random
bytes, 0600) — the AEAD key for witness slots and the peer-auth gate. **Revert:**
removed with `/etc/bedrock` contents on reset. **Idempotent:** respects an
existing file.

### 4. `write_bootstrap_cluster_json`
Writes a minimal `/etc/bedrock/cluster.json` with `cluster_uuid`, `cluster_name`,
`mgmt_master = self`, this node, and empty `tiers`/`witnesses`/`vms`/… blocks.
`cluster.json` is a local bootstrap file holding the rqlite peer list, read by
`rqlite_setup.render_env_file()` at every boot (rqlite can't report its own peers
before it starts). It is not a runtime projection — once rqlite is up, every
consumer reads it via `cluster_state.load_cluster()`. **Revert:** removed on
reset. **Idempotent:** written verbatim each run.

### 5. `install_obs_binaries`
Downloads `victoria-metrics` and `victoria-logs` from `<repo>/binaries/` into
`/opt/bedrock/bin/`, `chmod 0755`. **Revert:** binaries removed on reset.
**Idempotent:** skips a binary already present.

### 6. `install_exporters`
`exporters.install(repo)` fetches `node_exporter` + `vm_exporter.py` and writes
their units. **Revert:** removed on reset. **Idempotent:** skips an existing
`node_exporter`; re-fetches `vm_exporter.py` (cheap).

### 7. `write_obs_services`
Writes `scrape.yml` (node:9100, libvirt:9177), renders the `bedrock-vm`
(VictoriaMetrics :8428) and `bedrock-vl` (VictoriaLogs :9428, syslog :5140) units,
and installs the dashboard (`with_metrics=True`). **Revert:** units removed on
reset. **Idempotent:** overwrites with current content (cheap).

### 8. `start_obs_services`
`systemctl enable --now bedrock-vm bedrock-vl`. **Revert:** stopped on reset.
**Idempotent:** no-op if already active.

### 9. `provision_storage_n1`
`tier_storage.setup_n1(write_rqlite=False)`: ensure the LVM thinpool, create the
SeaweedFS volume LV (`bedrock-weed-volume`, 30G, XFS, no DRBD, mounted at
`/var/lib/bedrock/seaweedfs/volumes`), and create the cluster-singleton directory
`/var/lib/bedrock/cluster`. At N=1 the cluster singleton (arbiter rqlite + filer
leveldb3 + S3 IAM) is a plain dir on the root FS; the
[`cluster_tier_promote_master`](cluster_tier_promote_master.md) saga snapshots
and restores its contents onto a DRBD volume at N=1→N=2 (XFS preserved
byte-for-byte by external metadata), so paths are identical before and after.
Records `tiers.cluster.mode = local` locally only; `write_rqlite=False` because
rqlite isn't up — `mirror_tier_state` pushes it later. **Revert:** LVs/dir removed
on reset. **Idempotent:** each helper checks existence first.

### 10. `bootstrap_cluster_ca`
Stands up the TLS PKI before rqlited starts (rqlited reads its certs at process
start; no hot-reload). Ensures `/var/lib/bedrock/cluster`, generates the CA
(`/var/lib/bedrock/cluster/ca/ca.{key,crt}`, master-only), ensures the peer-auth
keypair, signs and installs the master's node cert (`/etc/bedrock/node.crt`,
`/etc/bedrock/node.key.pem`, CA at `/etc/bedrock/ca.crt`), and signs the arbiter
cert for `<prefix>.254` (`cluster_arbiter.ARBITER_OCTET`). The CA lives under
`/var/lib/bedrock/cluster`, so it migrates onto the DRBD volume with the singleton
on the N=1→N=2 promote. **Revert:** cert files removed on reset. **Idempotent:**
CA/arbiter generators skip existing files; the node cert is re-signed each run
(cheap, deterministic).

### 11. `render_rqlited_env`
`rqlite_setup.render_env_file()` writes `/etc/bedrock/rqlited.env` from
`cluster.json` + `state.json`. The rqlite node-id is the loopback's last octet so
it stays stable across reboots and joins. **Revert:** removed on reset.
**Idempotent:** overwrites with the current rendering.

### 12. `start_rqlited`
`reset-failed` + `enable` + `restart bedrock-rqlited` (single-node Raft on this
loopback), then polls `https://127.0.0.1:4001/status` over mTLS (node
cert/key/CA) for up to 30 s until `store.raft.state == Leader`. **Fails loud** on
timeout — the seed step needs a writable leader. **Revert:** stopped on reset.
**Idempotent:** restart on a running rqlited is cheap.

### 13. `apply_schema`
Applies `bedrock_schema.sql` to rqlite via `bedrock_d.state`. **Revert:** none
needed (DB removed with the LV on reset). **Idempotent:** every statement is
`CREATE TABLE IF NOT EXISTS`.

### 14. `seed_cluster_state`
In one rqlite session: insert the singleton `cluster_info` row, register this node
into `nodes` (with its SSH pubkey + `bedrock_pubkey`), set its loopback, seed the
default operator (`root` / `admin`, changed later via `bedrock operator passwd`),
set this node as the `metrics` + `logs` obs backend, and set `mgmt_master = self`.
**Revert:** rows go away with the rqlite DB on reset. **Idempotent:** all writes
are `INSERT OR REPLACE` / upsert.

### 15. `mirror_tier_state`
`tier_storage.mirror_tier_state_to_rqlite()` pushes the cluster singleton's row
(`mode=local`, `backend_path=/var/lib/bedrock/cluster`) into the rqlite `tiers`
table so followers and future joiners see it. **Revert:** none needed.
**Idempotent:** `INSERT OR REPLACE`.

### 16. `start_bedrock_d`
`daemon-reload` + `reset-failed` + `enable --now bedrock-d`. The unified daemon
starts: its orchestrator tasks (`rqlite_subscriber`, `boot_orchestrator`,
`no_quorum_responder`, `converge_retry`, `backup_scheduler`) and the netd thread
begin running. **Revert:** stopped on reset. **Idempotent:** `enable --now`.

### 17. `seaweedfs_install`
`seaweedfs.ensure_install()` verifies `/usr/local/bin/weed` (staged by
`install.sh`) and creates the seaweed directory tree. **Revert:** dirs removed on
reset. **Idempotent:** check + `mkdir -p`.

### 18. `seaweedfs_configs`
Renders `/etc/bedrock/seaweedfs.env`, `seaweedfs-master.toml`,
`seaweedfs-filer.toml`, and `seaweedfs-s3.json`. **Revert:** removed on reset.
**Idempotent:** overwrites with the current rendering.

### 19. `seaweedfs_start_local`
`seaweedfs.promote_to_master_volume_host()`: weed-volume (:8080) and weed-s3
(:8333) on every node; weed-master (:9333) only if this loopback is in the Raft-3
master set (the lowest-octet nodes; at N=1 that is this node). **Revert:** stopped
on reset. **Idempotent:** `enable --now`; stops/disables master if this node is
not in the set.

### 20. `seaweedfs_start_filer`
`seaweedfs.promote_to_filer_host()` starts the filer singleton bound to
`.254:8888` — at N=1 the master's own loopback alias. Then polls `127.0.0.1:8333`
for up to 15 s so post-init smoke tests can PUT objects (logs a warning, does not
fail, if s3 doesn't bind). **Revert:** stopped on reset. **Idempotent:** starting
a running filer is a no-op.

### 21. `seaweedfs_init_collections`
`seaweedfs.init_collections()` runs `weed shell fs.configure -apply` to map filer
path prefixes to collection + replication: `/scratch/`→scratch/000,
`/iso/ /templates/ /snapshots/`→standard, `/backups/`→critical. Replication scales
with N (`standard` 000→001 at N≥2; `critical` →002 at N≥3), so an N=1 box can
still take ISO/template uploads. **Revert:** none needed. **Idempotent:**
`fs.configure -apply` overwrites the policy for the same prefix.

### 22. `seed_iso_library`
`seaweedfs.seed_iso_library()` mounts the shared FUSE namespace if needed and
copies any `*.iso` staged in `/opt/bedrock/iso/` (e.g. `virtio-win.iso` baked into
the install ISO) into the filer's `/iso/` subtree, visible as
`/mnt/bedrock/iso/<name>.iso` on every node. **Revert:** none needed. **Idempotent:**
skips files already present; wrapped so a failure logs a warning rather than
failing init.
