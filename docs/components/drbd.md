# DRBD 9 in Bedrock

DRBD is the block-level replication layer under every pet and ViPet VM.
Bedrock uses DRBD 9 (`kmod-drbd9x` / `drbd9x-utils` from ELRepo),
configured with external metadata and `--max-peers=7` so resources can
grow and shrink peers online without full resync.

## Key configuration choices

### ⚠ Silent-truncation hazard (read this first)

DRBD's most dangerous failure mode is **silent device truncation**.
If the external meta-disk is too small for the data size × max-peers,
DRBD does **not** error; it quietly shrinks the effective `/dev/drbdN`
to whatever the meta can index. `blockdev --getsize64 /dev/drbdN`
then returns *less* than the backing LV. `virsh blockcopy --pivot`
fails with `Copy failed` at 0 % because destination < source.

Seen in the wild on this project: a 40 GB backing LV produced an
18.13 GB `/dev/drbdN` because the meta LV was 4 MB (fine for ≤ 2 GB
data). The code change fixing this is the sizing formula below; the
code change ensuring **a future regression can never be silent** is a
runtime assertion in `_vm_convert_upgrade` right after `drbdadm up`:

```python
drbd_bytes = _lv_bytes(src["host"], f"/dev/drbd{minor}")
if drbd_bytes != src_size:
    raise HTTPException(500,
        f"DRBD silent-truncation guard tripped on {resource}: "
        f"/dev/drbd{minor} = {drbd_bytes} bytes, backing LV = {src_size} "
        f"bytes (delta {src_size - drbd_bytes} bytes). Meta LV almost "
        f"certainly too small — check meta_mb formula.")
```

If this assertion ever trips, **stop**. The conversion will have been
aborted before any data movement; nothing has been corrupted. Fix the
root cause (most likely the meta-LV sizing formula or a regression
that reverted to internal meta) before re-trying.

### External meta-disk

Every resource has a **separate** small thin LV holding DRBD metadata,
referenced as `meta-disk /dev/<vg>/vm-<name>-disk0-meta`. Allocation
scales with data size:

```
  meta_mb = max(32, 32 + data_gb)
```

i.e. 32 MB baseline + 1 MB per GB of data. A 40 GB disk gets a 72 MB
meta LV; a 10 GB disk gets 42 MB. Meta LVs are thin-provisioned on the
same pool so the actual block footprint is a few MB — the size just
defines the *ceiling*.

Why: DRBD internal metadata (the default) eats ~128 KB from the tail of
the underlying data LV, making `/dev/drbdN` strictly smaller than the
underlying LV. `virsh blockcopy` then refuses to pivot into it
("destination is smaller than source"). External meta keeps the DRBD
device byte-for-byte the same size as the data LV — **provided the meta
LV is big enough**. DRBD doesn't error on an undersized meta LV; it
silently truncates the effective device size to what fits, and
subsequent `blockcopy --pivot` fails with "Copy failed" at 0 % (the
destination is still smaller than the source). The formula above
gives plenty of headroom for max-peers=7.

### `--max-peers=7`

At `drbdadm create-md` time Bedrock always passes `--max-peers=7`,
reserving bitmap slots in the meta-disk for up to 7 peers. Without
this, adding a 3rd peer to a 2-way resource later hits `Not enough
free bitmap slots` and the only recovery is a full metadata rewrite.

### Protocol C + conservative after-split-brain policy

```
  net {
      allow-two-primaries no;
      after-sb-0pri  discard-zero-changes;
      after-sb-1pri  discard-secondary;
      after-sb-2pri  disconnect;
  }
```

- **Protocol C** (set per resource): writes ACKed only after peer ACK
  arrives. Synchronous, strongest consistency guarantee.
- **`allow-two-primaries no`** in steady state; the migrate path toggles
  it to `yes` for the duration of a live-migrate, then toggles back.
- **After split-brain policies** prefer Secondary/no-change side if
  automatable; otherwise disconnect and require operator resolution.

## Resource naming + numbering

- **Resource name**: `vm-<name>-disk0`. Single disk per VM today.
- **DRBD device**: `/dev/drbd<minor>`, minor picked as the next free
  integer in [1000, 1900]. Starts at 1000 — leaves room below 1000
  for legacy / manual test resources.
- **TCP port**: `7000 + minor`. For minor 1000 → port 8000. Multiple
  resources on the same ring use different ports.
- **Data LV**: `/dev/<vg>/vm-<name>-disk0` (vg = `almalinux` by default).
- **Meta LV**: `/dev/<vg>/vm-<name>-disk0-meta`, 4 MB thin.

## Resource config example (3-way ViPet)

```text
resource vm-webapp1-disk0 {
    protocol C;
    net { allow-two-primaries no; after-sb-0pri discard-zero-changes;
          after-sb-1pri discard-secondary; after-sb-2pri disconnect; }
    on bedrock-sim-1.bedrock.local {
        node-id 0;
        device /dev/drbd1000;
        disk /dev/almalinux/vm-webapp1-disk0;
        address 10.99.0.10:8000;
        meta-disk /dev/almalinux/vm-webapp1-disk0-meta;
    }
    on bedrock-sim-2.bedrock.local { node-id 1; ... 10.99.0.11:8000; ... }
    on bedrock-sim-3.bedrock.local { node-id 2; ... 10.99.0.12:8000; ... }
    connection-mesh {
        hosts bedrock-sim-1.bedrock.local
              bedrock-sim-2.bedrock.local
              bedrock-sim-3.bedrock.local;
    }
}
```

Written by `_gen_drbd_res()` in mgmt/app.py and landed on every node
via `_write_drbd_res()` (base64-over-SSH, idempotent).

## Lifecycle under Bedrock

```
   convert cattle→pet         create peer LVs + meta LV
                              write .res on both nodes
                              drbdadm create-md --force --max-peers=7
                              drbdadm up
                              drbdadm primary --force (on src)
                              virsh blockcopy ... --pivot  ← VM now on /dev/drbdN

   convert pet→vipet          same, but add a 3rd peer, connection-mesh,
                              drbdadm adjust on existing nodes + up on new

   convert vipet→pet          drbdadm down + wipe-md on dropped
                              drbdsetup disconnect/del-peer on kept hosts
                              rewrite .res for 2 peers
                              drbdadm adjust on remaining

   convert pet/vipet→cattle   virsh blockcopy back to raw LV + pivot
                              drbdadm down + wipe-md everywhere
                              rm .res + lvremove peer LVs

   live migrate               allow-two-primaries=yes on both
                              drbdadm primary on dst
                              virsh migrate --live ... --migrateuri tcp://<drbd-ip>
                              drbdadm secondary on src
                              allow-two-primaries=no on both

   power loss                 (see scenarios/power-loss-*.md)
```

## States you'll see in `drbdadm status`

```
  vm-foo-disk0 role:Primary           ← who's writing
    disk:UpToDate open:yes            ← local state, VM has it open
    bedrock-sim-2.bedrock.local role:Secondary
      peer-disk:UpToDate              ← peer in sync
    bedrock-sim-3.bedrock.local role:Secondary
      replication:SyncSource peer-disk:Inconsistent done:22.10
                                      ← pushing updates to this peer
                                      (3-way just added, catching up)
```

Roles: `Primary`, `Secondary`. Disk states: `UpToDate`,
`Inconsistent` (during initial sync or after split-brain discard),
`Outdated` (disconnected long enough to lose the generation race),
`DUnknown` (peer unreachable). Replication state: `Established`,
`SyncSource`, `SyncTarget`, `VerifyS/VerifyT`, `PausedSync*`.

## Observability

- **kernel ring buffer** (`dmesg`, `journalctl -k`): state changes,
  resync events, split-brain detection — noisy but authoritative.
- **`vm_exporter`** parses `drbdsetup status --json` and exports
  `drbd_resource_role`, `drbd_disk_state`, `drbd_sync_percent` per
  resource + peer pair.
- **dashboard DRBD tile** on /vm/<name>: shows role, disk, peer disk,
  sync percent — updated every 3 s by the state push loop.

## Operator command reference

```bash
# single-resource status
drbdadm status vm-foo-disk0

# full cluster-wide status (pipeable JSON)
drbdsetup status --json

# force a resync from this node outward (after a known divergent peer)
drbdadm -- --discard-my-data connect vm-foo-disk0   # on loser
drbdadm connect vm-foo-disk0                        # on winner

# bring a resource up from scratch (after manual .res edit)
drbdadm adjust vm-foo-disk0

# pause / resume sync (useful during maintenance)
drbdadm pause-sync vm-foo-disk0
drbdadm resume-sync vm-foo-disk0
```

## Known limitations

- **No auto-promote after primary loss** (yet). The failover
  orchestrator (`bedrock-failover.py`) is the intended solution and is
  validated on the physical lab; the sim cluster today requires manual
  `drbdadm primary` + `virsh start` on a surviving peer.
- **4 MB meta LV is oversized but cheap** — thin-provisioned, so the
  actual block usage is < 32 KB.
- **`--max-peers=7`** allows scaling to 7-way without re-creating meta,
  but the convert code only has paths for pet (2-way) and ViPet (3-way).
  Growing past 3 is left to a future operator who sets up DRBD
  manually; Bedrock's validator is 3-node.
