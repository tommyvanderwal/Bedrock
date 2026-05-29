# Files Bedrock reads and writes

Canonical list of every file any Bedrock component touches at runtime.
Grouped by who owns the file and what changes it.

## Per-node identity and cluster state

Cluster topology no longer lives in a local file. It lives in **rqlite**
(Raft-replicated SQLite: tables `nodes`, `vms`, `drbd_resources`,
`cluster_info`, …) and is read via `cluster_state.load_cluster()` (read-level
`none`, so it works even without quorum). The only per-node local cluster file
is `state.json`.

| Path | Owner | Shape | Written by | Read by |
|---|---|---|---|---|
| `/etc/bedrock/state.json` | all | JSON | `bedrock bootstrap` (init hw section), `bedrock init`/`join` (cluster_*); crash-durable via fsync + atomic rename | `bedrock status`, `bedrock vm *`, `installer/lib/*` |
| `/etc/bedrock/cluster.key` | all | 32-byte binary | mgmt master at `bedrock init` (`daemon_setup.write_cluster_key`); shipped to joiners by the join handshake | witness AEAD auth, bedrock-net signed multicast probe (HMAC-SHA256) |
| `/etc/bedrock/installer.env` | all | `KEY=val` | `install.sh` | `bedrock` CLI `get_repo()` |
| `/etc/systemd/system/bedrock-d.service` | all | systemd unit | `install.sh` | systemd at boot; starts `/usr/local/bin/bedrock-d` (netd thread + mgmt/orchestrator asyncio) |
| `/etc/NetworkManager/system-connections/bedrock-mesh-<nic>.nmconnection` | all | NM keyfile | `nmcli con add` invoked by `bedrock-d`'s netd `ensure_link_local` | NetworkManager — drives RFC 3927 link-local on the mesh NIC |

`state.json` shape:

```json
{
  "bootstrap_done": true,
  "hardware": { "hostname": "...", "cpu_model": "...", "vcpus": 4,
                "ram_mb": 15988, "nics": [...], "root_disk_gb": 99,
                "has_virt": true },
  "cluster_name": "bedrock-e2e",
  "cluster_uuid": "abcd-...",
  "role": "mgmt+compute" | "compute",
  "node_id": 0,
  "node_name": "bedrock-sim-1.bedrock.local",
  "witness_host": "self" | "<external-host>",
  "mgmt_ip": "192.168.2.152",
  "mgmt_url": "https://192.168.2.152:8443",
  "drbd_ip": "",                            // legacy; mesh layer ignores
  "loopback_ip": "100.X.Y.1"                // /32 on lo, cluster identity
}
```

Cluster-wide topology (the `nodes` map, VM rows, DRBD resources, the
bedrock-net path table) lives in rqlite, not in any local file. Read it with
`cluster_state.load_cluster()`, which returns the same dict shape the old
`cluster.json` projection used to have.

## Binaries and mgmt application

Installed via `dashboard_install.install_dashboard()` (every node serves the
dashboard) and `exporters.install()` (init + join). The full observability
binary set (`exporters.OBS_BINS`) lands on **every** node; which ones actually
run is decided at runtime by `observability.reconcile()` from `obs_backends`:
the agents run everywhere, the VictoriaMetrics/VictoriaLogs backends only on
the two designated nodes.

| Path | Source | Owner |
|---|---|---|
| `/opt/bedrock/bin/vmagent` | `<repo>/binaries/vmagent` | all nodes (runs everywhere) |
| `/opt/bedrock/bin/vlagent` | `<repo>/binaries/vlagent` | all nodes (runs everywhere) |
| `/opt/bedrock/bin/victoria-metrics` | `<repo>/binaries/victoria-metrics` | all nodes (runs only on the 2 metrics backends) |
| `/opt/bedrock/bin/victoria-logs` | `<repo>/binaries/victoria-logs` | all nodes (runs only on the 2 logs backends) |
| `/opt/bedrock/bin/vmbackup`, `/opt/bedrock/bin/vmrestore` | `<repo>/binaries/{vmbackup,vmrestore}` | all nodes (backend seed only) |
| `/opt/bedrock/bin/node_exporter` | `<repo>/binaries/node_exporter` | all nodes |
| `/opt/bedrock/bin/vm_exporter.py` | `<repo>/binaries/vm_exporter.py` | all nodes |
| `/opt/bedrock/mgmt/app.py` | `<repo>/mgmt.tar.gz` → extract | all nodes |
| `/opt/bedrock/mgmt/ws.py` | same | all nodes |
| `/opt/bedrock/mgmt/victoria.py` | same | all nodes |
| `/opt/bedrock/mgmt/vm_exporter.py` | same | all nodes (also dup at bin/) |
| `/opt/bedrock/mgmt/novnc/*` | same | all nodes |
| `/opt/bedrock/mgmt/ui/build/*` | same | all nodes |

Updates: re-run `bedrock init` / replace files + `systemctl restart`.
There is no OTA mechanism yet.

## Runtime data

| Path | Written by | Rotation / retention |
|---|---|---|
| `/opt/bedrock/data/vm/` | VictoriaMetrics (on the 2 designated metrics backends) | 90 d retention |
| `/opt/bedrock/data/vl/` | VictoriaLogs (on the 2 designated logs backends) | 90 d retention |
| `/opt/bedrock/scrape.yml` | `write_scrape_config()` (rebuilt from rqlite cluster state) | regenerated every time |
| `/mnt/bedrock/iso/` | operator (via dashboard `/isos` or scp through the FUSE mount) | never auto-rotated |
| `/opt/bedrock/iso/` | `bedrock init` (virtio-win.iso staging only) | one-time seed into the filer via `seaweedfs.seed_iso_library` |
| `/etc/bedrock/vm_inventory.json` | `save_inventory()` in mgmt/app.py | per-VM priority + creation metadata |
| `/var/lib/bedrock/alpine.qcow2` | `bedrock_d/vm/create.py` (saga executor, on the VM's host) | cached per node, never rotated |
| `/var/lib/bedrock-vg-extra.img` | `ensure_vg()` in tier_storage.py (only when the VG is short on space) | sparse loop PV, `max(min_mb*4, 4096)` MB, attached by `bedrock-vg-loop.service` |

## ISO mount points (identical on every node)

| Path | Node | Source | Mode |
|---|---|---|---|
| `/mnt/bedrock/` | every node | SeaweedFS FUSE mount (filer namespace) | rw |
| `/mnt/bedrock/iso/` | every node | `/iso/` collection in the filer namespace | rw via the FUSE mount |

Backing: SeaweedFS volume servers + filer. Replication for `/iso/`
is node-count-aware (000 at N=1, 001 at N≥2; see
`installer/lib/seaweedfs.py::init_collections`).

Mount unit: `/etc/systemd/system/bedrock-fuse-mount.service` (FUSE
mount via `weed mount -filer=… -dir=/mnt/bedrock`). No NFS server,
no bind mount, no automount — every node mounts the same filer
directly.

## Systemd units

Shipped from `installer/configs/*.service`. The dashboard, exporters, and the
netd mesh/election/witness loop all run inside the single `bedrock-d` daemon —
there are no separate `bedrock-mgmt`, `node-exporter`, or `vm-exporter` units
anymore. Observability is HA dual-backend, with its units **generated and
converged at runtime** by `installer/lib/observability.py::reconcile()` (called
on every rqlite revision), not hand-installed `.service` files:
`bedrock-vmagent` / `bedrock-vlagent` run on every node (dual-writing to the
backends), while `bedrock-vm` / `bedrock-vl` run only on the two designated
metrics/logs backends (`obs_backends.metrics` / `obs_backends.logs`).

| Unit | On which nodes | Role | Auto-start at boot? |
|---|---|---|---|
| `bedrock-d.service` | all | unified daemon: netd thread + mgmt/orchestrator asyncio | yes (`multi-user.target`) |
| `bedrock-rqlited.service` | all | per-node rqlite (consensus foundation) | yes (`multi-user.target`) |
| `bedrock-rqlited-arbiter.service` | `.254` holder | arbiter rqlite voter on the `.254` VIP | no — started by `bedrock-d` boot/arbiter takeover |
| `bedrock-weed-master.service` | `min(3,N)` nodes | SeaweedFS master (Raft) | no — `bedrock-d` boot orchestrator |
| `bedrock-weed-volume.service` | all | SeaweedFS volume | no — boot orchestrator |
| `bedrock-weed-filer.service` | `.254` holder | SeaweedFS filer (DRBD-backed) | no — boot orchestrator |
| `bedrock-weed-s3.service` | all | SeaweedFS S3 gateway | no — boot orchestrator |
| `bedrock-fuse-mount.service` | all | FUSE mount of the filer root at `/mnt/bedrock` | enabled (after filer up) |
| `bedrock-mdns.service` | all | mDNS responder | yes (`multi-user.target`) |
| `bedrock-redirect.service` | all | HTTP :80 → HTTPS :8443 redirector | yes (`multi-user.target`) |
| `bedrock-cert-refresh.service` | all | TLS cert renewal | timer/oneshot |
| `bedrock-vg-loop.service` | testbed/small disks | loop-PV for VG headroom | `local-fs-pre.target` |
| `bedrock-vmagent.service` | all | VictoriaMetrics agent (dual-writes to both metrics backends) | runtime-generated by `observability.reconcile()` |
| `bedrock-vlagent.service` | all | VictoriaLogs agent (syslog `:5140`, dual-writes to both logs backends) | runtime-generated by `observability.reconcile()` |
| `bedrock-vm.service` | 2 metrics backends | VictoriaMetrics backend (`:8428`) | runtime-generated; started post-seed gate |
| `bedrock-vl.service` | 2 logs backends | VictoriaLogs backend (`:9428`, syslog `:5141`) | runtime-generated by `observability.reconcile()` |

## DRBD files

Per-resource `/etc/drbd.d/vm-<name>-disk0.res`, one per VM with HA:

- **Written by**: `_write_drbd_res()` in mgmt/app.py (convert path) or
  `_create_pet/_create_vipet` in installer/lib/vm.py (create path).
- **Format**: DRBD 9 text config with `on <node>` blocks, a
  `connection-mesh` (3-way) or single `connection` (2-way), external
  `meta-disk /dev/.../vm-X-disk0-meta` (convert path) or internal
  (legacy create path).
- **Removed by**: `rm -f /etc/drbd.d/vm-<name>-disk0.res` on downgrade
  to cattle or VM delete.

`global_common.conf` is left at its ELRepo package default.

`/etc/modules-load.d/drbd.conf` (one line: `drbd`) is written during
`bedrock bootstrap` by `installer/lib/packages.py` so systemd's
`systemd-modules-load.service` loads the DRBD kernel module at every
boot — no `modprobe drbd` needed at runtime.

## SSH / cluster identity

| Path | Who writes | Purpose |
|---|---|---|
| `/root/.ssh/id_ed25519[.pub]` | `ssh-keygen` on first `bedrock init`/`join` (or test e2e script) | Per-node identity; pubkey must exist in every peer's `authorized_keys` |
| `/root/.ssh/authorized_keys` | operator (or future `bedrock join` auto-push) | Every peer's pubkey, deduplicated |
| `/root/.ssh/known_hosts` | `ssh-keyscan` at join time + first-connect `accept-new` | Every peer's host ed25519 key, by mgmt-LAN and DRBD-ring IPs |
| `/root/.ssh/config` | `configure_base()` in os_setup.py | `Host 192.168.* 10.* bedrock-*` → `StrictHostKeyChecking=accept-new` |

## What's **not** written by Bedrock

- `/etc/libvirt/*`: left at distro defaults.
- `/etc/lvm/lvm.conf`: default.
- `/etc/selinux/config`: only the `SELINUX=` value is changed to
  `permissive` (bootstrap).
- `/etc/rsyslog.conf`: untouched (syslog forwarding is a follow-up).

## Secrets

After the v0.1 secrets sweep, no secrets are in the tracked tree.
Runtime sensitive values:

| Environment variable | Set where | Used for |
|---|---|---|
| `BEDROCK_SSH_PASS` | operator shell or `bedrock-d.service` drop-in | paramiko password fallback when key auth fails (dev/lab only) |
| `BEDROCK_SIM_PASSWD_HASH` | operator shell before `testbed/spawn.py` | root password hash for sim-node cloud-init; empty → SSH-key-only |
| `BEDROCK_WITNESS_URL` | operator shell or mgmt env | override the default witness probe URL |

Production clusters should not set `BEDROCK_SSH_PASS` at all — rely on
SSH key mesh.
