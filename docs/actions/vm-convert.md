# Change HA level (cattle ↔ pet ↔ ViPet)

Hot-converts a running VM between workload types. The VM stays up the entire
time — data is swung in or out of DRBD via `virsh blockcopy --reuse-external
--pivot` while QEMU keeps serving I/O.

**Triggered by:**

- Dashboard: VM detail page → PET / ViPet checkboxes
- HTTP: `POST /api/vms/{name}/convert` with `{"target_type": "cattle|pet|vipet"}`

**Source:** `mgmt/app.py:_vm_convert`, `_vm_convert_upgrade`,
`_vm_convert_downgrade`.

## What each transition does

```
  ┌── cattle ──┐     ┌─── pet (2-way) ───┐    ┌──── ViPet (3-way) ────┐
  │  local LV  │ ◀─▶ │ local LV + DRBD   │ ◀─▶ │ local LV + DRBD 3-way │
  │  (raw)     │     │ + 1 peer LV+meta  │    │ + 2 peer LV+meta pairs│
  └────────────┘     └───────────────────┘    └───────────────────────┘

    cattle → pet     : add peer, swing to /dev/drbdN, sync
    pet → ViPet      : add 3rd peer, drbdadm adjust, background sync
    ViPet → pet      : drop 1 peer, rewrite config, del-peer on primary
    pet → cattle     : swing back to raw LV, drbdadm down, lvremove peer
```

Each step logs to VictoriaLogs **and** pushes a WS `event` to the dashboard.

## Preconditions

- VM is **running**. Stopped VMs are rejected (`400 VM must be running to
  hot-convert`) — the pivot relies on QEMU's live blockcopy.
- For upgrade: enough peer nodes exist (the checkbox is already greyed
  out in the UI when not; the API enforces the same).
- SSH key mesh established between home and any peer nodes to be used.

## Sequence — cattle → pet

```
  T=0   POST /api/vms/NAME/convert {"target_type":"pet"}
        │
        │ build_cluster_state() → find current type = "cattle"
        │ pick peer = first other node
        │
        │ _find_vm_disk(src_host, NAME) via virsh dumpxml
        │   → src_lv = /dev/almalinux/vm-NAME-disk0, target_dev = vda
        │
        │ meta_lv_name = vm-NAME-disk0-meta   (4 MB thin)
        │ size_mb = blockdev --getsize64 src_lv  →  MB
        │
  T+0.1 ssh src: lvcreate -V 4M  -T almalinux/thinpool -n vm-NAME-disk0-meta
        │ push_log "Convert NAME: create external DRBD meta LV <path>"
        │
  T+0.3 ssh peer: _ensure_thinpool
        │ ssh peer: lvcreate -V <size>M -T almalinux/thinpool -n vm-NAME-disk0
        │ ssh peer: lvcreate -V 4M      -T almalinux/thinpool -n vm-NAME-disk0-meta
        │ push_log "Convert NAME: create peer LVs on <peer> (<n>M data + 4M meta)"
        │
  T+1s  _next_drbd_minor(all_hosts)  →  pick unused minor (e.g. 1000)
        │ _gen_drbd_res(NAME, minor, peers_info)  →  /etc/drbd.d/vm-NAME-disk0.res
        │   peers_info = [(name, ip, lv, meta_path), ...]
        │   protocol C; external meta-disk; max-peers via create-md flag
        │
        │ for h in all_hosts:
        │   ssh h: echo <b64> | base64 -d > /etc/drbd.d/vm-NAME-disk0.res
        │
  T+1.3 for h in all_hosts:
        │   ssh h: drbdadm create-md --force --max-peers=7 vm-NAME-disk0
        │   ssh h: drbdadm up vm-NAME-disk0
        │
  T+2s  ssh src: drbdadm primary --force vm-NAME-disk0
        │         (src has the data; peer is SyncTarget / Inconsistent)
        │
  T+2.3 push_log "Convert NAME: blockcopy vda → /dev/drbd1000"
        │
  T+2.3 ssh src:
        │   virsh blockcopy NAME vda /dev/drbd1000
        │     --reuse-external --wait --pivot --verbose
        │     --transient-job --blockdev --format raw
        │
        │   QEMU mirrors all of vda → /dev/drbd1000 while the VM keeps
        │   writing. /dev/drbd1000 maps to the same bytes on this host
        │   (src_lv is identical to the first N bytes of drbdN under
        │   external meta), so the copy is essentially self-to-self on
        │   the primary and replicates to the peer via DRBD.
        │
        │   On blockcopy completion, QEMU does an atomic "pivot": the
        │   VM disk becomes /dev/drbd1000; the original LV is released.
        │
  T+~4s blockcopy done.
        │
  T+4.1 dumpxml src NAME → the VM XML now has
        │     <source dev='/dev/drbd1000'/>
        │   scp XML to peer, virsh define on peer
        │     (so peer can run the VM after a future migrate)
        │
  T+4.5 push_log "Convert NAME: cattle → pet in 4.24s (DRBD minor 1000)"
        │ return 200 {"status":"converted","duration_s":4.24, ...}
        │
  (async) DRBD sync peer ← primary continues in background (~1 MB/s × disk-size
          depending on link and thin-mapping); dashboard DRBD tile shows
          Inconsistent/SyncTarget until it flips to UpToDate.
```

## Sequence — pet → ViPet

No blockcopy. Just add a third peer to an already-primary DRBD resource:

```
  T=0   POST /api/vms/NAME/convert {"target_type":"vipet"}
        │
        │ _parse_drbd_res(src, "vm-NAME-disk0") gives:
        │   { peers:[A,B], minor:1000, lv_path, meta_path, size_bytes }
        │
        │ chosen = first node not in peers
        │
  T+0.1 ssh new_peer: _ensure_thinpool
        │ ssh new_peer: lvcreate data LV  (size_mb from existing)
        │ ssh new_peer: lvcreate meta LV  (4 M)
        │ push_log "Convert NAME: add 3rd peer <new_peer>"
        │
  T+0.5 regenerate /etc/drbd.d/vm-NAME-disk0.res with 3 "on" blocks +
        │ connection-mesh; broadcast to all 3 nodes.
        │
  T+0.7 ssh new_peer: drbdadm create-md --force --max-peers=7 ...
        │ ssh all_hosts: drbdadm adjust vm-NAME-disk0
        │   (picks up the new peer entry, opens connection)
        │ ssh new_peer: drbdadm up vm-NAME-disk0
        │
  T+1s  scp VM XML to new_peer, virsh define
        │
  T+1.2 push_log "Convert NAME: pet → vipet, added <new_peer>"
        │ return 200 {"status":"converted","added_peer":"<new_peer>"}
        │
  (async) initial sync to new_peer over DRBD ring. During the sync, the
          DRBD tile shows SyncSource/Inconsistent on peer; writes still
          commit on the 2 UpToDate copies so the VM is unaffected.
```

## Sequence — ViPet → pet (downgrade)

```
  T=0   POST /api/vms/NAME/convert {"target_type":"pet", "peer_nodes":["<drop>"]}
        │  (if peer_nodes omitted, auto-pick a non-primary)
        │
  T+0.1 ssh drop: virsh undefine NAME     (remove VM from peer's libvirt)
        │ ssh drop: drbdadm down vm-NAME-disk0
        │ ssh drop: drbdadm wipe-md --force vm-NAME-disk0
        │
  T+0.5 rewrite /etc/drbd.d/vm-NAME-disk0.res for the remaining 2 peers
        │ write to kept_hosts; rm on dropped host
        │
  T+0.8 for h in kept_hosts:
        │   ssh h: drbdsetup disconnect RES <dropped_node_id> --force
        │   ssh h: drbdsetup del-peer RES <dropped_node_id> --force
        │   ssh h: drbdadm adjust vm-NAME-disk0
        │
  T+1s  ssh drop: lvremove -f <lv_path> <meta_path>
        │ push_log "Convert NAME: vipet → pet (dropped <drop>)"
```

## Sequence — pet or ViPet → cattle

```
  T=0   POST /api/vms/NAME/convert {"target_type":"cattle"}
        │
        │ _parse_drbd_res → peers, lv_path, minor, meta_path
        │ _find_vm_disk → target_dev (vda)
        │
  T+0.1 push_log "Convert NAME: pivot vda back to <lv_path>"
        │
  T+0.1 ssh src:
        │   virsh blockcopy NAME vda <lv_path>
        │     --reuse-external --wait --pivot --verbose
        │     --transient-job --blockdev --format raw
        │
        │   QEMU mirrors /dev/drbdN → raw LV (on primary, same underlying
        │   bytes minus meta-disk area; copy is a local no-op + trim).
        │   Pivots VM to the raw LV.
        │
  T+~2s for n in peers:
        │   h = host of n
        │   if n != src:
        │     ssh h: virsh undefine NAME
        │   ssh h: drbdadm down vm-NAME-disk0
        │   ssh h: drbdadm wipe-md --force vm-NAME-disk0
        │   ssh h: rm -f /etc/drbd.d/vm-NAME-disk0.res
        │
  T+~3s for n in peers where n != src:
        │   ssh host: lvremove -f <lv_path> <meta_path>
        │ ssh src: lvremove -f <meta_path>   (data LV kept — it's the VM disk now)
        │
  T+3.5 push_log "Convert NAME: vipet → cattle in 3.77s" (or pet→cattle)
        │ return 200 {"status":"converted","duration_s":3.77}
```

## Log lines — exact strings

Each `push_log` call lands in **VictoriaLogs** as a JSON line
(`{_msg, _time, hostname, app=bedrock-mgmt, level=info}`) and is
broadcast on the WebSocket `event` channel instantly.

```
Convert NAME: create external DRBD meta LV <path>
Convert NAME: create peer LVs on <peer> (<n>M data + 4M meta)
Convert NAME: blockcopy <target_dev> → /dev/drbd<minor>
Convert NAME: cattle → pet in <dur>s (DRBD minor <minor>)
Convert NAME: add 3rd peer <new_peer>
Convert NAME: pet → vipet, added <new_peer>
Convert NAME: vipet → pet (dropped <drop_name>)
Convert NAME: pivot <target_dev> back to <lv_path>
Convert NAME: vipet → cattle in <dur>s
Convert NAME: pet → cattle in <dur>s
```

Browser side: these land in the Recent Logs panel via the `events` store,
newest at top, **before** the next cluster-state push flips the tile
(tiles lag logs by up to ~3 s).

## Why the specific order

- **Extend (or add) metadata LV before touching DRBD**: `drbdadm create-md`
  writes at the very end of the meta-disk; if the meta LV were created
  after, `create-md` would hit the underlying data LV and corrupt it.
- **External meta-disk, not internal**: DRBD internal metadata steals
  ~128 KB off the tail of the data LV, so `/dev/drbdN < underlying-LV`
  size. `virsh blockcopy` refuses to target a smaller destination — "dst
  too small". External meta-disk keeps them byte-identical.
- **`--max-peers=7` at create-md**: reserves bitmap slots for up to 7
  peers in the metadata region. Without it, adding a 3rd peer later
  fails with "Not enough free bitmap slots" and the only recovery is
  `wipe-md` + full resync.
- **`drbdadm primary --force` before blockcopy**: blockcopy's write path
  goes through DRBD, which refuses writes to a Secondary.
- **`--blockdev --format raw` on blockcopy**: legacy blockdev API assumes
  `file` driver for the destination and fails with "'file' driver
  requires … to be a regular file" on /dev/drbdN. `--blockdev` switches
  to the new QEMU blockdev interface which supports `host_device`.
- **`--transient-job` on blockcopy**: persistent domains reject blockcopy
  without this flag. The job state doesn't need to survive a VM restart.
- **Define VM on peers only after successful pivot**: if the pivot fails
  mid-flight, we don't want stale XML pointing at a nonexistent drbdN on
  peers.
- **Downgrade pet → cattle pivots *before* teardown**: if DRBD is torn
  down first, the VM would lose its disk and crash.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `Copy failed` from virsh blockcopy | dst smaller than src (internal meta?) or peer unreachable | Check `blockdev --getsize64` on both; ensure external meta. |
| `Not enough free bitmap slots` when adding 3rd peer | Resource was created without `--max-peers=7` | Destroy (convert → cattle) and recreate with max-peers. |
| `Connection for peer node id N already exists` | Stale peer slot after failed add | `drbdsetup disconnect RES N --force; drbdsetup del-peer RES N --force; drbdadm adjust RES`. |
| HTTP 500 "no resources defined!" | `drbdadm status` returned before res file written (race on slow SSH) | Retry the convert — idempotent on a fresh cluster. |
| lvcreate on peer fails "Volume group … not found" | Peer never had a VM, no thinpool yet | `_ensure_thinpool()` now runs first; upgrade mgmt if older build. |
| Host key verification failed on blockcopy/migrate | SSH known_hosts cold on a peer | `ssh-keyscan -H <peer> >> /root/.ssh/known_hosts` — or set `BEDROCK_SSH_PASS` (dev only). |

## State after each transition

| Direction | On primary | On peer(s) | DRBD state |
|---|---|---|---|
| cattle → pet | data LV + meta LV, VM on `/dev/drbdN` | new data LV + meta LV, VM defined | Primary / SyncSource → UpToDate |
| pet → ViPet | unchanged | existing peer same; new peer gets LV+meta, VM defined | new peer SyncTarget until caught up |
| ViPet → pet | unchanged | dropped peer: VM undefined, LVs gone, res file removed | 2-way resource, adjust applied |
| pet/ViPet → cattle | VM on raw LV; meta LV gone; data LV kept | VM undefined, LVs + res removed | resource fully torn down |

## Operator perspective

- **Downtime**: zero. The VM's QEMU process never pauses beyond the
  sub-millisecond blockcopy pivot.
- **Observed latency during sync**: negligible on the primary (writes
  commit when local ACKs; DRBD doesn't wait for peer ACK in protocol C
  *for already-synced regions*). During initial sync to a fresh peer, the
  disk shows `Inconsistent` on the peer until the resync catches up.
- **Rollback**: every transition has an inverse. The downgrade paths are
  implemented and tested. Flipping a checkbox on and off is safe.
