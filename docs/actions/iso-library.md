# ISO library (upload, list, mount)

Bedrock keeps install ISOs (Windows, Linux, anything bootable) on
the cluster-wide SeaweedFS volume servers, surfaced at an
**identical path** on every node via the SeaweedFS FUSE mount.
VM creation references them by local file path —
`virt-install --cdrom /mnt/bedrock/iso/<name>.iso` works on
whichever node is building the VM.

## Layout

```
  every node
  ──────────
  /mnt/bedrock/             ← SeaweedFS FUSE mount, identical
                              on every cluster node
        iso/                ← /iso/ collection
              my-windows-server.iso
              ubuntu-22.04.iso
              alpine-3.21-standard.iso
              virtio-win.iso        ← always attached, never picked
        scratch/            ← /scratch/ collection (no replication)
        templates/          ← /templates/ collection
        snapshots/          ← /snapshots/ collection
        backups/            ← /backups/ collection (replication=002)

  SeaweedFS replication for /iso/ at N=1: 000 (single copy)
  SeaweedFS replication for /iso/ at N≥2: 001 (two copies, different node)
  See installer/lib/seaweedfs.py::init_collections.
```

Writes go through the local FUSE mount → filer → assigned volume
servers per collection policy. Every node sees the same files;
delete on any node removes cluster-wide. No NFS, no bind mount, no
per-node sync step.

## `virtio-win.iso` — always attached, never selected

When `bedrock init` runs, it fetches Red Hat's signed virtio-win
ISO into the ISO library (~750 MB, one-time). The mgmt node's
`_vm_create` automatically attaches this ISO as a **second CDROM**
(SATA bus) on every new VM that uses an install ISO. For Windows
Setup it's the source of `viostor` (disk) and `NetKVM` (network)
drivers — click "Load driver" during Setup and point it at the
virtio-win CDROM. For Linux installs the extra CDROM is harmless
and ignored.

The driver ISO is hidden from the "New VM" install-ISO dropdown so
no one accidentally boots from it. It still appears in the `/isos`
dashboard list alongside user-uploaded ISOs; deleting it just
makes the next `bedrock init` re-fetch on its next run.

## Uploading an ISO

**Via the dashboard** — recommended:

1. Sidebar → `ISOs` (or direct URL `/isos`)
2. Click `Choose .iso file`
3. Progress bar shows upload %; the file streams in 1 MB chunks
   through the dashboard API → FUSE → SeaweedFS → all nodes

**Via shell** — equally valid for big files or scripted uploads:

```bash
scp my-iso.iso root@<any-node>:/mnt/bedrock/iso/
```

Any node works; SeaweedFS replicates per the `/iso/` collection
policy. The dashboard lists files written either way.

## Backend

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/api/isos` | GET | — | `[{name, size_bytes}, ...]` |
| `/api/isos` | POST | multipart/form-data with `file` field | `{status, name, size_bytes}` |
| `/api/isos/{name}` | DELETE | — | `{status, name}` |

Uploads stream in 1 MB chunks straight to the FUSE mount, so memory
stays bounded even for multi-GB Windows ISOs. `python-multipart` is
the one extra pip dependency this adds.

Path traversal is blocked: the server always does `Path(name).name`
before writing — `../../etc/passwd.iso` becomes `passwd.iso`.

## How files reach every node

There's no separate mount unit to manage. The SeaweedFS FUSE mount
is part of every node's normal startup:

```
  systemd unit                what it does
  ──────────────────────────  ──────────────────────────────────────
  bedrock-weed-master         (3-Raft master subset only) — directory
                              + volume assignment authority
  bedrock-weed-volume         every node — stores volume bytes
  bedrock-weed-filer          (current arbiter host) — POSIX
                              namespace on top of volumes
  bedrock-weed-s3             same — S3-compatible front-end
  bedrock-weed-mount          every node — mounts the filer at
                              /mnt/bedrock via FUSE
```

When a node loses connectivity, its local FUSE mount still
responds to reads as long as it can reach a volume server holding
the requested file. Writes block until a writable volume is
available per replication policy — see L40 / lessons-log for what
happens when replication can't be satisfied at the current cluster
size.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `--cdrom /mnt/bedrock/iso/foo.iso: No such file` | FUSE mount didn't come up (weed-mount unit failed, or filer unreachable) | `systemctl status bedrock-weed-mount`; `findmnt /mnt/bedrock`. Restart the mount unit. |
| Upload hangs / `Input/output error` on close | Replication policy unsatisfiable at current N (e.g. `/iso/` pinned to 001 with N=1 nodes) | Re-run `init_collections` so replication matches cluster size — fixed in commit 7728fb1 |
| File visible on one node but not another | Volume server on the file's home node is unreachable | `weed shell -master <ip>:9333` → `volume.list` to see volume placement |
| Dashboard upload returns 405 Method Not Allowed | UI build is stale and POSTing to a renamed endpoint | Rebuild + republish mgmt.tar.gz — see L40, fixed by build-iso.sh + publish-to-s3.sh running `npm run build` |

## Related

- `docs/storage-architecture.md` — the broader SeaweedFS design
  (collections, replication policies, volume layout).
- `installer/lib/seaweedfs.py` — init / promote / demote helpers,
  including `init_collections` which sets the per-prefix
  replication.
- `mgmt/routes_iso.py` — the three /api/isos endpoints, writing
  through the FUSE mount at `/mnt/bedrock/iso/`.
- `installer/lib/vm.py` and `mgmt/app.py::_vm_create` — how
  `--cdrom /mnt/bedrock/iso/<name>` lands in the virt-install
  command.
