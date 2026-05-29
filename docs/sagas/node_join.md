# Saga: `node_join`

**Module:** `bedrock_d/install/node_join.py`  
**Class:** `NodeJoin`  
**Entry:** `run_node_join(witness, cluster_info, repo)`

## Purpose

Add a new node to an existing Bedrock cluster. Runs on the joining
node end-to-end: discovers the master, asks for operator approval,
provisions local storage, starts bedrock-d, joins rqlite as a voter,
joins SeaweedFS as a volume server, (at N≥2) joins the cluster-singleton
DRBD (`cluster`) as Secondary, and finally flips its own rqlite node
state from `joining` to `active`.

## Trigger

`bedrock join [<node-ip>]` CLI. `<node-ip>` is any current cluster
member (not necessarily the master). Omit it to auto-discover via
mDNS on the LAN. The CLI fetches cluster info from the chosen node,
displays this node's fingerprint for operator-approval on the
cluster dashboard, then calls `run_node_join()`.

The `witness` keyword arg below is a legacy parameter name — it
holds the IP/hostname the CLI dialled (master or any other cluster
node), not the cluster witness (Echo) host. Renaming it is a
follow-up cleanup that doesn't affect behaviour.

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
| 11 | [`pre_extract_mgmt`](#pre_extract_mgmt) | Extract staged `mgmt.tar.gz` into `/opt/bedrock` |
| 12 | [`start_bedrock_d`](#start_bedrock_d) | Enable + start the unified daemon |
| 13 | [`wait_master_reachable`](#wait_master_reachable) | Poll until the master's loopback responds |
| 14 | [`render_rqlited_env`](#render_rqlited_env) | rqlited config with `-join master_loopback:4001` |
| 15 | [`start_rqlited_joiner`](#start_rqlited_joiner) | Start rqlited as a Raft follower |
| 16 | [`install_dashboard`](#install_dashboard) | Stage the dashboard build under `/opt/bedrock/mgmt/ui/` |
| 17 | [`seaweedfs_install`](#seaweedfs_install) | Confirm `/usr/local/bin/weed` presence |
| 18 | [`seaweedfs_configs`](#seaweedfs_configs) | Render seaweed env + per-node configs |
| 19 | [`seaweedfs_start_local`](#seaweedfs_start_local) | Start weed-master/volume/s3 on this node |
| 20 | [`fuse_mount`](#fuse_mount) | Mount `/mnt/bedrock` from `.254:8888` |
| 21 | [`cluster_tier_join_peer`](#cluster_tier_join_peer) | Wait for master's [`cluster_tier_promote_master`](cluster_tier_promote_master.md) to finish, then join the `cluster` DRBD as Secondary |
| 22 | [`activate_node`](#activate_node) | Flip this node's rqlite `nodes.state` from `joining` to `active` |

## Revert

The inverse saga is [`node_leave`](node_leave.md), run as
`bedrock node leave <node-to-remove>` on any surviving node (normally
the master, never the target itself). node_leave unregisters the node
from rqlite (peers pick up the change on the next revision bump),
removes its rqlite Raft voter slot, stops services on the target node
(best-effort), and verifies the membership drop.

After `node_leave` runs cleanly, the now-removed node can run
`bedrock node reset` to wipe its local state and start over with
either `bedrock init` or `bedrock join` of a different cluster.

## Idempotency / resume

Re-running `bedrock join` after a crash picks up the existing
`init-progress.json` and resumes from the first not-`done` step.
`_enrich_params_from_state` seeds `node_name` + `bedrock_pubkey`
from durable sources (state.json + the on-disk Ed25519 key) so a
resumed run after `derive_identity` was already done still has
those values in ctx.

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

Minimal `/etc/bedrock/cluster.json` scaffold — just enough for
`rqlite_setup.render_env_file()` to read before this node joins rqlite.
Once `start_rqlited_joiner` completes, rqlite is the authoritative
store and every consumer reads it via `cluster_state.load_cluster()`;
there is no steady-state `cluster.json` projection (only `state.json`
is re-projected per revision).

### `install_peer_pubkeys`

Fetches the SSH pubkeys of every existing cluster node from the
master and appends them to this node's `/root/.ssh/authorized_keys`.
Idempotent: skips entries already present.

### `prescan_peer_hostkeys`

`ssh-keyscan` each peer's SSH hostkey into
`/root/.ssh/known_hosts`. Needed so paramiko (used by mgmt's SSH
helpers) doesn't prompt on first connect.

### `provision_storage_n1`

`tier_storage.setup_n1()`. Creates this node's LVM thinpool, the
SeaweedFS volume LV (`bedrock-weed-volume`, no DRBD), and the
cluster-singleton directory at `/var/lib/bedrock/cluster`. The
singleton starts as a plain dir on the root FS; it joins the master's
`cluster` DRBD as a Secondary in the `cluster_tier_join_peer` step once
the master has promoted (see
[`cluster_tier_promote_master`](cluster_tier_promote_master.md)).

### `pre_extract_mgmt`

Extracts the staged `/var/lib/bedrock-install/mgmt.tar.gz` into
`/opt/bedrock` so the bedrock-d daemon can import `mgmt.app` +
`mgmt.orchestrator` at startup. Must run before `start_bedrock_d` —
without it bedrock-d crash-loops waiting for the code. Idempotent (tar
overwrites). No-op if the tarball isn't staged.

### `start_bedrock_d`

`systemctl enable --now bedrock-d`. The orchestrator's
`rqlite_subscriber` task starts; it'll begin watching rqlite as
soon as rqlited comes up in the next step.

### `wait_master_reachable`

Polls `ping master_loopback` until reachable, with a 15 s timeout.
Depends on the mesh routing being up — `ensure_routing_sysctls()`
in `installer/lib/netd.py` applies the multi-NIC sysctls
(`arp_ignore`, `arp_announce`, `rp_filter`) that make this work
when each NIC carries a `169.254.0.0/16` link-local.

### `render_rqlited_env`

Writes `/etc/bedrock/rqlited.env` with the per-node config and
`-join <master_loopback>:4001` so rqlited bootstraps as a follower
joining the existing Raft cluster.

### `start_rqlited_joiner`

`systemctl enable --now bedrock-rqlited` (its env carries
`-join <master_loopback>:4001`). Polls `https://127.0.0.1:4001/status`
(mTLS) until this node's own Raft state is `Follower` or `Leader` —
i.e. it's a voter in the group — then returns. Fails loud on a 30 s
timeout. The `rqlite_subscriber` task kicks in once a leader is visible.

### `install_dashboard`

Calls `dashboard_install.install_dashboard(repo, with_metrics=False)`:
fetches `mgmt.tar.gz` from the install repo and extracts it into
`/opt/bedrock/mgmt` (including the Svelte UI build) so this node serves
the same dashboard as the master on its own HTTPS :8443. Idempotent.

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
of the peer-side cluster-singleton DRBD join. Polls **rqlite** (via
`cluster_state.load_cluster()` — explicitly NOT the removed
`cluster.json`) for `tiers.cluster.mode == "drbd"`, up to 120 s, while
the orchestrator's `cluster_tier_watcher` on the master fires and
finishes its promote. It then builds the peer list (master + recorded
peers + self) and caps it via `tier_storage.cap_singleton_peers()` —
the cluster-singleton DRBD is at most `min(3, N)`-way (lowest-octet
nodes). If this node isn't in the capped set (a 4th+ node) it logs a
skip and returns; otherwise it calls
`tier_storage.transition_to_n2_peer()` to allocate the peer LV pair,
write `/etc/drbd.d/cluster.res`, and `drbdadm up` as Secondary. The
initial sync then runs in the background.

If the cluster is still N=1 by the time this step runs (fewer than 2
nodes recorded), it no-ops and returns — the eventual second joiner
triggers the master's promote, and this first joiner picks up the
singleton via the orchestrator's reconcile path.

### `activate_node`

Final step: flips this node's rqlite `nodes.state` from `joining`
(set by the master at approval, so the joiner stayed out of the
master's election denominator during the join) to `active`. This
write commits because `start_rqlited_joiner` already made the node a
Raft voter. Calls `state.node_set_active(node_name)` — idempotent
(re-running rewrites the same `active` value).
