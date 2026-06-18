# Files Bedrock reads and writes

Canonical list of every file any Bedrock component touches at runtime,
grouped by who owns the file and what changes it.

Cluster-wide topology lives in **rqlite** (Raft-replicated SQLite: tables
`nodes`, `vms`, `drbd_resources`, `cluster_info`, `tiers`, `paths`,
`witnesses`, …). Code reads it via `cluster_state.load_cluster()` at read level
`none`, so it works without quorum. The only per-node local cluster file is
`/etc/bedrock/cluster.json`, a bootstrap file holding the rqlite peer list (read
by `rqlite_setup --render-env` at boot, before rqlite can report its own peers);
plus `/etc/bedrock/state.json` for this node's identity.

## Per-node identity and cluster state

| Path | Owner | Shape | Written by | Read by |
|---|---|---|---|---|
| `/etc/bedrock/state.json` | all | JSON | `bedrock bootstrap` (hw section), `bedrock init`/`join` (cluster_*); crash-durable via fsync + atomic rename, self-healed from cluster state if lost | `bedrock status`, `bedrock vm *`, `installer/lib/*` |
| `/etc/bedrock/cluster.json` | all | JSON | `bedrock init`/`join` (rqlite peer list) | `rqlite_setup --render-env` at every boot |
| `/etc/bedrock/cluster.key` | all | 32-byte binary | mgmt master at `bedrock init` (`daemon_setup.write_cluster_key`); shipped to joiners by the join handshake | witness AEAD auth (ChaCha20-Poly1305), bedrock-net signed mesh probe (HMAC-SHA256) |
| `/etc/bedrock/installer.env` | all | `KEY=val` | `install.sh` | `bedrock` CLI `get_repo()` (`BEDROCK_REPO=`) |
| `/etc/systemd/system/bedrock-d.service` | all | systemd unit | `install.sh` | systemd at boot; starts `/usr/local/bin/bedrock-d` (netd thread + mgmt/orchestrator asyncio) |
| `/etc/NetworkManager/system-connections/bedrock-mesh-<nic>.nmconnection` | all | NM keyfile | `nmcli con add` from bedrock-d's netd `ensure_link_local` | NetworkManager — drives RFC 3927 link-local (169.254/16) on each mesh NIC |

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
  "loopback_ip": "100.X.Y.1"                // /32 on lo, cluster identity
}
```

It also carries cold-boot recovery fields (e.g. the believed-master marker) read
before rqlite is up. The identity keys (`cluster_uuid`, `node_name`,
`loopback_ip`) are the ones the self-heal path restores from cluster state.

## Binaries and mgmt application

Installed via `dashboard_install.install_dashboard()` (every node serves the
dashboard) and `exporters.install()` (init + join). The full observability
binary set (`exporters.OBS_BINS` = `vmagent`, `vlagent`, `vmbackup`,
`vmrestore`, `victoria-metrics`, `victoria-logs`) lands on **every** node; which
ones actually run is decided at runtime by `observability.reconcile()` from
`obs_backends`: the agents run everywhere, the VictoriaMetrics/VictoriaLogs
backends only on the two designated nodes.

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
| `/opt/bedrock/mgmt/vm_exporter.py` | same | all nodes (also dup at `bin/`) |
| `/opt/bedrock/mgmt/novnc/*` | same | all nodes |
| `/opt/bedrock/mgmt/ui/build/*` | same | all nodes |

Updates: re-run `bedrock init` / replace files + `systemctl restart`. There is
no OTA mechanism.

## Runtime data

| Path | Written by | Rotation / retention |
|---|---|---|
| `/opt/bedrock/data/vm/` | VictoriaMetrics (on the 2 designated metrics backends) | 90 d retention |
| `/opt/bedrock/data/vl/` | VictoriaLogs (on the 2 designated logs backends) | 90 d retention |
| `/opt/bedrock/scrape.yml` | `write_scrape_config()` (rebuilt from rqlite cluster state) | regenerated every time |
| `/mnt/bedrock/iso/` | operator (via dashboard `/isos` or scp through the FUSE mount) | never auto-rotated |
| `/opt/bedrock/iso/` | `bedrock init` (virtio-win.iso staging) | seeded once into the filer via `seaweedfs.seed_iso_library` |
| `/etc/bedrock/vm_inventory.json` | `save_inventory()` in mgmt/app.py | per-VM priority + creation metadata |
| `/var/lib/bedrock/alpine.qcow2` | `bedrock_d/vm/create.py` (saga executor, on the VM's host) | cached per node, never rotated |
| `/var/lib/bedrock-vg-extra.img` | `_ensure_vg_headroom()` in tier_storage.py (only when the VG has < 1 GB free) | sparse loop PV, `max(min_mb*4, 4096)` MB, reattached at boot by `bedrock-vg-loop.service` |

## ISO mount points (identical on every node)

| Path | Node | Source | Mode |
|---|---|---|---|
| `/mnt/bedrock/` | every node | SeaweedFS FUSE mount (filer root) | rw |
| `/mnt/bedrock/iso/` | every node | `/iso/` prefix in the filer namespace | rw via the FUSE mount |

Backing: SeaweedFS volume servers + filer. Replication for `/iso/` is
node-count-aware (`000` at N=1, `001` at N≥2; see
`installer/lib/seaweedfs.py::init_collections`), so an upload never blocks on a
replica count the cluster can't satisfy.

Mount unit: `/etc/systemd/system/bedrock-fuse-mount.service` (`weed mount
-filer=… -dir=/mnt/bedrock`). Every node mounts the same filer root directly —
no NFS server, no bind mount, no automount.

## Systemd units

Shipped from `installer/configs/*.service`, plus the observability units that
`installer/lib/observability.py::reconcile()` **generates and converges at
runtime** (on every rqlite revision) rather than shipping as static files:
`bedrock-vmagent` / `bedrock-vlagent` (every node, dual-writing to the
backends) and `bedrock-vm` / `bedrock-vl` (only on the two designated
metrics/logs backends, `obs_backends.metrics` / `obs_backends.logs`). The
`node-exporter` and `vm-exporter` units are written and enabled by
`exporters.install()`.

| Unit | On which nodes | Role | Auto-start at boot? |
|---|---|---|---|
| `bedrock-d.service` | all | unified daemon: netd thread + mgmt/orchestrator asyncio | yes (`multi-user.target`) |
| `bedrock-rqlited.service` | all | per-node rqlite (consensus foundation, mTLS 4001/4002) | yes (`multi-user.target`) |
| `bedrock-mdns.service` | all | mDNS responder for `bedrock.local` | yes (`multi-user.target`) |
| `bedrock-redirect.service` | all | HTTP `:80` → HTTPS `:8443` redirector | yes (`multi-user.target`) |
| `node-exporter.service` | all | Prometheus node_exporter (`:9100`) | yes (`multi-user.target`) |
| `vm-exporter.service` | all | libvirt VM/DRBD exporter (`:9177`) | yes (`multi-user.target`) |
| `bedrock-cert-refresh.service` | all | TLS cert renewal | timer/oneshot |
| `bedrock-vg-loop.service` | nodes with a vg-extra loop PV | reattach loop PV for VG headroom | `local-fs-pre.target`, `ConditionPathExists` |
| `bedrock-rqlited-arbiter.service` | `.254` holder | arbiter rqlite voter on the `.254` VIP (4011/4012) | no — `bedrock-d` boot/arbiter takeover |
| `bedrock-weed-master.service` | `min(3,N)` lowest-octet nodes | SeaweedFS master (Raft) | no — `bedrock-d` boot orchestrator |
| `bedrock-weed-volume.service` | all | SeaweedFS volume | no — boot orchestrator |
| `bedrock-weed-filer.service` | `.254` holder | SeaweedFS filer (DRBD-backed) | no — boot orchestrator |
| `bedrock-weed-s3.service` | all | SeaweedFS S3 gateway | no — boot orchestrator |
| `bedrock-fuse-mount.service` | all | FUSE mount of the filer root at `/mnt/bedrock` | enabled (after filer up) |
| `bedrock-vmagent.service` | all | VictoriaMetrics agent (dual-writes to both metrics backends) | runtime-generated by `observability.reconcile()` |
| `bedrock-vlagent.service` | all | VictoriaLogs agent (syslog `:5140`, dual-writes to both logs backends) | runtime-generated by `observability.reconcile()` |
| `bedrock-vm.service` | 2 metrics backends | VictoriaMetrics backend (`:8428`) | runtime-generated; started post-seed gate |
| `bedrock-vl.service` | 2 logs backends | VictoriaLogs backend (`:9428`, syslog `:5141`) | runtime-generated by `observability.reconcile()` |

Per-VM DRBD, libvirtd, and the node's VMs are started by the boot orchestrator
(`_start_local_services`, idempotent, role-aware) once a clear quorum role is
known.

## DRBD files

Per-resource `/etc/drbd.d/<resource>.res`, one file per disk (`vm-<name>-disk0`,
`vm-<name>-disk1`, …):

- **Written by**: the VM-create saga via `bedrock_d/vm/create.py` →
  `bedrock_d/vm/drbd_config.py::render` (one `.res` per disk, per peer), or the
  HA-level-change/convert path via `_write_drbd_res()` + `_gen_drbd_res()` in
  mgmt/app.py.
- **Format**: DRBD 9 text config, `protocol C`, `on <node>` blocks with
  `node-id`, full-mesh `connection` blocks (one per pair), **external** metadata
  on a separate thin LV per resource — data at `/dev/bedrock/bedrock-data-<r>`,
  meta at `/dev/bedrock/bedrock-meta-<r>` (`drbdadm create-md --max-peers=7`).
  External meta keeps the DRBD device the same size as the data LV so
  `virsh blockcopy` can pivot 1:1.
- **Removed by**: `bedrock_d/vm/destroy.py` (`drbdadm down` → `wipe-md` →
  `rm -f <res_file_path>` on every peer) on VM delete or HA downgrade to cattle.

`global_common.conf` is left at its ELRepo package default.

`/etc/modules-load.d/drbd.conf` (one line: `drbd`) is written during `bedrock
bootstrap` by `installer/lib/packages.py`, so `systemd-modules-load.service`
loads the DRBD kernel module at every boot — no runtime `modprobe drbd`.

## SSH / cluster identity

| Path | Who writes | Purpose |
|---|---|---|
| `/root/.ssh/id_ed25519[.pub]` | `ssh-keygen` on first `bedrock init`/`join` (or test e2e script) | Per-node identity; pubkey must exist in every peer's `authorized_keys` |
| `/root/.ssh/authorized_keys` | operator / `bedrock join` key fan-out | Every peer's pubkey, deduplicated |
| `/root/.ssh/known_hosts` | `ssh-keyscan` at join + first-connect `accept-new` | Every peer's host ed25519 key, by mgmt-LAN and mesh IPs |
| `/root/.ssh/config` | `configure_base()` in os_setup.py | `Host 192.168.* 10.* bedrock-*` → `StrictHostKeyChecking accept-new`, fixed `UserKnownHostsFile` |

## What Bedrock does **not** write

- `/etc/libvirt/*`: distro defaults.
- `/etc/lvm/lvm.conf`: default.
- `/etc/selinux/config`: only the `SELINUX=` line is set to `permissive`
  (bootstrap, via `os_setup.configure_base`).
- `/etc/systemd/journald.conf.d/50-bedrock-forward.conf`: sets `ForwardToSyslog=yes` so journal entries reach rsyslog (written by `observability.reconcile_journal_forward()`).
- `/etc/rsyslog.d/50-bedrock-vlagent.conf`: forwards syslog (incl. journal) to `127.0.0.1:5140` (local vlagent). Managed by `observability.reconcile_journal_forward()`.

## Secrets

No secrets live in the tracked tree. The runtime cluster secret is
`/etc/bedrock/cluster.key` (32 bytes, see above). One environment override:

| Environment variable | Set where | Used for |
|---|---|---|
| `BEDROCK_SIM_PASSWD_HASH` | operator shell before `testbed/spawn.py` | root password hash for sim-node cloud-init; default `*` (disabled) → SSH-key-only |
