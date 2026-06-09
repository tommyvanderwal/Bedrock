# installer/lib/cluster_arbiter.py

Moves the cluster-singleton services onto whichever node `bedrock-d`'s realtime
layer (netd's election + witness) has elected mgmt master. The singletons are the
arbiter rqlite daemon (a second `rqlited` co-resident with the master, supplying a
third Raft voter), plus the SeaweedFS filer + S3 owned via `seaweedfs.py`. The
arbiter's data lives on a shared DRBD volume named `cluster` mounted at
`/var/lib/bedrock/cluster`, and its network identity is a secondary
`100.X.Y.254/32` on `lo`. This module is the actuator: it DRBD-promotes the
volume, mounts it, binds `.254`, starts the arbiter service, and starts the filer
— and the exact reverse on demote. It is called from the orchestrator's
`converge()` reconcile tick and from `boot_orchestrator`; netd calls
`ensure_lms_if_last_standing()` each Leader tick. The takeover path touches the
witness and local commands (`drbdadm`, `ip`, `mount`, `systemctl`, debugfs) only
— never rqlite, because rqlite is the service being recovered.

## Functions / Classes

### `attach_state(state) -> None`
Wire `bedrock-d`'s shared `BedrockState` into the module global `SHARED_STATE`.
- **In:** `state` — the live daemon state (`last_election_outcome`, `netd_ws`, `netd.peer_hb`).
- **Out:** None. Sets the module global so the rest of the module reads netd's election outcome and witness handle directly.

### `arbiter_loopback_ip() -> str`
Derive this cluster's arbiter `100.X.Y.254` address.
- **In:** none.
- **Out:** dotted IP string, or `""` if `cluster_uuid` is not yet readable. Reads `cluster_info.cluster_uuid` from local rqlite (level `none`, no quorum) and combines `cluster_addr.cluster_loopback_prefix(uuid)` with octet `254`.

### `promote_to_arbiter_host() -> dict`
Take over hosting the cluster singletons on this node. Idempotent — safe on every role tick; already-done steps no-op.
- **In:** none.
- **Out:** `arbiter_status()` dict. Side effects: at N>=2 runs the witness takeover protocol, then `drbdadm primary cluster` (with `--force` fallback), `mount`, creates `ARBITER_DATA` (mode 0700), `ip addr add .254/32 dev lo`, `rqlite_setup.render_arbiter_env_file()`, starts `bedrock-rqlited-arbiter.service`, `seaweedfs.promote_to_filer_host()`, then writes `cluster_info.mgmt_master`. At N=1 (no cluster DRBD): creates dirs, binds `.254`, promotes the filer, writes `mgmt_master`.

### `demote_arbiter_host() -> dict`
Stop hosting the singletons; reverse of promote. Idempotent.
- **In:** none.
- **Out:** `arbiter_status()` dict. Side effects: `seaweedfs.demote_filer_host()`, `ip addr del .254/32`, and when cluster DRBD is present also stops `bedrock-rqlited-arbiter.service`, unmounts, `drbdadm secondary cluster`. Finally clears this node's witness LMS bit (`set_own_slot(..., tag=0)`) when netd is wired.

### `arbiter_status() -> dict`
Read-only snapshot of local hosting state. No side effects.
- **In:** none.
- **Out:** `{drbd_role, mounted, ip_present, service_active, loopback_ip}` from `drbdadm role`, `mountpoint -q`, `ip addr show lo`, `systemctl is-active`, and `arbiter_loopback_ip()`.

### `i_should_host_arbiter() -> bool`
Decide whether this node should currently host the singletons.
- **In:** none.
- **Out:** `True`/`False`. Reads `SHARED_STATE.last_election_outcome` first: `leader`→True, `noquorum`/`follower`→False. When it is `""`/`init` (or no shared state) falls back to `state.json["role"]` containing `mgmt`.

### `converge() -> dict`
Single-shot reconcile: actuate local hosting state to match `i_should_host_arbiter()`. Called from the orchestrator's `converge_retry` tick and from `boot_orchestrator` after the role settles.
- **In:** none.
- **Out:** result of `promote_to_arbiter_host()`, `demote_arbiter_host()`, or `arbiter_status()` when already in the desired state. No direct rqlite write — only transitively via promote's `mgmt_master` write.

### `ensure_witness_claim(ws, *, node_has_majority) -> bool`
H6: maintain THIS node's witness claim each Leader tick. The claim is an exclusive reservation of the witness's pivotal vote. **Set** it only when the witness is PIVOTAL (`node_has_majority` is False — our node-votes alone fall short of quorum, i.e. an even node-split). **Release** it (set tag=0) the moment a node-majority is (re)established. Owned solely by the claiming node; never auto-expires. Called from netd's Leader branch with `node_has_majority = 100*len(reachable_peers) >= majority`.
- **In:** `ws` — netd's witness handle; `node_has_majority` — bool from the election result.
- **Out:** `True` iff it changed the claim bit. Side effect: `witness.set_own_slot(ws, marker=<masked local DRBD UUID>, tag=TAG_CLAIM)` + readback (3 × 1.5 s) on the set path, or `tag=0` on the release path. No-op while not hosting / witness not valid+confirmed / already in the right state. (Replaces the old `ensure_lms_if_last_standing`, which set on "no fresh peer" and only cleared on self-demote — the stale-claim bug; see INV-3/INV-7 in cluster-quorum-spec.md.)

Private helpers: `_run` (subprocess capture, never raises); DRBD steps `_drbd_role`, `_drbd_resource_exists` (gates on the `cluster-drbd-ready` marker), `_cluster_size`, `_drbd_promote`, `_drbd_secondary`; mount steps `_is_mounted` (uses `mountpoint -q`), `_mount` (resolves device via `drbdadm sh-dev`), `_umount`; IP steps `_arbiter_ip_present`, `_ip_add`, `_ip_del`; service steps `_svc_active`, `_svc_start`, `_svc_stop`; `_run_takeover_protocol` (the 5-step witness gate); `_set_mgmt_master_after_promote`; `_self_node_name`; `_fresh_peer_hbs`; `_peer_claims_master_now`; `_cold_boot_uuid_ok`; `_last_known_master_node_id`; `_read_local_drbd_uuid` (debugfs `data_gen_id`, fallback `drbdadm dump-md`).

Run as a script (`python3 cluster_arbiter.py <cmd>`) the module exposes a manual operator CLI: `status` (default), `promote`, `demote`, `converge`. It prints the resulting `arbiter_status()` dict as JSON and exits non-zero on error.

## How it works

The realtime layer decides; this module actuates. `converge()` reads
`i_should_host_arbiter()` and compares it to actual local state from
`arbiter_status()`:

```
should_host & not host-complete  -> promote_to_arbiter_host()
not should_host & host-partial   -> demote_arbiter_host()
otherwise                        -> return status (no-op)
```

`host-complete` (skip promote) requires every singleton up; `host-partial` (fire
demote) is true if any piece is up — so a follower with a stale `.254` left from a
prior role is always cleaned up.

**N=1 vs N>=2.** `_drbd_resource_exists()` returns true only once `tier_storage`
has written the local `cluster-drbd-ready` marker. While false (N=1, or mid
N=1→N=2 transition), promote skips every DRBD step and runs the singletons on the
local FS, still binding `.254` so client URLs are uniform from day one. Gating on
the marker — not on a parsed `.res` file — keeps `tier_storage` the sole owner of
the N=1→N=2 transition, so the arbiter never fires a premature `drbdadm primary`
or mounts an empty volume before the N=1 snapshot is restored.

**Promote ordering (N>=2):**

```
takeover protocol (witness, steps 1-5)   <- refuses promote if it fails
  drbdadm primary cluster  (--force fallback if peer unreachable)
  mount /dev/drbdN -> /var/lib/bedrock/cluster
  mkdir rqlite data (0700)
  ip addr add .254/32 dev lo
  render_arbiter_env_file
  start bedrock-rqlited-arbiter.service
  seaweedfs.promote_to_filer_host()
  _set_mgmt_master_after_promote          <- RESULT, written last
```

An idempotent fast-path skips the witness protocol when DRBD is already Primary
AND the mount is present AND `.254` is bound (the common case on repeated converge
ticks).

**mgmt_master is a result, not a trigger.** netd's election DRIVES the promote;
`mgmt_master` in rqlite is written only at the end, by
`_set_mgmt_master_after_promote()`, and only once `arbiter_status()` confirms
hosting (N>=2: service_active + ip_present + DRBD Primary; N=1: filer active +
ip_present). If that write fails because rqlite is still electing, the next
converge tick re-promotes (no-op) and retries — no deadlock, because the promote
never gates on `mgmt_master` already being set.

**Takeover protocol (`_run_takeover_protocol`, rqlite-free):**

```
no shared state / netd not running -> allow (boot fallback)
peer claims master (fresh HB)      -> DEFER (steal-back guard)
last_master is None or == me:
    cold-boot UUID check stale     -> REFUSE
    N>=2 & cold-boot patience left -> DEFER (30s)
    else                           -> ALLOW (first/self promote)
last_master is another node:
    witness unreachable, N>=3      -> ALLOW (rqlite quorum covers it)
    witness unreachable, N<=2      -> REFUSE
    slot[M] missing                -> REFUSE (INV-7 worst-case)
    slot[M] fresh                  -> REFUSE (cluster healthy elsewhere)
    slot[M] stale & lms=1          -> REFUSE (operator must clear LMS)
    slot[M] stale & lms=0:
        local DRBD UUID != marker  -> REFUSE (divergence)
        else: set own slot lms+marker, read back (3x1.5s) -> ALLOW
```

The steal-back guard (`_peer_claims_master_now`) reads netd's in-memory `peer_hb`:
if any peer's heartbeat is fresh (within `PEER_HB_FRESH_S` = 2 s) and advertises
itself as `believed_master`, this node defers rather than steal the role back from
a live survivor. Cold-boot patience defers the first promote for
`COLD_BOOT_PATIENCE_S` (30 s) at N>=2 so a slower peer can converge cleanly.
`_cold_boot_uuid_ok` refuses only when it can prove the local DRBD generation is
stale: the witness still holds this node's own slot, its marker differs from the
local UUID, and `state.classify_arbiter_uuid` reports that local UUID superseded.

**DRBD `--force`.** `_drbd_promote()` first tries a plain `drbdadm primary`; on a
failover the previous primary is unreachable, so DRBD refuses with "Need access to
UpToDate data". Since the election + witness DRBD-UUID match already authorized
this node as master, it retries with `--force`.

**Witness-claim lifecycle.** A claim (`tag.claim=1`, the bit formerly called "lms")
never auto-expires. The arbiter owns the bit: `ensure_witness_claim()` SETS it only
while the witness is PIVOTAL for this node's quorum (an even node-split,
`node_has_majority` False) and RELEASES it (`tag=0`) the moment a node-majority is
restored; `demote_arbiter_host()` also clears it on self-demote. The everyday
set/release is fully automatic — an operator decommission (`bedrock node leave`) is
needed ONLY when a genuinely-pivotal holder dies permanently before releasing. See
INV-3/INV-7 in `docs/cluster-quorum-spec.md`. (The predecessor `ensure_lms_if_last_standing`
SET on "only one left" and cleared only on self-demote → a node that went solo once,
e.g. the N=1 init window, held a stale claim forever and disabled mgmt-master
auto-failover.)

**Reading the local DRBD UUID.** `_read_local_drbd_uuid()` reads DRBD9's debugfs
`data_gen_id` while the resource is up (the takeover case), falling back to
`drbdadm dump-md` for a detached resource.

## Why

The witness + local commands are the only inputs on the takeover path because the
arbiter rqlite is the very service being recovered — making rqlite a precondition
would deadlock at N=2, where rqlite cannot form quorum until the arbiter being
decided about is running. `.254` lives on `lo` as a `/32` so rqlite and clients
see a constant address across master moves; the IP simply changes which node's
`lo` it sits on.
