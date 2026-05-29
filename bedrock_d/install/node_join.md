# bedrock_d/install/node_join.py

The `NodeJoin` saga — the joiner-side flow behind `bedrock join [<node-ip>]`. It
takes a freshly-installed node and folds it into an existing cluster: derives an
identity, runs the encrypted approval handshake with the master, installs TLS
material, provisions local storage, starts `bedrock-d` and `bedrock-rqlited` as a
Raft voter, brings up local SeaweedFS, optionally joins the cluster-singleton
DRBD replica set, and finally flips its own `nodes.state` to `active`. It is a
sibling of `cluster_init.py` and shares the same crash-resumable saga model. The
CLI command `bedrock join` calls `run_node_join(...)`. Steps delegate the heavy
lifting to helpers under `installer/lib/` (reachable via a `sys.path` shim).

## Functions / Classes

### `class NodeJoin` — `@saga("node_join")`
The ordered, idempotent step list for the joiner. The executor runs each step
once and records progress; a re-run resumes from the first not-done step. Every
step body opens with its own idempotency check and writes no rqlite state before
`start_rqlited_joiner` (the joiner is not a Raft voter until then).
- **ctx inputs** (from `run_node_join` / `cmd_join`): `witness` (IP/hostname the
  CLI dialled to fetch cluster info — any current cluster node, not the Echo
  witness host), `cluster_info` (the master's `/cluster-info` reply:
  `cluster_uuid`, `cluster_name`, `mgmt_url`, `nodes`), `repo` (install-repo URL
  for binary downloads).
- **ctx outputs** (filled as steps run): `mgmt_ip`, `node_name`,
  `bedrock_pubkey` (hex), `mgmt_url`, `request_id`, `approval` (dict),
  `loopback_ip`, `master_loopback`, `cluster_uuid`.

Steps, in order:

| Step | Purpose | Key side effects |
|------|---------|------------------|
| `prepare_dirs` | mkdir `/mnt/bedrock`, `/opt/bedrock`, `/root/.ssh` (0700) | dirs |
| `detect_mgmt_ip` | Pick joiner LAN IP (prefer br0, else any UP non-link-local NIC) | sets `ctx["mgmt_ip"]`; raises if none |
| `derive_identity` | Generate/read Ed25519 key, derive `node_name` | `peer_auth.pubkey_hex()`; sets `node_name`, `bedrock_pubkey` |
| `install_exporters` | Install node + vm exporters | `exporters.install(repo)` |
| `request_join_approval` | ECDH handshake + approval poll + TLS install | POST `/api/join/request`, poll status, write `/etc/bedrock/cluster.key` (0600) + CA-signed node cert; sets `ctx["approval"]` |
| `write_state_json` | Commit identity + `role=compute` to state.json | `state.save()` (atomic) |
| `write_bootstrap_cluster_json` | Local `cluster.json` (incl. SELF) for rqlite env render | writes `/etc/bedrock/cluster.json` |
| `install_peer_pubkeys` | Add peer SSH pubkeys | appends `/root/.ssh/authorized_keys` (de-duped) |
| `prescan_peer_hostkeys` | `ssh-keyscan` peers for `virsh migrate` | appends/sorts `/root/.ssh/known_hosts` |
| `provision_storage_n1` | LVM thinpool + local tier LVs | `tier_storage.setup_n1(write_rqlite=False)` |
| `pre_extract_mgmt` | Untar `mgmt.tar.gz` → `/opt/bedrock` | `tar xzf` |
| `start_bedrock_d` | Enable+start `bedrock-d`; set rp_filter/forwarding sysctls | `systemctl enable --now bedrock-d`; `sysctl` |
| `wait_master_reachable` | Bounded ping of master loopback over mesh | raises after 15 s if unreachable |
| `render_rqlited_env` | Write `/etc/bedrock/rqlited.env` | `rqlite_setup.render_env_file()` |
| `start_rqlited_joiner` | Start `bedrock-rqlited` with `-join`; wait until Raft voter | `systemctl restart bedrock-rqlited`; polls mTLS `/status` |
| `install_dashboard` | Serve dashboard on this node | `dashboard_install.install_dashboard(repo, with_metrics=False)` |
| `seaweedfs_install` | Ensure `/usr/local/bin/weed` present | `seaweedfs.ensure_install()` |
| `seaweedfs_configs` | Render seaweed env + master/filer/s3 configs | `seaweedfs.write_*` |
| `seaweedfs_start_local` | Start weed-master (if in Raft-3 set), volume, s3 | `seaweedfs.promote_to_master_volume_host()` |
| `fuse_mount` | FUSE-mount filer at `/mnt/bedrock` via `.254:8888` | `seaweedfs.ensure_iso_library_mount()`; failure non-fatal |
| `cluster_tier_join_peer` | Join cluster-singleton DRBD secondary (N>=2 only) | polls rqlite for tier `mode=drbd`, `tier_storage.transition_to_n2_peer` |
| `activate_node` | Flip own `nodes.state` joining→active | `state.node_set_active` via `RqliteClient` |

### `run_node_join(*, witness, cluster_info, repo) -> None`
Entry point for `bedrock join`. Submits or resumes the `node_join` op and raises
on failure.
- **In:** `witness` (the node IP/hostname the CLI fetched cluster info from),
  `cluster_info` (dict from the master's `/cluster-info`), `repo` (install-repo
  URL).
- **Out:** `None`. Side effects: ensures `/var/lib/bedrock/` exists; opens a
  `FileSagaBackend` at `/var/lib/bedrock/init-progress.json`; submits a fresh
  `node_join` op or resumes/retries an existing non-completed one for this node;
  raises `RuntimeError` naming the failing step + error if the saga does not reach
  `COMPLETED`.

### Private helpers
- `_local_node_name() -> str` — `socket.gethostname()`, falling back to `"joiner"`.
- `_enrich_params_from_state(params) -> None` — mutates `params` in place, adding
  durable identity (`cluster_uuid`, `node_name`, `loopback_ip`, `mgmt_ip` from
  state.json; `bedrock_pubkey` from `peer_auth.pubkey_hex()`) so a resumed saga
  whose earlier steps are skipped still finds the ctx values they would have set.
- `_update_op_params(backend, op_id, new_params) -> None` — rewrites a stored op's
  `params` + `updated_at` via the backend's `_write`.

## How it works

The saga is the joiner half of a two-party handshake; the master half lives in the
mgmt-side join handler and the operator's Approve click. The end state is a fully
participating node: a Raft voter in rqlite, running `bedrock-d`, serving the
dashboard, hosting SeaweedFS volume + s3, and (on N>=2, if it lands in the
lowest-octet replica set) carrying a cluster-singleton DRBD secondary.

**Resume model.** `run_node_join` scans `init-progress.json` for an existing
`node_join` op targeting this node whose `state != "completed"`. If found, it
refreshes that op's params and either `retry`s (if `failed`) or `execute_one`s
(if in-flight); otherwise it `submit`s a new op. Before running, params are passed
through `_enrich_params_from_state` so identity survives a crash even when the
step that originally produced it (`derive_identity`) is now skipped as done —
otherwise a later step such as `request_join_approval` would KeyError on
`bedrock_pubkey`.

**The rqlite-write boundary.** No step touches rqlite until the node is a voter:

```
  prepare_dirs … wait_master_reachable        (local + handshake only)
  render_rqlited_env
  start_rqlited_joiner   ───────►  node becomes a Raft Follower/Leader (voter)
  install_dashboard … cluster_tier_join_peer   (rqlite reads now safe)
  activate_node          ───────►  first rqlite WRITE: nodes.state = active
```

`start_rqlited_joiner` restarts `bedrock-rqlited` (started with `-join` at the
master) and polls the local mTLS `https://127.0.0.1:4001/status` up to 30 s,
returning once `store.raft.state` is `Leader` or `Follower` — either means "voter
in the Raft group". It raises with the last observed raft state on timeout.

**Approval handshake.** `request_join_approval` generates an ephemeral ECDH
keypair, computes a fingerprint of `bedrock_pubkey` (the string the operator
clicks Approve on at the master's dashboard), POSTs the request, polls until
approval, and decrypts the sealed reply to recover `cluster.key`. The approval
also carries the peer list, peer pubkeys/IPs, the allocated `loopback_ip`,
`master_loopback_ip`, `mgmt_master`, and a CA-signed node cert + CA cert. When the
cert pair is present it is installed (with the on-disk Ed25519 seed) so rqlited can
serve mTLS; when both are empty the step logs a warning and lets rqlited fall back
to plain HTTP.

**Storage join.** `cluster_tier_join_peer` only does work at N>=2. It polls rqlite
(via `cluster_state.load_cluster`, not cluster.json) for the master flipping the
`cluster` tier `mode` to `drbd`, up to 120 s, then builds a peer set (master +
recorded peers + self), caps it to the lowest-octet `SINGLETON_MAX_REPLICAS`-way
replica set, and — only if this node survives the cap — calls
`tier_storage.transition_to_n2_peer` to join the DRBD secondary so the initial
sync carries the master's filer leveldb + arbiter rqlite data over. At N=1 it is a
no-op; if this node is not in the capped set it skips the singleton join.

**Sequencing guards.** `pre_extract_mgmt` must precede `start_bedrock_d` so the
daemon can import mgmt at startup instead of crash-looping. `start_bedrock_d`
precedes the rqlite steps because `bedrock-rqlited` has `Requires=bedrock-d`.
`wait_master_reachable` bounds the mesh-route wait to 15 s so rqlited's `-join`
never silently hangs on an unreachable loopback. `fuse_mount` tolerates a
not-yet-ready filer (logs and lets auto-mount retry).

## Why

`activate_node` runs last and is the saga's only rqlite write: the master
registers the joiner as `joining` at approval time, keeping it out of the master's
election denominator during the join; the node only counts as an active member
once its own rqlited is a committed voter, so the activating write can succeed.
