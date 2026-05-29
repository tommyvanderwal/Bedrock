# ISO library (upload, list, mount)

Install ISOs (Windows, Linux, anything bootable) live in the SeaweedFS
filer namespace and surface at an **identical path** on every node via a
shared FUSE mount at `/mnt/bedrock`. VM creation references them by local
file path — `virt-install --cdrom /mnt/bedrock/iso/<name>.iso` works on
whichever node builds the VM (`bedrock_d/vm/create.py` step `virsh_install`).

## Layout

```
  every node
  ──────────
  /mnt/bedrock/             ← SeaweedFS FUSE mount, identical
                              on every cluster node (weed mount of
                              the filer root at filer VIP .254:8888)
        iso/                my-windows-server.iso
                            ubuntu-22.04.iso
                            alpine-3.21-standard.iso
                            virtio-win.iso        ← hidden from the install dropdown
        scratch/
        templates/
        snapshots/
        backups/
```

`init_collections` (`installer/lib/seaweedfs.py`) maps these path prefixes
onto three SeaweedFS collections, picking a replication that the current
cluster size can satisfy (an unsatisfiable factor hangs writes at
volume-assign time):

```
  prefix         collection   replication (by node count N)
  ─────────────  ──────────   ─────────────────────────────
  /scratch/      scratch      000              (1 copy, all N)
  /iso/          standard     N=1 → 000, N≥2 → 001
  /templates/    standard     N=1 → 000, N≥2 → 001
  /snapshots/    standard     N=1 → 000, N≥2 → 001
  /backups/      critical     N=1 → 000, N=2 → 001, N≥3 → 002
```

Writes go through the local FUSE mount → filer → volume servers per the
collection policy. Every node sees the same files; a delete on any node
removes the file cluster-wide. No NFS, no bind mount, no per-node sync.

## `virtio-win.iso`

`bedrock init` (mgmt install) fetches the virtio-win driver ISO (~750 MB,
one-time) into `/opt/bedrock/iso`, then `seed_iso_library` copies it into the
filer at `/mnt/bedrock/iso/virtio-win.iso`. The fetch tries the LAN repo
mirror first, then falls back to the upstream Fedora stable build
(`fedorapeople.org/.../stable-virtio/virtio-win.iso`); a failed fetch only
warns. The ISO carries Windows `viostor` (disk) and `NetKVM` (network)
drivers. The dashboard filters it out of the "New VM" and CDROM-insert
dropdowns (`name !== 'virtio-win.iso'`) so it is never picked as a boot disk;
it still appears in the `/isos` list. Deleting it makes the next `bedrock
init` re-fetch it.

To load the drivers during Windows Setup, insert the driver ISO from the VM
Settings → CDROM control (`POST /api/vms/{name}/cdrom`, action `insert`),
then click "Load driver" and point Setup at it.

## Uploading an ISO

**Via the dashboard** — recommended:

1. Sidebar → `ISOs` (or direct URL `/isos`)
2. Click `Choose .iso file`
3. The browser POSTs the file (multipart) to `/api/isos`; the API writes it
   in 1 MB chunks to the FUSE mount → filer → volume servers, and SeaweedFS
   replicates it to all nodes per the `/iso/` policy. An XHR upload-progress
   bar shows the percentage.

**Via shell** — for big files or scripted uploads:

```bash
scp my-iso.iso root@<any-node>:/mnt/bedrock/iso/
```

Any node works; SeaweedFS replicates per the `/iso/` policy. The dashboard
lists files written either way. The extension is normalised to lowercase
`.iso` (Microsoft ships `.ISO`); the basename is preserved.

## Backend (`mgmt/routes_iso.py`)

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/api/isos` | GET | — | `[{name, size_bytes}, ...]` |
| `/api/isos` | POST | multipart/form-data, `file` field | `{status, name, size_bytes}` |
| `/api/isos/{name}` | DELETE | — | `{status, name}` |

All three operate on `/mnt/bedrock/iso` (`ISO_DIR`). GET lists every
case-insensitive `.iso`. POST writes in 1 MB chunks straight to the FUSE
mount, so memory stays bounded for multi-GB Windows ISOs (multipart parsing
needs `python-multipart`, part of the FastAPI dep set). DELETE blocks path
traversal with `Path(name).name`, so `../../etc/passwd.iso` collapses to
`passwd.iso`.

## How files reach every node

The FUSE mount comes up as its own systemd unit, written by
`seaweedfs.ensure_iso_library_mount()` and named `bedrock-fuse-mount.service`
(a `weed mount` is a long-running FUSE helper, not a one-shot `mount()`
syscall, so it is a Service unit, not a `.mount`). The unit is generated and
enabled during mgmt + agent install, on node join, and re-asserted by the
mgmt orchestrator; it is identical on every node:

```
  weed mount -filer=<filer-VIP>:8888 -dir=/mnt/bedrock -allowOthers -dirAutoCreate
```

It targets the filer VIP (`.254:8888`), not a fixed node, so the mount
string never changes when the arbiter host flips — the VIP moves with the
arbiter and the FUSE client auto-reconnects. The unit starts with
`--no-block` and `Restart=on-failure`; `weed` retries internally until the
filer is reachable, so install never blocks on the mount.

The SeaweedFS units behind it (`installer/configs/bedrock-weed-*.service`):

```
  unit                   runs on                    role
  ─────────────────────  ─────────────────────────  ──────────────────────
  bedrock-weed-master    Raft-3 lowest-octet subset directory + volume
                         (N=1→1, N=2→1, N≥3→3)      assignment authority
  bedrock-weed-volume    every node                 stores volume bytes
  bedrock-weed-s3        every node                 S3 gateway (:8333)
  bedrock-weed-filer     arbiter host (.254)        POSIX namespace (:8888)
```

`bedrock-d` starts volume + s3 + (if elected) master on every node via
`seaweedfs.promote_to_master_volume_host()`; `cluster_arbiter` owns the
filer singleton on `.254` via `promote_to_filer_host()`. Filer metadata is
leveldb3 under `/var/lib/bedrock/cluster/seaweedfs/`, on the cluster-singleton
DRBD volume, so it moves with the arbiter role.

A node that loses connectivity still serves reads from its local FUSE mount
as long as it can reach a volume server holding the requested file. Writes
block until a writable volume satisfies the replication policy.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `--cdrom /mnt/bedrock/iso/foo.iso: No such file` | FUSE mount didn't come up (mount unit failed, or filer VIP unreachable) | `systemctl status bedrock-fuse-mount`; `findmnt /mnt/bedrock`; restart the unit |
| Upload hangs / `Input/output error` on close | Replication policy unsatisfiable at the current N (e.g. `/iso/` at 001 with N=1) | `init_collections` matches replication to N; re-run it after the node count is right |
| File visible on one node but not another | Volume server on the file's home node is unreachable | `weed shell -master <ip>:9333` → `volume.list` to see placement |

## Related

- `docs/storage-architecture.md` — the broader SeaweedFS design
  (collections, replication policies, volume layout).
- `installer/lib/seaweedfs.py` — `init_collections` (path → collection
  policy), `ensure_iso_library_mount` (the FUSE unit), `seed_iso_library`,
  filer promote/demote.
- `mgmt/routes_iso.py` — the three `/api/isos` endpoints, writing through
  the FUSE mount at `/mnt/bedrock/iso/`.
- `bedrock_d/vm/create.py` — step `virsh_install` puts
  `--cdrom /mnt/bedrock/iso/<name>.iso` into the virt-install command.
