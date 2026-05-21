# Saga: `node_join`

**Module:** `bedrock_d/install/node_join.py`  
**Class:** `NodeJoin`  
**Entry:** `run_node_join(witness, cluster_info, repo)`

## Purpose

Add a new node to an existing Bedrock cluster. Runs on the joining
node end-to-end: discovers the master, asks for operator approval,
provisions local storage, starts bedrock-d, joins rqlite as a voter,
joins SeaweedFS as a volume server, and (at N=2) joins the
cluster-tier DRBD as Secondary.

## Trigger

`bedrock join [--witness <master-host>]` CLI. The CLI first does
mDNS discovery to find a reachable master, fetches its cluster info,
displays this node's fingerprint for operator-approval, then calls
`run_node_join()`.

Same `FileSagaBackend` at `/var/lib/bedrock/init-progress.json` as
[`cluster_init`](cluster_init.md) — rqlite isn't this node's yet.

## Inputs (`ctx` keys set by the entry point)

| key | type | meaning |
|-----|------|---------|
| `witness` | str | Master host the CLI dialled to fetch cluster info |
| `cluster_info` | dict | What the master returned at `/api/cluster` — cluster_uuid, mgmt_url, existing nodes |
| `repo` | str | Install repo URL for fetching observability binaries |

## Outputs (`ctx` keys filled by the saga's own steps + enrichment)

| key | filled by | meaning |
|-----|-----------|---------|
| `node_name` | `derive_identity` (also seeded by `_enrich_params_from_state`) | This node's name |
| `bedrock_pubkey` | `derive_identity` + `_enrich_params_from_state` | Ed25519 inter-node identity pubkey |
| `mgmt_ip` | `detect_mgmt_ip` | This node's primary LAN IP |
| `loopback_ip` | `request_join_approval` | Allocated by the master in the approval response |
| `cluster_key` | `request_join_approval` | Decrypted from the master's sealed reply |

## Step overview

| # | Step | What it does |
|---|------|--------------|
| 1 | [`prepare_dirs`](#prepare_dirs) | Create `/etc/bedrock`, `/var/lib/bedrock`, `/opt/bedrock` |
| 2 | [`detect_mgmt_ip`](#detect_mgmt_ip) | Pick this node's primary LAN IP |
| 3 | [`derive_identity`](#derive_identity) | Generate Ed25519 inter-node key + derive node_name |
| 4 | [`install_exporters`](#install_exporters) | node_exporter + vm_exporter |
| 5 | [`request_join_approval`](#request_join_approval) | ECDH handshake + POST `/api/join/request`; block on operator approval |
| 6 | [`write_state_json`](#write_state_json) | Persist node_name, cluster_uuid, loopback_ip |
| 7 | [`write_bootstrap_cluster_json`](#write_bootstrap_cluster_json) | Minimal cluster.json |
| 8 | [`install_peer_pubkeys`](#install_peer_pubkeys) | Fetch peer SSH pubkeys from the master |
| 9 | [`prescan_peer_hostkeys`](#prescan_peer_hostkeys) | Populate `/root/.ssh/known_hosts` |
| 10 | [`provision_storage_n1`](#provision_storage_n1) | `tier_storage.setup_n1()` |
| 11 | [`pre_extract_mgmt`](#pre_extract_mgmt) | Copy mgmt code into `/opt/bedrock/mgmt/` |
| 12 | [`start_bedrock_d`](#start_bedrock_d) | Enable + start the unified daemon |
| 13 | [`wait_master_reachable`](#wait_master_reachable) | Poll until the master's loopback responds |
| 14 | [`render_rqlited_env`](#render_rqlited_env) | rqlited config with `-join master_loopback:4001` |
| 15 | [`start_rqlited_joiner`](#start_rqlited_joiner) | Start rqlited as a Raft follower |
| 16 | [`install_dashboard`](#install_dashboard) | Stage the dashboard build under `/opt/bedrock/mgmt/ui/` |
| 17 | [`seaweedfs_install`](#seaweedfs_install) | Confirm `/usr/local/bin/weed` presence |
| 18 | [`seaweedfs_configs`](#seaweedfs_configs) | Render seaweed env + per-node configs |
| 19 | [`seaweedfs_start_local`](#seaweedfs_start_local) | Start weed-master/volume/s3 on this node |
| 20 | [`fuse_mount`](#fuse_mount) | Mount `/mnt/bedrock` from `.254:8888` |
| 21 | [`cluster_tier_join_peer`](#cluster_tier_join_peer) | Wait for master's [`cluster_tier_promote_master`](cluster_tier_promote_master.md) to finish, then join DRBD as Secondary |

## Revert

The inverse saga is [`node_leave`](node_leave.md), run on the **master**
with `--target <node-to-remove>`. node_leave unregisters the node
from rqlite + propagates the new cluster.json to remaining peers +
stops services on the target node + verifies the membership drop.

After `node_leave` runs cleanly, the now-removed node can run
`bedrock node reset` to wipe its local state and start over with
either `bedrock init` or `bedrock join` of a different cluster.

## Idempotency / resume

Re-running `bedrock join` after a crash picks up the existing
`init-progress.json` and resumes from the first not-`done` step.
`_enrich_params_from_state` seeds `node_name` + `bedrock_pubkey`
from durable sources (state.json + the on-disk Ed25519 key) so a
resumed run after `derive_identity` was already done still has
those values in ctx — see
[`lesson_join_saga_resume_keyerrors`](../../../.claude/projects/-home-tommy-projects/memory/lesson_join_saga_resume_keyerrors.md).

`request_join_approval` is the only blocking step — it polls the
master at 2 s intervals up to 10 min and gives up if no operator
approves. The saga can simply be re-run after approval.

## Step details

### `prepare_dirs`

Same as cluster_init — create `/etc/bedrock`, `/var/lib/bedrock`,
`/opt/bedrock`.

### `detect_mgmt_ip`

Scans `ip -4 addr` for UP NICs and picks the first non-link-local
IPv4. This is the node's `mgmt_ip` — used by peers to dial mgmt
HTTPS on 8443.

### `derive_identity`

Calls `peer_auth.pubkey_hex()` (creates the Ed25519 keypair if it
doesn't exist; reads it on subsequent calls — idempotent). Sets
`ctx["node_name"]` to the system hostname and `ctx["bedrock_pubkey"]`
to the hex-encoded public key.

### `install_exporters`

Fetches node_exporter + vm_exporter from the install repo and
writes the systemd units. Same shape as cluster_init's variant.

### `request_join_approval`

Generates an ephemeral X25519 keypair, POSTs `/api/join/request` to
the master with this node's name, fingerprint, hardware summary,
and the ephemeral public key. The master records a `pending` row
in `join_requests`; an operator must approve via the dashboard
(`/api/join/approve`).

When the master approves, the response is sealed via ECDH (master's
ephemeral + this node's ephemeral, HKDF-salted with the request_id)
and contains the cluster_key + this node's allocated loopback_ip.
The step decrypts, writes `cluster.key` to `/etc/bedrock/`, and
stores `loopback_ip` in ctx.

Times out after 10 min if no approval. Re-running the saga issues
a fresh handshake (new request_id).

### `write_state_json`

Persists `node_name`, `cluster_uuid`, `loopback_ip` (and the
hardware summary) into `/etc/bedrock/state.json` via the atomic
write helper.

### `write_bootstrap_cluster_json`

Minimal cluster.json that contains enough for netd to start before
this node joins rqlite — it gets overwritten by the
rqlite_subscriber projection as soon as `start_rqlited_joiner`
completes.

### `install_peer_pubkeys`

Fetches the SSH pubkeys of every existing cluster node from the
master and appends them to this node's `/root/.ssh/authorized_keys`.
Idempotent: skips entries already present.

### `prescan_peer_hostkeys`

`ssh-keyscan` each peer's SSH hostkey into
`/root/.ssh/known_hosts`. Needed so paramiko (used by mgmt's SSH
helpers) doesn't prompt on first connect.

### `provision_storage_n1`

`tier_storage.setup_n1()`. Creates this node's local thinpool +
three tier LVs + XFS + mounts. At N=1 every tier is local; the
critical tier flips to DRBD when the master's
[`cluster_tier_promote_master`](cluster_tier_promote_master.md) runs.

### `pre_extract_mgmt`

Copies the mgmt code from `/usr/local/lib/bedrock/mgmt/` (or the
ISO) into `/opt/bedrock/mgmt/` so the bedrock-d daemon can find it.
Done before `start_bedrock_d` because bedrock-d imports from
`/opt/bedrock/mgmt/`.

### `start_bedrock_d`

`systemctl enable --now bedrock-d`. The orchestrator's
`rqlite_subscriber` task starts; it'll begin watching rqlite as
soon as rqlited comes up in the next step.

### `wait_master_reachable`

Polls `ping master_loopback` until reachable, with a 15 s timeout.
This is where the mesh-loopback routing has to work — see
[`lesson_mesh_loopback_asymmetric_routes`](../../../.claude/projects/-home-tommy-projects/memory/lesson_mesh_loopback_asymmetric_routes.md)
and [`lesson_mesh_rp_filter`](../../../.claude/projects/-home-tommy-projects/memory/lesson_mesh_rp_filter.md)
for the sysctls that make this work on a multi-NIC mesh.

### `render_rqlited_env`

Writes `/etc/bedrock/rqlited.env` with the per-node config and
`-join <master_loopback>:4001` so rqlited bootstraps as a follower
joining the existing Raft cluster.

### `start_rqlited_joiner`

`systemctl enable --now bedrock-rqlited`. Polls until rqlited
reports it has a leader (could be itself or the master) before
returning. The rqlite_subscriber kicks in once a leader is visible.

### `install_dashboard`

Copies the Svelte build artifacts from the ISO into
`/opt/bedrock/mgmt/ui/build/` so the local mgmt-HTTPS-8443 serves
the dashboard. Same UI as the master.

### `seaweedfs_install`

`/usr/local/bin/weed` presence check.

### `seaweedfs_configs`

Renders the seaweed env + master/filer/s3 configs scoped to this
node.

### `seaweedfs_start_local`

Starts weed-master + weed-volume + weed-s3 on this node. Filer
stays singleton on the master.

### `fuse_mount`

Mounts `/mnt/bedrock` from `.254:8888` (the cluster-singleton
filer). Best-effort — mount failure logs a warning but doesn't
fail the saga, because the filer may still be promoting.

### `cluster_tier_join_peer`

Inline (in this saga, not the standalone
[`cluster_tier_join_peer`](cluster_tier_join_peer.md) saga) version
of the peer-side cluster-tier DRBD join. Polls cluster.json for
`tiers.critical.mode == "drbd"` (up to 120 s — waits for the
orchestrator's `cluster_tier_watcher` on the master to fire and
finish its promote), then calls
`tier_storage.transition_to_n2_peer()` to allocate the peer LV pair,
write the .res file, and `drbdadm up` as Secondary. The initial
sync then runs in the background.

If the cluster is still N=1 by the time this step runs (no other
nodes recorded), it no-ops and returns — the eventual second
joiner will trigger the master's promote and the **first** joiner
(this node) will join via the orchestrator's reconcile path.
