# Files Bedrock reads and writes

Canonical list of every file any Bedrock component touches at runtime.
Grouped by who owns the file and what changes it.

## Per-node identity and cluster state

| Path | Owner | Shape | Written by | Read by |
|---|---|---|---|---|
| `/etc/bedrock/state.json` | all | JSON | `bedrock bootstrap` (init hw section), `bedrock init`/`join` (cluster_*) | `bedrock status`, `bedrock vm *`, `installer/lib/*` |
| `/etc/bedrock/cluster.json` | mgmt node | JSON | `save_cluster()` in mgmt/app.py (on register) | mgmt dashboard, `bedrock vm create` peer selection |
| `/etc/bedrock/installer.env` | all | `KEY=val` | `install.sh` | `bedrock` CLI `get_repo()` |

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
  "mgmt_url": "http://192.168.2.152:8080",
  "drbd_ip": "10.99.0.10"
}
```

`cluster.json` shape:

```json
{
  "cluster_name": "bedrock-e2e",
  "cluster_uuid": "abcd-...",
  "nodes": {
    "bedrock-sim-1.bedrock.local": {
      "host": "192.168.2.152",
      "drbd_ip": "10.99.0.10",
      "tb_ip":   "10.99.0.10",
      "eno_ip":  "10.99.0.10",
      "role":    "mgmt+compute",
      "cockpit": "https://192.168.2.152:9090"
    },
    ...
  }
}
```

## Binaries and mgmt application

Installed by `installer/lib/mgmt_install.py` (init) and
`installer/lib/exporters.py` (init + join):

| Path | Source | Owner |
|---|---|---|
| `/opt/bedrock/bin/victoria-metrics` | `<repo>/binaries/victoria-metrics` | mgmt node only |
| `/opt/bedrock/bin/victoria-logs` | `<repo>/binaries/victoria-logs` | mgmt node only |
| `/opt/bedrock/bin/node_exporter` | `<repo>/binaries/node_exporter` | all nodes |
| `/opt/bedrock/bin/vm_exporter.py` | `<repo>/binaries/vm_exporter.py` | all nodes |
| `/opt/bedrock/mgmt/app.py` | `<repo>/mgmt.tar.gz` → extract | mgmt node only |
| `/opt/bedrock/mgmt/ws.py` | same | mgmt node only |
| `/opt/bedrock/mgmt/victoria.py` | same | mgmt node only |
| `/opt/bedrock/mgmt/vm_exporter.py` | same | mgmt node (also dup at bin/) |
| `/opt/bedrock/mgmt/novnc/*` | same | mgmt node only |
| `/opt/bedrock/mgmt/ui/build/*` | same | mgmt node only |

Updates: re-run `bedrock init` / replace files + `systemctl restart`.
There is no OTA mechanism yet.

## Runtime data

| Path | Written by | Rotation / retention |
|---|---|---|
| `/opt/bedrock/data/vm/` | VictoriaMetrics (on mgmt node) | 90 d retention |
| `/opt/bedrock/data/vl/` | VictoriaLogs (on mgmt node) | 90 d retention |
| `/opt/bedrock/scrape.yml` | `save_cluster()` → `write_scrape_config()` on register | regenerated every time |
| `/var/lib/bedrock/alpine.qcow2` | `_download_alpine_on_node()` in vm.py | cached per node, never rotated |
| `/var/lib/bedrock-vg.img` | `_ensure_thin_pool()` (testbed only) | 20 GB loop file for synthetic VG |

## Systemd units

Written by `mgmt_install.install_full()` and `exporters.install()`:

| Unit | On which nodes | ExecStart |
|---|---|---|
| `bedrock-mgmt.service` | mgmt | `/usr/bin/python3 /opt/bedrock/mgmt/app.py` |
| `bedrock-vm.service` | mgmt | `/opt/bedrock/bin/victoria-metrics -storageDataPath=... -promscrape.config=/opt/bedrock/scrape.yml -retentionPeriod=90d -httpListenAddr=:8428` |
| `bedrock-vl.service` | mgmt | `/opt/bedrock/bin/victoria-logs -storageDataPath=... -httpListenAddr=:9428 -syslog.listenAddr.tcp=:5140` |
| `node-exporter.service` | all | `/opt/bedrock/bin/node_exporter --web.listen-address=:9100` |
| `vm-exporter.service` | all | `/usr/bin/python3 /opt/bedrock/bin/vm_exporter.py` |

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
| `BEDROCK_SSH_PASS` | operator shell or `bedrock-mgmt.service` drop-in | paramiko password fallback when key auth fails (dev/lab only) |
| `BEDROCK_SIM_PASSWD_HASH` | operator shell before `testbed/spawn.py` | root password hash for sim-node cloud-init; empty → SSH-key-only |
| `BEDROCK_WITNESS_URL` | operator shell or mgmt env | override the default witness probe URL |

Production clusters should not set `BEDROCK_SSH_PASS` at all — rely on
SSH key mesh.
