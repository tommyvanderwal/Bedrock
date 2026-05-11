# Files Bedrock reads and writes

Canonical list of every file any Bedrock component touches at runtime.
Grouped by who owns the file and what changes it.

## Per-node identity and cluster state

| Path | Owner | Shape | Written by | Read by |
|---|---|---|---|---|
| `/etc/bedrock/state.json` | all | JSON | `bedrock bootstrap` (init hw section), `bedrock init`/`join` (cluster_*) | `bedrock status`, `bedrock vm *`, `installer/lib/*` |
| `/etc/bedrock/cluster.json` | all | JSON | mgmt master via `save_cluster()` + orchestrator's view_builder fold; replicated to followers via the bedrock-rust log | mgmt dashboard, `bedrock vm create` peer selection, `bedrock-net` for cluster prefix |
| `/etc/bedrock/cluster.key` | all | 32-byte binary | mgmt master at `bedrock init` (`daemon_setup.write_cluster_key`); replicated to joiners via register response | bedrock-rust (witness AEAD auth), bedrock-net (signed multicast probe HMAC) |
| `/etc/bedrock/daemon.toml` | all | TOML | orchestrator subscriber's `daemon_setup.render_from_snapshot` on every relevant log entry | bedrock-rust on startup + after each `systemctl restart` triggered by toml hash change |
| `/etc/bedrock/installer.env` | all | `KEY=val` | `install.sh` | `bedrock` CLI `get_repo()` |
| `/etc/systemd/system/bedrock-net.service` | all | systemd unit | `install.sh` | systemd at boot; starts `/usr/local/bin/bedrock-net` |
| `/etc/NetworkManager/system-connections/bedrock-mesh-<nic>.nmconnection` | all | NM keyfile | `nmcli con add` invoked by bedrock-net's `ensure_link_local` | NetworkManager — drives RFC 3927 link-local on the mesh NIC |

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
  "drbd_ip": "",                            // legacy; mesh layer ignores
  "loopback_ip": "100.X.Y.1"                // /32 on lo, cluster identity
}
```

`cluster.json` shape:

```json
{
  "cluster_name": "bedrock-e2e",
  "cluster_uuid": "abcd-...",
  "nodes": {
    "bedrock-sim-1": {
      "host":        "192.168.2.152",
      "drbd_ip":     "",                    // legacy
      "loopback_ip": "100.X.Y.1",           // /32 cluster identity
      "role":        "mgmt+compute",
      "cockpit":     "https://192.168.2.152:9090",
      "pubkey":      "ssh-ed25519 ..."
    },
    ...
  },
  "paths": {
    // canonical-keyed bedrock-net path table
    "bedrock-sim-1|enp3s0|bedrock-sim-2|enp3s0": {
      "node_a": "bedrock-sim-1", "nic_a": "enp3s0",
      "link_addr_a": "169.254.10.20",
      "node_b": "bedrock-sim-2", "nic_b": "enp3s0",
      "link_addr_b": "169.254.30.40",
      "speed_mbps": 0, "rtt_us": 0,
      "observed_at": 1778409620.17, "up_since": 1778409620.17
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
| `/opt/bedrock/iso/` | operator (via dashboard `/isos` or scp) | never auto-rotated |
| `/etc/bedrock/vm_inventory.json` | `_vm_create` and `_vm_delete` in mgmt | per-VM priority + creation metadata |
| `/var/lib/bedrock/alpine.qcow2` | `_download_alpine_on_node()` in vm.py | cached per node, never rotated |
| `/var/lib/bedrock-vg.img` | `_ensure_thin_pool()` (testbed only) | 20 GB loop file for synthetic VG |

## ISO mount points (identical on every node)

| Path | Node | Source | Mode |
|---|---|---|---|
| `/opt/bedrock/iso/` | mgmt | local directory | rw (writable by mgmt only) |
| `/mnt/isos/` | mgmt | bind-mount of `/opt/bedrock/iso` | ro |
| `/mnt/isos/` | compute | NFS automount of `<mgmt>:/opt/bedrock/iso` | ro, idle-timeout 5 min |

Mount units: `/etc/systemd/system/mnt-isos.mount` (bind or NFS) and,
on compute nodes, `/etc/systemd/system/mnt-isos.automount`.

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
| `BEDROCK_SSH_PASS` | operator shell or `bedrock-mgmt.service` drop-in | paramiko password fallback when key auth fails (dev/lab only) |
| `BEDROCK_SIM_PASSWD_HASH` | operator shell before `testbed/spawn.py` | root password hash for sim-node cloud-init; empty → SSH-key-only |
| `BEDROCK_WITNESS_URL` | operator shell or mgmt env | override the default witness probe URL |

Production clusters should not set `BEDROCK_SSH_PASS` at all — rely on
SSH key mesh.
