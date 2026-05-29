# mgmt/app.py

The FastAPI application that backs the Bedrock dashboard and the cluster
management/orchestration API. It is imported and served by `bedrock-d`'s mgmt
thread (`serve_main()` is the entrypoint that thread calls). It owns: the
operator dashboard (Svelte build + noVNC), the LAN mgmt REST API, the
loopback CLI endpoint, the WebSocket state hub, and the request paths for VM
lifecycle, HA-level conversion, import/export, backups, observability-backend
promotion, operator accounts, and the join handshake. Operators reach it on
8443 HTTPS; the local `bedrock` CLI reaches it on `127.0.0.1:8001`. Cluster-wide
reads come from the local rqlite replica; writes go through `lib.bedrock_state`
(rqlite) and per-node actions go out over a pooled SSH mesh.

## Functions / Classes

### `serve_main()`
Bind uvicorn listeners and block until SIGTERM; the `bedrock-d` mgmt thread calls this.
- **In:** nothing.
- **Out:** never returns normally. Starts a daemon thread serving `app` on
  `127.0.0.1:8001` (HTTP, CLI/intra-process), then on the main thread serves
  either `0.0.0.0:8443` HTTPS (when `/etc/bedrock/tls/{cert,key}.pem` exist) or
  `0.0.0.0:8444` HTTP bootstrap (cert-less node, so a joiner can fetch
  `/api/cluster`). Both listeners share the one `app` object.

### `app`
The module-level `FastAPI` instance. All routes, the auth middleware, and the
extracted route modules (`routes_obs`, `routes_operations`, `routes_support`,
`routes_console`, `routes_iso`) register onto it. Static mounts: `/novnc` and
`/_app`; a `/{path:path}` SPA fallback serves the Svelte build.

### `require_peer(request) -> str`  (FastAPI dependency)
Accepts a request signed by a known cluster node.
- **In:** `request` — the incoming request; reads `Authorization` header + body.
- **Out:** verified node name. Raises `HTTPException(401)` on any failure.
  Looks up the caller's Ed25519 pubkey from `load_cluster()` and verifies via
  `peer_auth.verify`.

### `require_operator(request) -> str`  (FastAPI dependency)
Accepts a valid operator Bearer session token.
- **In:** `request` — reads client host + `Authorization: Bearer <token>`.
- **Out:** username string. Loopback (`127.0.0.1`/`::1`) is exempt and returns
  `"local"`. Raises `HTTPException(401)` otherwise.

### `require_operator_or_peer(request) -> str`  (FastAPI dependency)
Accepts EITHER an operator Bearer token OR a peer Ed25519 signature.
- **In:** `request`.
- **Out:** `"op:<user>"` or `"peer:<node>"` so the handler knows the call site.
  Raises `HTTPException(401)`.

### `load_cluster() -> dict`
Cluster-wide state snapshot.
- **In:** nothing.
- **Out:** dict (`cluster_name`, `nodes`, `vms`, `operators`, `obs_backends`,
  `backup_targets`, `join_requests`, …) read from the local rqlite replica via
  `cluster_state.load_cluster()` at read level `none` (works without quorum). On
  failure returns `{"cluster_name": "bedrock", "nodes": {}}`.

### `save_cluster(cluster)`
- **In:** `cluster` — a state dict.
- **Out:** none. Only side effect is `write_scrape_config(cluster)`.

### `write_scrape_config(cluster)`
Regenerate the VictoriaMetrics scrape config and reload the agent.
- **In:** `cluster` — state dict; uses node `host` values.
- **Out:** writes `/opt/bedrock/scrape.yml` (node:9100 + libvirt:9177 targets);
  fires `systemctl restart --no-block bedrock-vmagent.service`. No-op if
  `/opt/bedrock` doesn't exist (not a mgmt node) or no hosts known.

### `get_nodes() -> dict`
- **Out:** `load_cluster()["nodes"]`.

### `ssh_cmd(host, cmd, timeout=10) -> str`
Run a command on `host` over the pooled SSH connection.
- **In:** `host`, `cmd`, `timeout`.
- **Out:** stdout, stripped. On `SSHException`/`EOFError`/`OSError` drops the
  pooled client and re-raises.

### `ssh_cmd_rc(host, cmd, timeout=30) -> (str, int)`
Same, but returns combined stdout+stderr and the exit code.
- **Out:** `(combined_output, exit_code)`.

### `get_node_info(name, cfg) -> dict`
One SSH round-trip that gathers a node's full status.
- **In:** `name` — node name; `cfg` — its config dict (needs `host`).
- **Out:** dict with `online`, VM lists, `drbd_raw`, load/mem/kernel/uptime,
  `thinpools`, `switches`, `mesh`, `cockpit_url`. On any error returns an
  `online: False` dict with empty fields + `error`.

### `parse_drbd_status(raw) -> dict`
Parse `drbdadm status` text into per-resource role/disk/peer/replication/done.
- **In:** `raw` — `drbdadm status` output.
- **Out:** `{resource: {role, disk, peer_role, peer_disk, replication, done}}`.

### `get_witness_status() -> dict`
- **Out:** `{"witnesses": <cluster.witnesses map>}`.

### `get_vm_disks(host, vm_name) -> list[dict]`
Enumerate a VM's block disks (cdroms excluded) from `virsh dumpxml` + DRBD status.
- **In:** `host`, `vm_name`.
- **Out:** one dict per disk (`target`, `bus`, `source`, `drbd_resource`,
  `drbd_minor`, `backing_lv`), sorted by target. `[]` if the VM doesn't exist.

### `get_vm_drbd_resource(host, vm_name) -> str`
Shim over `get_vm_disks` returning the first disk's DRBD resource (or `""`).

### `get_vm_vnc_port(host, vm_name) -> int`
- **Out:** `5900 + display` for a running VM, else `-1`.

### `build_cluster_state() -> dict`
The full dashboard snapshot — the heavy SSH fan-out.
- **In:** nothing.
- **Out:** `{nodes, vms, witness, topology}`. Fans out `get_node_info` and
  per-VM probes across thread pools; merges DRBD state, per-disk sizes, VNC URLs,
  and `load_inventory()` metadata into each VM record.

### `build_physical_topology(nodes_data) -> dict`
Roll up per-node switch + mesh-neighbour observations into a cluster view.
- **In:** `nodes_data` — the `get_node_info` results keyed by node.
- **Out:** `{switches, links, node_count, switch_count, link_count, computed_at}`,
  switches grouped by device MAC (`device_key`), node-to-node links
  canonicalised so (A,B)/(B,A) collapse. Best-effort writes a cache copy to
  `/run/bedrock/physical_topology.json`. View-only; never folded into cluster
  state.

### `_auth_middleware(request, call_next)`  (HTTP middleware)
Gate on `/api/*` and `/ws*`: allow public paths and loopback, else require an
operator Bearer token or a peer Ed25519 signature. Restores the request body
after reading it so the handler can re-read.

### `login(req: LoginReq, request)`  — `POST /api/login`
Operator login.
- **In:** `LoginReq{username, password}`.
- **Out:** `{token, exp, user}`. Per-IP leaky-bucket throttle (5 fails/min →
  429); wrong creds → 401. Verifies via `operator_auth.verify_password`, mints
  via `mint_token`, logs the login.

### `whoami(user)`  — `GET /api/whoami`
- **Out:** `{"user": <name>}`.

### `list_operators(user)` / `set_operator(req)` / `remove_operator(req)`
Operator account management (operator-authed).
- **`GET /api/operators`** → sorted usernames (hashes never exposed).
- **`POST /api/operators/set`** (`OperatorSet{username, password}`) → upsert via
  `bedrock_state.operator_set`. Min 4-char password.
- **`POST /api/operators/remove`** (`OperatorRemove{username}`) → delete via
  `bedrock_state.operator_remove`; refuses removing yourself or the last operator.

### `join_request(req: JoinRequest)`  — `POST /api/join/request`  (UNAUTH)
A joiner asks to join.
- **In:** `JoinRequest{node_name, host, bedrock_pubkey, x25519_eph_pubkey, ssh_pubkey}`.
- **Out:** `{request_id, fingerprint}`. Records the request via
  `bedrock_state.join_request` (replicated so any dashboard shows it); caches the
  joiner's SSH pubkey + host in the in-process `_PENDING_SSH_PUBKEYS` map (kept
  out of the log).

### `join_status(id)`  — `GET /api/join/status`  (UNAUTH; request_id is the secret)
Joiner polls for the operator's decision.
- **In:** `id` — request_id.
- **Out:** `{state}`; on `approved` adds the ECDH bundle
  (`master_eph_pubkey`/`ciphertext`/`nonce`), cluster membership
  (`node_map`, peer pubkeys/IPs, `mgmt_master`, the joiner's `loopback_ip`), and
  the CA + CA-signed node cert PEMs. 404 on unknown id.

### `join_pending(user)`  — `GET /api/join/pending`
- **Out:** `{pending: [...]}` — all pending join requests, for the approval popup.

### `join_approve(req: JoinApprove, user)`  — `POST /api/join/approve`
Operator approves a join; runs the whole admission sequence (see How it works).
- **In:** `JoinApprove{request_id}`.
- **Out:** `{state: "approved", loopback_ip}`. Side effects: seals `cluster.key`
  under ECDH, allocates the joiner's loopback `/32`, installs + fans out the
  joiner's SSH pubkey, signs the joiner's TLS cert with the cluster CA, writes
  `node_register`/`node_loopback`/(optionally `obs_backends_set`)/`join_resolved`
  rows, and on a 1→2 observability promote seeds + starts `bedrock-vm` on the joiner.

### `join_reject(req: JoinReject, user)`  — `POST /api/join/reject`
- **In:** `JoinReject{request_id, reason}`.
- **Out:** `{state: "rejected"}`; records `join_resolved(decision="rejected")`.

### `observability_promote(req: ObsPromote, user)`  — `POST /api/observability/backends`
Add or swap a metrics/logs backend; synchronous.
- **In:** `ObsPromote{new_node, replace, kind}`.
- **Out:** `{metrics_backends, logs_backends, seed_report}`. Flips the
  `obs_backends` snapshot first, waits ~2 s, seeds the new node's data dir
  (`observability.seed_backend`, `force` on replace), then SSHes
  `systemctl start bedrock-vm.service` on the new node.

### WebSocket `/ws` + `handle_rpc(method, params)`
Token-authed (via `?token=`) WebSocket. On connect, pushes cached `_last_state`,
then serves RPCs: `vm.start`, `vm.shutdown`, `vm.poweroff`, `vm.migrate` (each
dispatched to the blocking impl in a thread executor).

### `state_push_loop()`  (background task)
Every 3 s: `build_cluster_state()` in an executor, store into `_last_state`,
`hub.broadcast("cluster", state)`.

### `startup()`  (`@app.on_event("startup")`)
One-time (lock-guarded) startup: seed `_last_state` from `load_cluster()`, wire
the task registry to the WS hub, launch `state_push_loop`, write the scrape
config, and `orchestrator.start_all()` (sharing the same `mgmt.orchestrator`
module instance `bedrock-d` already imported).

### REST read endpoints
- **`GET /api/cluster`** → cached `_last_state`.
- **`GET /api/topology`** → cached topology rollup.
- **`GET /api/tasks`**, **`GET /api/tasks/{id}`** → task registry snapshot.
- **`GET /cluster-info`**  (public) → discovery info for `bedrock join`
  (`cluster_name`, `cluster_uuid`, node names, `mgmt_url`, `witness_host` from
  `/etc/bedrock/state.json`).
- **`GET /api/nodes`** → the nodes map.
- **`GET /api/peer-test`** → `{verified_caller}` (Ed25519 smoke test).

### Import library  (`/api/imports*`)
Stage and convert foreign disk images (VMware/Hyper-V/qcow2/OVA) into Bedrock VMs.
- **`POST /api/imports/upload`** — stream a disk image to
  `/opt/bedrock/imports/<id>/`, then synchronously `_inspect_os`. Returns meta.
- **`POST /api/imports/{id}/convert`** — fire `_run_convert` (qemu-img, or
  virt-v2v for OVA / Windows driver injection); returns immediately.
- **`POST /api/imports/{id}/create-vm`** — task-tracked `_vm_create_from_import`.
- **`GET /api/imports`**, **`GET /api/imports/{id}`**, **`DELETE /api/imports/{id}`**.

### Export library  (`/api/exports*`, `/api/vms/{vm}/export`)
`qemu-img convert` a VM's live disk to qcow2/vmdk/vhdx/raw under
`/opt/bedrock/exports/<id>/` (local, or remote via ssh+dd through a FIFO).
Endpoints: `POST /api/vms/{vm}/export`, `GET /api/exports`,
`GET /api/exports/{id}/download`, `DELETE /api/exports/{id}`.

### VM lifecycle  (saga-backed)
- **`POST /api/vms`** (`VMCreateRequest`) — validates everything sync, writes a
  `vm_create_intent` breadcrumb, then fire-and-forget runs the `vm_create` saga
  via `_run_vm_saga`; returns `{task_id, intent_revision}`.
- **`DELETE /api/vms/{vm}`** — fire-and-forget `vm_destroy` saga + inventory cleanup.
- **`POST /api/vms/{vm}/migrate`** (`MigrateRequest`) — synchronous `vm_migrate`
  saga (target defaults to the VM's backup peer).
- **`POST /api/vms/{vm}/start|stop|force-stop`** — `_vm_start` / `_vm_shutdown` /
  `_vm_poweroff` (direct `virsh` over SSH, DRBD-promote-aware on start).

### `_run_vm_saga(kind, params) -> dict`
Submit + synchronously run a `bedrock_d` VM saga on this (master) node.
- **In:** `kind` (`vm_create`/`vm_destroy`/`vm_migrate`), `params`.
- **Out:** `{op_id, state, last_step}`; raises `HTTPException(500)` if the saga
  doesn't reach `COMPLETED`.

### `_vm_create_peers(vm_type) -> (home, peers)`
Resolve the replica set: cattle → `[home]`, pet → `[home, peer]`, vipet →
`[home, peer, peer]`. Raises 400 if the cluster is too small.

### HA-level conversion  (`POST /api/vms/{vm}/ha-level`, `HaLevelRequest`)
Fire-and-forget convert between cattle/pet/vipet. Validates sync, then runs
`_vm_set_ha_level` in a task. The conversion engine lives in:
- **`_vm_set_ha_level_up(...)`** — cattle→pet/vipet (per-disk: create meta + peer
  LVs, generate `.res`, `create-md`/`up`, silent-truncation guard, then live
  `virsh blockcopy --pivot` or offline XML rewrite; full unwind on failure) and
  pet→vipet (add a third peer to each resource).
- **`_vm_set_ha_level_down(...)`** — vipet→pet (drop a peer) and pet/vipet→cattle
  (pivot each `/dev/drbdN` back to its raw LV, tear DRBD down, drop peer LVs).
- Helpers: `_count_drbd_peers`, `_gen_drbd_res`, `_write_drbd_res`,
  `_parse_drbd_res`, `_next_drbd_minor`/`_release_drbd_minor` (process-local
  minor reservation in band 1102–1189), `_lv_bytes`, `_find_vm_disk`,
  `_ensure_thinpool`, `_vm_disk_vg`.

### VM settings  (`/api/vms/{vm}/...`)
- **`GET .../settings`** → `_vm_get_settings` (vcpus, ram, disk, paths, cdrom,
  priority, cpu_shares).
- **`POST .../compute`** (`ComputeRequest`) → `_vm_set_resources` (vcpu/ram queued
  for reboot; disk grown live, DRBD-resize-aware).
- **`POST .../priority`** (`PriorityRequest`) → `_vm_set_priority` (cpu_shares live
  + mirrored to rqlite).
- **`POST .../cdrom`** (`CdromRequest`) → `_vm_set_cdrom` (eject/insert ISO).
- **`POST .../disks`** (`AttachDiskRequest`) → live `virsh attach-disk` of a new
  thin LV.

### `_vm_create_from_import(meta, req, task) -> dict`
Turn a converted import into a cattle VM (not saga-routed): firmware sniff
(BIOS/UEFI), thin-pool fit check, one LV + `qemu-img convert` per source disk,
`virt-install` (q35, matched firmware, UTC clock, Hyper-V enlightenments for
Windows), inventory + import-meta update.

### Backups  (`/api/backup*`, `/api/vms/{vm}/backup*`, `/api/backups`)
Kopia orchestration; delegates to `mgmt/backup.py` (`_import_backup_module`).
- **`POST /api/backup/targets`** (`BackupTargetSetRequest`) — propagate inline
  secrets to every node (`/etc/bedrock/backup.key`,
  `/etc/bedrock/backup-credentials/<id>.env`, mode 0600, never logged), connect
  the master, then write `BACKUP_TARGET_SET` to rqlite for peer reactors.
- **`GET /api/backup/targets`**, **`DELETE /api/backup/targets/{id}`**.
- **`GET /api/backup/credentials/status`** — per-node secret presence booleans.
- **`POST /api/vms/{vm}/backup`** / **`/restore`** — task-tracked
  `backup.run_backup` / `run_restore`.
- **`GET /api/vms/{vm}/backups`**, **`GET /api/backups`** (cluster-wide history),
  **`DELETE /api/vms/{vm}/backups/{snap}`**.
- **`POST|DELETE /api/vms/{vm}/backup-schedule`** (`BackupScheduleSetRequest`) —
  cron schedule in the log; validated via `mgmt/cron.py`.
- **`GET /api/cron/preview`** — next-N cron fire times.

### Secret-propagation helpers
`_propagate_secret` (write to every node, returns ok/failed), `_write_local_secret`,
`_write_remote_secret` (paramiko SFTP, atomic tmp+`posix_rename`),
`_render_s3_creds_env`, `_self_host`.

### SSH-pubkey + identity helpers
`_append_authorized_key` (local or remote `authorized_keys`), `_read_local_pubkey`,
`_mgmt_node_name` (node with `mgmt` in its role; first node as fallback).

### `push_log(msg, node, app, level)`
Stream a log line to dashboard WebSockets, then persist to VictoriaLogs.
- **Out:** broadcasts `"event"` over the hub (best-effort, scheduled onto the
  main loop) and blocks on the VictoriaLogs insert second.

## How it works

```
operator browser ──HTTPS 8443──┐
                                ├─► FastAPI `app` ──► _auth_middleware
bedrock CLI ─HTTP 127.0.0.1:8001┘        │
                                         ├─ reads:  load_cluster()  ──► local rqlite replica (level=none)
                                         ├─ writes: lib.bedrock_state.*  ──► rqlite (Raft)
                                         ├─ node ops: ssh_cmd / ssh_cmd_rc ──► pooled paramiko mesh
                                         └─ heavy lifecycle: _run_vm_saga ──► bedrock_d sagas
```

Auth funnels through one middleware. Every `/api/*` (and `/ws*`) request must be
public-listed, come from loopback, or carry an operator Bearer token or a peer
Ed25519 signature; the body is read once and re-injected so handlers can read it
again. The `require_*` dependencies repeat this per-route for endpoints that
need the verified identity. The browser WebSocket can't set headers, so `/ws`
takes its token as a query param. Loopback is trusted unconditionally — the
local `bedrock` CLI on 8001 sends no token, and martian/`rp_filter` rules make a
spoofed-loopback source from a real NIC unreachable.

The dashboard never waits on SSH. `state_push_loop` rebuilds the whole snapshot
every 3 s into `_last_state`; `/ws`, `/api/cluster`, and `/api/topology` all
serve that cached object instantly. `build_cluster_state` is the expensive part:
it fans `get_node_info` out across a thread pool (one big multi-section SSH
command per node), parses DRBD per resource, then probes each VM (disks, sizes,
VNC) in a second pool, and finally rolls up physical topology.

SSH is pooled. `_ssh_connect` keeps one paramiko client per host with a 20 s
keepalive and reconnects on a stale transport; `ssh_cmd`/`ssh_cmd_rc` drop the
cached client on error so the next call reconnects. This keeps the 3 s probe
loop from hammering sshd's pre-auth queue.

The join handshake is three hops, and the master only acts on operator approval:

```
joiner ── POST /api/join/request (unauth) ──► record in rqlite + cache ssh_pubkey in memory
   │                                          push popup to every dashboard
operator ── POST /api/join/approve ──► seal cluster.key under X25519-ECDH (HKDF salt=request_id)
   │                                   allocate next free loopback /32 (scan rqlite nodes)
   │                                   install + fan out joiner ssh_pubkey to every peer
   │                                   sign joiner TLS cert with cluster CA
   │                                   node_register(state='joining') + node_loopback
   │                                   (1→2 obs promote? add backend, seed, start bedrock-vm)
   │                                   join_resolved(approved, ECDH bundle + cert PEMs)
joiner ── GET /api/join/status (poll) ──► sees 'approved' + everything to finish its install
```

The joiner is registered `state='joining'` so it stays out of the election
denominator until its own saga self-activates — the master can't be tipped into
NoQuorum mid-join. The SSH-pubkey side-channel lives only in memory; a mgmt
restart between request and approval loses it (the crypto path still works, only
peer→joiner SSH waits for a key refresh).

VM lifecycle (create/destroy/migrate) runs on the master through the `bedrock_d`
sagas — the API handler validates synchronously, returns 202 + a task id, and
runs the saga in a thread; the saga owns crash-resume. HA-level conversion and
import-create-VM are the exception: they drive `virsh`/`drbdadm`/`lvcreate`
directly over SSH from this module, with explicit unwind on failure.

`_vm_set_ha_level_up` cattle→pet/vipet is the most delicate path, per disk:

```
create source meta LV ─► create data+meta LVs on each peer ─► write <res>.res everywhere
   ─► create-md --max-peers=7 + drbdadm up on all ─► primary --force on source
   ─► SILENT-TRUNCATION GUARD: assert size(/dev/drbdN) == size(backing LV)   ← fail BEFORE pivot
   ─► online:  virsh blockcopy --pivot   (no guest pause)
      offline: rewrite persistent XML <source dev=/dev/drbdN>, redefine
   ─► (all disks done) define VM on peers
on any failure ─► _unwind: abort blockjobs, drbdadm down/wipe-md, rm .res, lvremove peer LVs,
                  release reserved minor
```

DRBD minors are picked from band 1102–1189 (clear of the singleton minor 1101
and the mesh minors 1132/1133/1134) under a process-local lock + reservation
set, so two parallel converts can't grab the same minor before either's
`/dev/drbdN` exists.

`startup()` is guarded by a lock because `bedrock-d` serves two uvicorn
instances (8443 + 8001) in separate threads, each firing the startup hook on the
same `app`. It also reuses the already-imported `mgmt.orchestrator` module from
`sys.modules` so the orchestrator's shared `_STATE` is the one `bedrock-d`
attached to — a fresh `import orchestrator` would create a second module with
its own `_STATE=None`.

Backup secrets (kopia repo key, S3 creds) are propagated to every node over the
SSH mesh, mode 0600, and never enter the log; only paths/endpoint/bucket metadata
go into `BACKUP_TARGET_SET`. Remote writes use SFTP with tmp+`posix_rename` so a
re-write atomically replaces the existing file.

## Why

Cached `_last_state` + pooled SSH keep the dashboard responsive and stop the 3 s
probe loop from exhausting sshd. Reads come from the local rqlite replica at
level `none` so the dashboard and CLI keep working even when the cluster has lost
quorum. The pivot-time silent-truncation guard exists because DRBD shrinks
`/dev/drbdN` without erroring on an undersized meta LV; asserting size equality
before `blockcopy` catches that with real byte counts instead of a cryptic "Copy
failed" at 0%.
