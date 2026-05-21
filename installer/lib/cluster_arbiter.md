# `cluster_arbiter.py`

**Module purpose.** Owns the imperative "this node should/should
not host the cluster singletons" transitions. The cluster
singletons are:

- the `tier-critical` DRBD volume mounted at `/var/lib/bedrock/cluster`
- the **arbiter rqlite** instance (a separate `rqlited` on ports
  4011/4012, bound to the .254 VIP)
- the `.254/32` master VIP on `lo`
- the SeaweedFS **filer** (which needs `/var/lib/bedrock/cluster/seaweedfs/`
  to be the same leveldb3 on whichever node currently holds master).
  **Not** the S3 gateway — that runs on every node bound `0.0.0.0`
  and authenticates against IAM identities living inside the
  filer DB. See `docs/storage-architecture.md`.

`converge()` is the idempotent entry point called from
`mgmt/orchestrator.rqlite_subscriber` every rqlite-revision change
AND from `mgmt/orchestrator.converge_retry` every 5 s. It reads
`state.json["role"]` ("mgmt+compute" or "compute") to decide
should-host, and `arbiter_status()` to decide am-host. If they
disagree, it calls `promote_to_arbiter_host()` or
`demote_arbiter_host()`.

The "should-host" question is answered by `state.json["role"]`,
which the orchestrator's subscriber regenerates from rqlite's
`cluster_info.mgmt_master` on every revision tick. Election in
bedrock-net is what flips `cluster_info.mgmt_master`; that
propagates via Raft to state.json on every node, which flips
`converge()` here.

## Constants

- `TIER_RESOURCE = "tier-critical"` — DRBD resource name.
- `MOUNT_POINT = Path("/var/lib/bedrock/cluster")` — where the
  DRBD device is mounted on the master.
- `ARBITER_DATA = MOUNT_POINT / "rqlite-arbiter"` — arbiter
  rqlite's data dir (lives on the DRBD volume so it follows the
  master).
- `ARBITER_SVC = "bedrock-rqlited-arbiter.service"`.

## Functions

- `_run(cmd, check=False, timeout=30) -> (rc, stdout, stderr)` —
  internal subprocess wrapper that captures + returns; never raises
  unless `check=True`.
- `arbiter_loopback_ip() -> str` — returns `100.X.Y.254` derived
  from this cluster's CGNAT prefix (see `cluster_addr.py`).
  Returns `""` if `state.json` is missing.
- `_drbd_role() -> str` — `Primary` / `Secondary` / `Unknown`.
- `_drbd_resource_exists() -> bool` — true iff
  `/etc/drbd.d/tier-critical.res` exists AND `drbdadm status
  tier-critical` returns 0. False at N=1 before `bedrock storage
  promote`.
- `_cluster_size() -> int` — count of nodes in cluster.json.
- `_drbd_promote()` — `drbdadm primary tier-critical`. If that
  fails with "Need access to UpToDate data" (peer unreachable
  during failover), retries with `drbdadm -- --force primary` —
  the takeover protocol (`docs/cluster-quorum-spec.md`) has already
  verified `slot[prev_master].marker == local drbdadm current-uuid`
  before this function is called, so the local data IS UpToDate
  even if DRBD can't reach the peer to confirm.
- `_drbd_secondary()` — `drbdadm secondary tier-critical`. Idempotent
  failure (already secondary) is logged but not raised.
- `_is_mounted(path) -> bool` — `findmnt -n -T <path>` returns 0.
- `_mount()` — resolves the DRBD device via `drbdadm sh-dev`, then
  `mount /dev/drbdN /var/lib/bedrock/cluster`. No-op if already
  mounted.
- `_umount()` — lazy unmount of `/var/lib/bedrock/cluster`. No-op
  if not mounted.
- `_arbiter_ip_present() -> bool` — checks `ip -4 addr show lo`
  for the .254/32 line.
- `_ip_add() -> str` — adds `.254/32` to lo. Treats "File exists"
  as success (idempotent).
- `_ip_del()` — removes `.254/32` from lo. Idempotent.
- `_svc_active(unit) -> bool` — `systemctl is-active --quiet`.
- `_svc_start(unit)` / `_svc_stop(unit)` — `systemctl start/stop`.
- `promote_to_arbiter_host() -> dict` — main promote sequence:
  - N=1 mode (no `tier-critical.res`): ensure `MOUNT_POINT`
    directory exists, ensure `ARBITER_DATA` dir exists. Start
    SeaweedFS filer + s3 (`seaweedfs.promote_to_filer_host`). No
    .254 VIP at N=1.
  - N≥2 mode: `drbdadm primary` → mount → ensure
    `ARBITER_DATA` mode 0700 → `_ip_add()` (.254) → render
    `/etc/bedrock/rqlited-arbiter.env` → start the arbiter rqlite
    unit → start filer + s3.
  Returns `arbiter_status()` at the end.
- `demote_arbiter_host() -> dict` — exact reverse:
  - Stop filer + s3 first (they hold the mount open).
  - Stop arbiter rqlite.
  - `_ip_del()` (.254).
  - `_umount()`.
  - `drbdadm secondary tier-critical`.
- `arbiter_status() -> dict` — snapshot: `{drbd_role, mounted,
  ip_present, service_active, ...}`. Used by `converge()` to
  decide if we're currently hosting.
- `i_should_host_arbiter() -> bool` — `True` iff
  `state.json["role"]` contains "mgmt". The rqlite subscriber is
  what flips this; election only writes mgmt_master, the rest
  cascades.
- `converge() -> dict` — read should-host + am-host. If
  should AND not am → `promote_to_arbiter_host`. If not should
  AND am → `demote_arbiter_host`. Else return current status.
  Idempotent and safe to call on every rqlite revision + every
  5 s timer tick.
