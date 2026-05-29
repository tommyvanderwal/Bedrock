# Change HA level (cattle <-> pet <-> ViPet)

Converts a VM between workload types by swinging each disk in or out of DRBD.
A **running** VM stays up the whole time: `virsh blockcopy --reuse-external
--pivot` mirrors the disk under live QEMU I/O. A **shut-off** VM skips
blockcopy and just rewrites its persistent libvirt XML to point at the DRBD
device; DRBD's initial sync streams to the peers in the background.

**Triggered by:**

- Dashboard: VM **Settings** page -> PET / ViPet checkboxes.
- HTTP: `POST /api/vms/{vm_name}/ha-level` with `{"vm_type": "cattle|pet|vipet",
  "peer_nodes": [...]?}`. `peer_nodes` is optional (auto-picked otherwise).
  All validation runs synchronously, so a bad request gets a 4xx instead of a
  task that fails async. On success returns
  `{"status":"accepted","task_id":...,"from":...,"to":...}`; progress flows
  over the WS `task` channel. See [`../components/tasks.md`](../components/tasks.md).
  A no-op target returns `{"status":"no-op","current":...}`.

**Source:** `mgmt/app.py` — `api_vm_set_ha_level` (route) ->
`_vm_set_ha_level` (dispatch) -> `_vm_set_ha_level_up` / `_vm_set_ha_level_down`.

Current type is derived live: `vm.drbd_resource` set + `_count_drbd_peers >= 3`
=> vipet; resource set => pet; else cattle. Source node = `running_on`, falling
back to the first `defined_on` node for a shut-off VM.

## Multi-disk semantics

The convert iterates over **every** data disk (`get_vm_disks`, cdroms
excluded). A VM with `vda`+`vdb`+`vdc` becomes three DRBD resources on
cattle->pet:

```
  vm-NAME-disk0   <- vda's local LV
  vm-NAME-disk1   <- vdb's local LV
  vm-NAME-disk2   <- vdc's local LV
```

Each resource gets its own `.res` file, external meta LV, and peer LV pair.
Minors come from `_next_drbd_minor()`, which picks + reserves an unused minor
in the **1102..1189** band across all hosts (process-local lock guards two
concurrent converts from picking the same minor). `drbd_port_for(minor)` maps
each to a port in the **7700..7799** band (minor 1102 -> 7702, 1103 -> 7703).
`_gen_drbd_res(resource, minor, peers)` takes the resource name explicitly so
one VM's `.res` files never collide.

**Atomicity (upgrade only):** all-or-nothing per operation. If disk 2 fails
mid-way, `_unwind()` aborts any in-flight blockcopy (`virsh blockjob --abort`,
so libvirt clears `disk->blockjob`) then unwinds disks 0 and 1 (drbd down +
wipe-md + remove peer LVs + delete `.res` + release the minor reservation), so
the VM never ends up half-pet. Steps emit per disk via `task.step_*`, so the
dashboard drawer shows progress:

```
  disk0 (vda): create meta LV on source            ok
  disk0 (vda): generate DRBD res                    ok
  disk0 (vda): create-md + up                       ok
  disk0 (vda): assert /dev/drbd1102 == backing LV   ok
  disk0 (vda): blockcopy -> /dev/drbd1102           ok
  disk1 (vdb): create meta LV on source             running ...
```

## What each transition does

```
  +-- cattle --+     +--- pet (2-way) ---+    +---- ViPet (3-way) ----+
  |  local LV  | <-> | local LV + DRBD   | <-> | local LV + DRBD 3-way |
  |  (raw)     |     | + 1 peer LV+meta  |    | + 2 peer LV+meta pairs |
  +------------+     +-------------------+    +-----------------------+

    cattle -> pet     : add peer, swing to /dev/drbdN, sync
    pet -> ViPet      : add 3rd peer, drbdadm adjust, background sync
    ViPet -> pet      : drop 1 peer, rewrite config, del-peer on kept
    pet -> cattle     : swing back to raw LV, drbdadm down, lvremove peer
```

Direct cattle <-> ViPet is allowed too (rank-based dispatch); it just adds or
drops two peers in one operation.

## Preconditions

- **Running** VM: blockcopy path (zero downtime). **Shut-off** VM: offline
  XML-rewrite path. Both are supported; the VM state is not a hard gate.
- Upgrade requires enough usable peer nodes. The route filters `peer_nodes`
  (or all other nodes) down to the count needed (`pet`=1, `vipet`=2) and 400s
  if too few. The dashboard greys the checkbox in the same case.
- Each peer must already have a thin pool (`bedrock bootstrap` ran on it).
  `_ensure_thinpool` only verifies it and raises a clear 500 if absent;
  runtime never auto-creates pools.
- SSH key mesh established between the source host and any peers used.

## Sequence -- cattle -> pet (running VM)

```
  T=0   POST /api/vms/NAME/ha-level {"vm_type":"pet"}
        |
        | build_cluster_state() -> current type = cattle, src = running_on
        | chosen = (peer_nodes or other nodes)[:1]
        | get_vm_disks(src) -> [{target:vda, backing_lv:/dev/VG/vm-NAME-disk0}, ...]
        |
  per disk i (resource = vm-NAME-disk<i>):
        |
        | meta_mb = max(32, 32 + size_gb*2); meta_lv = <data-lv>-meta
        | ssh src : lvcreate -V <meta_mb>M -T VG/thinpool -n <data-lv>-meta
        |
        | ssh peer: _ensure_thinpool
        | ssh peer: lvcreate -V <size>M data LV ; lvcreate -V <meta_mb>M meta LV
        |
        | minor = _next_drbd_minor(all_hosts)
        | _gen_drbd_res(resource, minor, peers_info)  -> /etc/drbd.d/<res>.res
        |   protocol C; external meta-disk; split-brain policy
        |   (after-sb-0pri discard-zero-changes / 1pri discard-secondary /
        |    2pri disconnect); 2 "on" blocks + connection
        | write .res to all_hosts (base64 over ssh)
        |
        | for h in all_hosts:
        |   ssh h: drbdadm create-md --force --max-peers=7 <res>
        |   ssh h: drbdadm up <res>
        | ssh src: drbdadm primary --force <res>   (src has data; peer SyncTarget)
        |
        | SILENT-TRUNCATION GUARD: assert blockdev /dev/drbd<minor> == src LV
        |   bytes. A short device => meta LV too small; fail loud pre-pivot.
        |
        | ssh src: virsh blockjob NAME vda --abort   (clear any stale job)
        | ssh src: virsh blockcopy NAME vda /dev/drbd<minor>
        |            --reuse-external --wait --pivot --verbose
        |            --transient-job --blockdev --format raw
        |   QEMU mirrors vda -> /dev/drbd<minor> while the VM keeps writing,
        |   then atomically pivots the VM disk to /dev/drbd<minor>. External
        |   meta keeps the bytes 1:1, so on the primary the copy is local;
        |   DRBD replicates to the peer.
        | _release_drbd_minor(minor)
        |
  all disks done:
        | dumpxml NAME -> define on each peer (so a later migrate works)
        | push_log "Convert NAME: cattle -> pet in <dur>s (N disk(s))"
        | return {"status":"converted","from":"cattle","to":"pet",
        |         "disks":[...],"duration_s":<dur>,"peers":[src,...]}
        |
  (async) DRBD resync peer<-primary continues; the DRBD tile shows
          Inconsistent/SyncTarget until UpToDate.
```

Shut-off VM: identical up to `drbdadm primary --force`, then instead of
blockcopy it rewrites `<source dev='...'>` in the inactive XML to
`/dev/drbd<minor>` and `virsh define`s it. No local copy; DRBD's initial sync
from the (forced-primary) live data LV streams to the peer.

## Sequence -- pet -> ViPet

No blockcopy: add a third peer to each already-primary DRBD resource.

```
  T=0   POST /api/vms/NAME/ha-level {"vm_type":"vipet"}
        | resources = [d.drbd_resource for d in get_vm_disks(src)]
        | new_peer = peer_nodes[0], else first node not in existing peers
        |
  per resource:
        | existing = _parse_drbd_res(src, resource)  -> peers, minor, lv/meta, size
        | ssh new_peer: _ensure_thinpool; lvcreate data LV; lvcreate meta LV
        | _gen_drbd_res(resource, minor, 3 peers) -> connection-mesh; write all 3
        | ssh new_peer: drbdadm create-md --force --max-peers=7 <res>
        | ssh all_hosts: drbdadm adjust <res>   (opens the new connection)
        | ssh new_peer: drbdadm up <res>
        |
        | dumpxml NAME -> virsh define on new_peer
        | push_log "Convert NAME: pet -> vipet in <dur>s (N resource(s) added peer X)"
        | return {"status":"converted","from":"pet","to":"vipet",
        |         "resources":[...],"added_peer":X,"duration_s":<dur>}
        |
  (async) initial sync to new_peer over the DRBD ring; writes still commit on
          the 2 UpToDate copies, so the VM is unaffected.
```

## Sequence -- ViPet -> pet (downgrade)

```
  T=0   POST /api/vms/NAME/ha-level {"vm_type":"pet", "peer_nodes":["DROP"]?}
        |
        | drop = peer_nodes[0] if set, else first existing peer != src.
        | Never the source/primary; passing it 400s.
        |
        | ssh drop: virsh undefine NAME    (remove VM from dropped peer's libvirt)
        |
  per resource:
        | ssh drop: drbdadm down <res> ; drbdadm wipe-md --force <res>
        | rewrite .res for the 2 remaining peers; write kept hosts; rm on drop
        |
        | del-peer dance per kept host, drop_idx = peers.index(drop) (strict order):
        |   1. drbdsetup disconnect <res> <drop_idx> --force  (tear TCP; --force
        |      because the link may be mid-sync)
        |   2. drbdsetup del-peer   <res> <drop_idx> --force  (free the node-id slot)
        |   3. drbdadm  adjust      <res>                     (re-read 2-peer mesh)
        |
        | ssh drop: lvremove -f <lv_path> <meta_path>
        |
        | push_log "Convert NAME: vipet -> pet (dropped X, N resource(s))"
        | return {"status":"converted","from":"vipet","to":"pet",
        |         "dropped":X,"resources":[...]}
```

## Sequence -- pet or ViPet -> cattle

```
  T=0   POST /api/vms/NAME/ha-level {"vm_type":"cattle"}
        |
        | per_resource = _parse_drbd_res(src, r) for each disk's resource
        |
  pivot phase (before teardown -- tearing DRBD first would crash the VM):
        | per resource: match target_dev via disk.drbd_minor == existing.minor
        |   ssh src: virsh blockcopy NAME <target_dev> <lv_path>
        |              --reuse-external --wait --pivot --verbose
        |              --transient-job --blockdev --format raw
        |   QEMU mirrors /dev/drbdN -> raw LV (same bytes) and pivots to it.
        |
  teardown phase:
        | for each non-src peer: ssh: virsh undefine NAME
        | per resource, per peer:
        |   ssh: drbdadm down <res> ; drbdadm wipe-md --force <res>
        |   ssh: rm -f /etc/drbd.d/<res>.res
        |   src:  lvremove -f <meta_path>           (data LV kept -- it's the VM disk)
        |   peer: lvremove -f <lv_path> <meta_path>
        |
        | push_log "Convert NAME: <cur> -> cattle in <dur>s"
        | return {"status":"converted","from":<cur>,"to":"cattle","duration_s":<dur>}
```

## Logging

Per-step progress is reported through the task system (`task.step_start` /
`step_done` / `step_fail`), surfaced in the dashboard task drawer and on the WS
`task` channel. One summary `push_log` lands per operation in **VictoriaLogs**
as `{_msg, _time, hostname, app="bedrock-mgmt", level}` and broadcasts on the
WS `event` channel:

```
Convert NAME: cattle -> pet in <dur>s (N disk(s))
Convert NAME: pet -> vipet in <dur>s (N resource(s) added peer X)
Convert NAME: vipet -> pet (dropped X, N resource(s))
Convert NAME: <cur> -> cattle in <dur>s
Convert NAME: FAILED (<err>) -- unwinding              (level=error, on rollback)
```

## Why the specific order

- **Create the meta LV before `create-md`**: `drbdadm create-md` writes at the
  end of the meta-disk; if the meta LV came later, `create-md` would land on
  the data LV and corrupt it.
- **External meta-disk, not internal**: internal metadata steals tail bytes,
  so `/dev/drbdN < underlying LV`. `virsh blockcopy` refuses a smaller
  destination ("dst too small"). External meta keeps them byte-identical, which
  the silent-truncation guard asserts before any pivot.
- **`--max-peers=7` at create-md**: reserves bitmap slots for up to 7 peers so
  adding a 3rd peer later doesn't fail with "Not enough free bitmap slots"
  (whose only fix is wipe-md + full resync).
- **`drbdadm primary --force` before blockcopy**: blockcopy writes go through
  DRBD, which refuses writes to a Secondary.
- **`--blockdev --format raw` on blockcopy**: the new QEMU blockdev interface
  supports `host_device`; the legacy API assumes a regular `file` and fails on
  `/dev/drbdN`.
- **`--transient-job` on blockcopy**: persistent domains reject blockcopy
  without it, and the job needn't survive a VM restart.
- **Define VM on peers only after a successful pivot**: a mid-flight failure
  must not leave stale XML pointing at a nonexistent drbdN on peers.
- **pet/ViPet -> cattle pivots before teardown**: tearing DRBD down first would
  yank the VM's disk and crash it.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| Silent-truncation guard tripped | `/dev/drbdN` smaller than backing LV (meta LV too small / internal meta) | Caught pre-pivot with byte counts in the log; fix the `meta_mb` formula and retry. |
| `blockcopy failed` (rc != 0) | dst smaller than src, or peer unreachable | Compare `blockdev --getsize64`; ensure external meta; the upgrade rolls back automatically. |
| `Not enough free bitmap slots` adding 3rd peer | Resource made without `--max-peers=7` | Convert -> cattle and recreate. |
| `Connection for peer node id N already exists` | Stale peer slot after a failed add | `drbdsetup disconnect RES N --force; drbdsetup del-peer RES N --force; drbdadm adjust RES`. |
| 500 `thin pool ... does not exist on <peer>` | Peer never ran `bedrock bootstrap` | Bootstrap that node; `_ensure_thinpool` does not auto-create pools. |
| `Host key verification failed` on blockcopy/migrate | SSH known_hosts cold for a peer | `ssh-keyscan -H <peer> >> /root/.ssh/known_hosts`. |

## State after each transition

| Direction | On primary | On peer(s) | DRBD state |
|---|---|---|---|
| cattle -> pet | data LV + meta LV, VM on `/dev/drbdN` | new data LV + meta LV, VM defined | Primary / SyncSource -> UpToDate |
| pet -> ViPet | unchanged | existing peer unchanged; new peer gets LV+meta, VM defined | new peer SyncTarget until caught up |
| ViPet -> pet | unchanged | dropped peer: VM undefined, LVs gone, `.res` removed | 2-way resource, adjust applied |
| pet/ViPet -> cattle | VM on raw LV; meta LV gone; data LV kept | VM undefined, LVs + `.res` removed | resource torn down |

## Operator perspective

- **Downtime**: zero for a running VM -- QEMU never pauses beyond the
  sub-millisecond blockcopy pivot. A shut-off VM converts via XML rewrite.
- **Latency during sync**: negligible on the primary (protocol C commits on
  local ACK for already-synced regions). A fresh peer shows `Inconsistent`
  until its resync catches up.
- **Rollback**: every transition has an inverse; flipping the checkbox on and
  off is safe.
