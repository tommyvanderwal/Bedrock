# Saga: `node_join`

**Module:** `bedrock_d/install/node_join.py`
**Class:** `NodeJoin`
**Entry:** `run_node_join(witness, cluster_info, repo)`

## Purpose

Add a new node to an existing Bedrock cluster. Runs end-to-end on the
joining node: requests operator approval, provisions local storage,
starts bedrock-d, joins rqlite as a Raft voter, brings up SeaweedFS
volume + s3 locally, (at N≥2) joins the cluster-singleton DRBD
(`cluster`) as Secondary, and finally flips its own rqlite `nodes.state`
from `joining` to `active`.

## Trigger

`bedrock join [<node-ip>]` CLI. `<node-ip>` is any current cluster
member (not necessarily the master); omit it to auto-discover via mDNS
on the LAN. The CLI fetches cluster info from the chosen node, shows
this node's fingerprint for operator approval on the dashboard, then
calls `run_node_join()`.

The `witness` ctx key holds the IP/hostname the CLI dialled (any cluster
node), not the cluster witness (Echo) host — it carries the same value
through to `state.json` as `witness_host`.

Backend is the file-based `FileSagaBackend` at
`/var/lib/bedrock/init-progress.json` (same path and `kind`-keyed store
as [`cluster_init`](cluster_init.md)) — rqlite isn't this node's yet, so
saga progress can't live there.

## Inputs (`ctx` keys set by the entry point)

| key | type | meaning |
|-----|------|---------|
| `witness` | str | Host the CLI dialled to fetch cluster info |
| `cluster_info` | dict | What that node returned: `cluster_uuid`, `cluster_name`, `mgmt_url`, existing `nodes` |
| `repo` | str | Install repo URL for fetching binaries |

`_enrich_params_from_state` also seeds `cluster_uuid`, `node_name`,
`loopback_ip`, `mgmt_ip`, and `bedrock_pubkey` from durable sources
(state.json + the on-disk Ed25519 key) so a resumed run still has them.

## Outputs (`ctx` keys filled by steps + enrichment)

| key | filled by | meaning |
|-----|-----------|---------|
| `mgmt_ip` | `detect_mgmt_ip` | This node's primary LAN/bridge IP |
| `node_name` | `derive_identity` | This node's name (system hostname) |
| `bedrock_pubkey` | `derive_identity` | Ed25519 inter-node identity pubkey (hex) |
| `request_id` | `request_join_approval` | Join handshake id |
| `loopback_ip` | `request_join_approval` | `/32` allocated by the master in the sealed reply |
| `master_loopback` | `request_join_approval` | Master's loopback (rqlite `-join` target, mesh ping target) |
| `cluster_uuid` | `request_join_approval` | From `cluster_info` |
| `approval` | `request_join_approval` | Full sealed-reply dict: peer pubkeys/IPs, node map, certs |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`prepare_dirs`](#prepare_dirs) | Create `/mnt/bedrock`, `/opt/bedrock`, `/root/.ssh` |
| 2 | [`detect_mgmt_ip`](#detect_mgmt_ip) | Pick this node's LAN/bridge IP (prefer `br0`) |
| 3 | [`derive_identity`](#derive_identity) | Ensure Ed25519 inter-node key + set `node_name` |
| 4 | [`install_exporters`](#install_exporters) | node_exporter + vm_exporter |
| 5 | [`request_join_approval`](#request_join_approval) | ECDH handshake + `POST /api/join/request`; block on operator approval; install certs |
| 6 | [`write_state_json`](#write_state_json) | Persist identity + role into `state.json` |
| 7 | [`write_bootstrap_cluster_json`](#write_bootstrap_cluster_json) | Bootstrap `cluster.json` for rqlited env render |
| 8 | [`install_peer_pubkeys`](#install_peer_pubkeys) | Append peers' SSH pubkeys to `authorized_keys` |
| 9 | [`prescan_peer_hostkeys`](#prescan_peer_hostkeys) | `ssh-keyscan` peers into `known_hosts` |
| 10 | [`provision_storage_n1`](#provision_storage_n1) | `tier_storage.setup_n1()` |
| 11 | [`pre_extract_mgmt`](#pre_extract_mgmt) | Extract staged `mgmt.tar.gz` into `/opt/bedrock` |
| 12 | [`start_bedrock_d`](#start_bedrock_d) | Enable + start the unified daemon |
| 13 | [`wait_master_reachable`](#wait_master_reachable) | Ping the master's loopback over the mesh |
| 14 | [`render_rqlited_env`](#render_rqlited_env) | rqlited config with `-join <master_loopback>:4001` |
| 15 | [`start_rqlited_joiner`](#start_rqlited_joiner) | Start rqlited as a Raft follower |
| 16 | [`install_dashboard`](#install_dashboard) | Stage the dashboard build under `/opt/bedrock/mgmt` |
| 17 | [`seaweedfs_install`](#seaweedfs_install) | Confirm `/usr/local/bin/weed` present |
| 18 | [`seaweedfs_configs`](#seaweedfs_configs) | Render seaweed env + per-node configs |
| 19 | [`seaweedfs_start_local`](#seaweedfs_start_local) | Start weed-volume/s3 (+ master if in Raft-3 set) |
| 20 | [`fuse_mount`](#fuse_mount) | FUSE-mount `/mnt/bedrock` from `.254:8888` |
| 21 | [`cluster_tier_join_peer`](#cluster_tier_join_peer) | At N≥2, join the `cluster` DRBD as Secondary |
| 22 | [`activate_node`](#activate_node) | Flip rqlite `nodes.state` `joining` → `active` |

## Revert

The inverse saga is [`node_leave`](node_leave.md), run as
`bedrock node leave <node-to-remove>` on a surviving node (normally the
master, never the target itself). It writes `node_unregister` to rqlite,
drops the leaver's Raft voter slot via `DELETE /remove`, re-shuffles
cluster-DRBD membership if the leaver carried a replica, and stops
services on the target best-effort. After it runs cleanly, the removed
node can `bedrock node reset` to wipe local state and start over.

## Idempotency / resume

Re-running `bedrock join` after a crash picks up the existing
`init-progress.json` op for this node and resumes from the first
not-`done` step (`retry` if the op failed, `execute_one` otherwise).
`_enrich_params_from_state` re-seeds identity so a resumed run after
`derive_identity` was already done still has `node_name` +
`bedrock_pubkey` in ctx.

`request_join_approval` is the only blocking step — it polls the master
at 2 s intervals up to 10 min and gives up if no operator approves. The
saga can be re-run after approval (a fresh handshake issues a new
`request_id`; the master's allocation is stable per node).

## Step details

### `prepare_dirs`

`mkdir -p` for `/mnt/bedrock`, `/opt/bedrock`, and `/root/.ssh` (mode
0700). Idempotent.

### `detect_mgmt_ip`

Reads the node's hardware NIC inventory from `state.json` and picks an
IPv4: prefer an UP `br0`, else the first UP NIC with a non-link-local
address. Sets `ctx["mgmt_ip"]` — the address peers dial for mgmt HTTPS
on 8443. Raises if no usable NIC.

### `derive_identity`

Calls `peer_auth.pubkey_hex()` — creates the Ed25519 keypair on first
call, reads it after, so it is idempotent. Sets `ctx["node_name"]` to
the system hostname (falling back to `node<N+1>` from the existing node
count) and `ctx["bedrock_pubkey"]` to the hex public key.

### `install_exporters`

Fetches node_exporter + vm_exporter from the install repo and writes
their systemd units (`exporters.install`, check-and-skip).

### `request_join_approval`

Generates an ephemeral X25519 keypair and `POST`s `/api/join/request` to
the master (`mgmt_install._request_join`) with this node's name,
`mgmt_ip`, `bedrock_pubkey`, the ephemeral pubkey, and the node's SSH
pubkey. The master records a `pending` row; an operator approves on the
dashboard (`POST /api/join/approve`), which allocates this node's
loopback `/32` and registers it in rqlite as state `joining`.

The approval reply is sealed via ECDH (master ephemeral + this node's
ephemeral, HKDF-salted with the `request_id`). The step decrypts it,
writes `cluster.key` (0600) to `/etc/bedrock/`, stores `loopback_ip`,
`master_loopback`, peer pubkeys/IPs, and the node map in
`ctx["approval"]`, and — when the reply carries `node_cert_pem` +
`ca_cert_pem` — installs the CA-signed TLS cert + CA cert + PEM node key
(`cluster_ca.install_node_cert`) so rqlited can come up with mTLS.

Polls up to 10 min; re-running issues a fresh `request_id`.

### `write_state_json`

Persists into `/etc/bedrock/state.json` (atomic): `cluster_name`,
`cluster_uuid`, `role=compute`, `node_id`, `node_name`, `witness_host`,
`mgmt_url`, `mgmt_ip`, `loopback_ip`. Idempotent — `state.save`
overwrites atomically.

### `write_bootstrap_cluster_json`

Writes `/etc/bedrock/cluster.json` — the local bootstrap file holding
`cluster_uuid`, `cluster_name`, `mgmt_master`, and a `nodes` map (peer
map from the approval, plus a self-entry the master may not have folded
yet). `cluster.json` exists so `rqlite_setup.render_env_file()` can
render the rqlited peer list before this node is part of rqlite; it is
read at every boot, not a runtime state projection. Idempotent —
merges into any existing file.

### `install_peer_pubkeys`

Appends every peer's SSH pubkey (from the approval) to this node's
`/root/.ssh/authorized_keys`. Idempotent — de-duplicates.

### `prescan_peer_hostkeys`

`ssh-keyscan -H` each peer IP into `/root/.ssh/known_hosts`, then
`sort -u`. Pre-trusts hostkeys so `virsh migrate` over qemu+ssh works
on first connect. No-op when no peer IPs. Idempotent.

### `provision_storage_n1`

`tier_storage.setup_n1(write_rqlite=False)` — rqlite isn't up yet.
Ensures this node's LVM thinpool, the local SeaweedFS volume LV
(`bedrock-weed-volume`, no DRBD), and the cluster-singleton directory at
`/var/lib/bedrock/cluster`. The singleton is a plain dir on the root FS
here; it joins the master's `cluster` DRBD as Secondary in
`cluster_tier_join_peer` once the master has promoted (see
[`cluster_tier_promote_master`](cluster_tier_promote_master.md)).

### `pre_extract_mgmt`

Extracts the staged `/var/lib/bedrock-install/mgmt.tar.gz` into
`/opt/bedrock` so bedrock-d can import `mgmt.app` + `mgmt.orchestrator`
at startup; without it bedrock-d crash-loops. Idempotent (tar
overwrites). No-op if the tarball isn't staged.

### `start_bedrock_d`

`systemctl daemon-reload` + `reset-failed` + `enable --now bedrock-d`,
then sets the mesh sysctls `rp_filter=2` (loose, for the mesh's
asymmetric multi-NIC paths) and `ip_forward=1`. The orchestrator's
`rqlite_subscriber` task starts here and begins watching rqlite once
rqlited comes up next.

### `wait_master_reachable`

`ping` the master's loopback over the mesh, 30 tries × 0.5 s (15 s),
then fail loud rather than let rqlited's `-join` hang. No-op if no
`master_loopback` is known. Depends on bedrock-net's
`ensure_routing_sysctls()` (`arp_ignore`, `arp_announce`, `rp_filter`)
that make the multi-NIC `169.254.0.0/16` mesh route correctly.

### `render_rqlited_env`

`rqlite_setup.render_env_file()` writes `/etc/bedrock/rqlited.env` from
`cluster.json` + `state.json`, including `-join <master_loopback>:4001`
so rqlited bootstraps as a follower. Idempotent.

### `start_rqlited_joiner`

`reset-failed` + `enable` + `restart bedrock-rqlited`. Polls
`https://127.0.0.1:4001/status` (mTLS, via `node.crt` / `node.key.pem` /
`ca.crt`) until this node's Raft state is `Follower` or `Leader` — i.e.
a voter in the group — then returns. Fails loud on a 30 s timeout. The
`rqlite_subscriber` task takes over once a leader is visible.

### `install_dashboard`

`dashboard_install.install_dashboard(repo, with_metrics=False)` —
fetches `mgmt.tar.gz` and extracts it into `/opt/bedrock/mgmt`
(including the Svelte UI build) so this node serves the dashboard on its
own HTTPS :8443. Idempotent.

### `seaweedfs_install`

`seaweedfs.ensure_install()` — confirms `/usr/local/bin/weed` is
present. Idempotent.

### `seaweedfs_configs`

`seaweedfs` renders the env + master/filer/s3 configs scoped to this
node. Idempotent.

### `seaweedfs_start_local`

`seaweedfs.promote_to_master_volume_host()` — starts weed-volume +
weed-s3 (every node), and weed-master only when this node is in the
Raft-3 master set (lowest-octet rule, or the
`seaweed_master_membership` table once populated). The filer stays a
singleton on `.254`, owned by `cluster_arbiter`. Idempotent.

### `fuse_mount`

`seaweedfs.ensure_iso_library_mount()` installs a systemd unit that
FUSE-mounts `/mnt/bedrock` from the filer at `.254:8888` (the cluster
VIP, so the target doesn't change when the arbiter host flips).
Best-effort — a mount failure logs a warning and the saga continues,
since the filer may still be promoting; the unit retries in the
background.

### `cluster_tier_join_peer`

Joins the cluster-singleton DRBD so the initial sync carries the
master's filer leveldb3 + arbiter rqlite data over. Reads rqlite via
`cluster_state.load_cluster()`:

- **N=1** (fewer than 2 nodes recorded): no-op — the singleton stays
  local until a second node triggers the master's promote.
- **N≥2**: polls up to 120 s for `tiers.cluster.mode == "drbd"` (set by
  the master's [`cluster_tier_promote_master`](cluster_tier_promote_master.md)),
  then builds the peer list (master + recorded peers + self) and caps it
  with `tier_storage.cap_singleton_peers()` — the singleton DRBD is at
  most `min(3, N)`-way (lowest-octet nodes). If this node isn't in the
  capped set (a 4th+ node), it logs a skip and returns; otherwise it
  calls `tier_storage.transition_to_n2_peer()` to allocate the peer LV
  pair, write `/etc/drbd.d/cluster.res`, and `drbdadm up` as Secondary.
  The initial sync then runs in the background.

This mirrors the standalone
[`cluster_tier_join_peer`](cluster_tier_join_peer.md) saga; the
join-as-secondary logic lives once in `transition_to_n2_peer`.
Idempotent — `transition_to_n2_peer` checks for existing LVs/config
before creating, and `drbdadm up` on an up resource is a no-op.

### `activate_node`

Flips this node's rqlite `nodes.state` from `joining` (set by the master
at approval, keeping the joiner out of the master's election denominator
during the join) to `active` via `state.node_set_active(node_name)`.
This write commits because `start_rqlited_joiner` already made the node
a Raft voter. Idempotent — the UPDATE to `active` is a no-op once the
node is already active.
