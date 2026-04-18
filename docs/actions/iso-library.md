# ISO library (upload, list, mount)

Bedrock keeps install ISOs (Windows, Linux, anything bootable) on the
mgmt node and makes them visible at an **identical path** on every
cluster node via NFS auto-mount. VM creation references them by local
file path — `virt-install --cdrom /mnt/isos/<name>.iso` works wherever
the VM is being built.

## Layout

```
  mgmt node
  ─────────
  /opt/bedrock/iso/              ← the actual files live here
        my-windows-server.iso
        ubuntu-22.04.iso
        alpine-3.21-standard.iso
        README.md
  /mnt/isos/       ── bind-mount (ro) ──► /opt/bedrock/iso
  (same path, same view, no NFS loop)

  NFS server: :2049  →  exports /opt/bedrock/iso to
                          192.168.2.0/24 (ro)
                          10.99.0.0/24 (ro)

  compute nodes (bedrock-sim-2, bedrock-sim-3, …)
  ──────────────────────────────────────────────
  /mnt/isos/       ── NFS-automount ──► <mgmt-ip>:/opt/bedrock/iso
  (systemd automount, on-demand; idle timeout 5 min)
```

**The result**: every node resolves `/mnt/isos/foo.iso` to the same bytes.
Bedrock's VM-create code uses `/mnt/isos/` exclusively, so future
cross-node VM provisioning works without any path rewriting.

## Uploading an ISO

**Via the dashboard** — recommended:

1. Sidebar → `ISOs` (or direct URL `/isos`)
2. Click `Choose .iso file`
3. Progress bar shows upload %; on completion the list refreshes

**Via shell** — equally valid for big files or scripted uploads:

```bash
scp my-iso.iso root@<mgmt>:/opt/bedrock/iso/
```

The dashboard lists both paths in the same table.

## Backend

| Endpoint | Method | Body | Returns |
|---|---|---|---|
| `/api/isos` | GET | — | `[{name, size_bytes}, ...]` |
| `/api/isos/upload` | POST | multipart/form-data with `file` field | `{status, name, size_bytes}` |
| `/api/isos/{name}` | DELETE | — | `{status, name}` |

Uploads stream in 1 MB chunks straight to disk, so memory stays bounded
even for multi-GB Windows ISOs. `python-multipart` is the one extra
pip dependency this adds.

Path traversal is blocked: the server always does `Path(name).name`
before writing — `../../etc/passwd.iso` becomes `passwd.iso`.

## systemd units (auto-generated)

On the mgmt node, `mnt-isos.mount`:

```ini
[Unit]
Description=Bedrock ISO library (bind mount)

[Mount]
What=/opt/bedrock/iso
Where=/mnt/isos
Type=none
Options=bind,ro

[Install]
WantedBy=multi-user.target
```

On every compute node, `mnt-isos.mount` + `mnt-isos.automount`:

```ini
# mnt-isos.mount
[Unit]
Description=Bedrock ISO library (NFS)
After=network-online.target
Wants=network-online.target

[Mount]
What=<mgmt-ip>:/opt/bedrock/iso
Where=/mnt/isos
Type=nfs
Options=ro,nolock,soft,_netdev

# mnt-isos.automount
[Automount]
Where=/mnt/isos
TimeoutIdleSec=300
```

Automount means: the NFS mount only happens when something actually
touches `/mnt/isos`. After 5 minutes of inactivity it unmounts. If the
mgmt node is briefly unreachable, a subsequent touch re-triggers the
mount — no manual intervention.

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| `--cdrom /mnt/isos/foo.iso: No such file` on mgmt | Bind mount not up | `systemctl start mnt-isos.mount` |
| same on a compute node | NFS automount blocked by firewall or mgmt down | `systemctl status mnt-isos.automount`; check `showmount -e <mgmt-ip>` |
| `Permission denied` writing to `/mnt/isos` | Mount is read-only by design | Upload to `/opt/bedrock/iso/` on mgmt (via dashboard or scp), not to `/mnt/isos` |
| ISO visible on mgmt but not compute | NFS export scope mismatches node's IP | Check `/etc/exports.d/bedrock-iso.exports`; the source subnets must cover every cluster node |

## Related

- [`vm-create.md`](vm-create.md) — how `--cdrom /mnt/isos/<name>` lands
  in the virt-install command.
- [`../reference/files.md`](../reference/files.md) — all paths Bedrock
  writes on disk.
