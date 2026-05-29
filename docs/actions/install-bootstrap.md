# Install a node (`curl | bash` → `bedrock bootstrap`)

One-shot script that turns a fresh **AlmaLinux 10 minimal** box into a Bedrock
node. It stages the whole Bedrock payload, installs packages, configures the OS,
and prints the next step (`init` or `join`). It stops short of the cluster —
joining one is a separate subcommand.

**Triggered by:** an operator running, as root:

```bash
# default: rolling dev build from the public Hetzner S3 bucket
curl -fsSL https://bedrock.fsn1.your-objectstorage.com/dev/install.sh | sudo bash

# pin a release, or point at a LAN dev repo:
BEDROCK_REPO=http://192.168.2.145:8000 \
  curl -fsSL ${BEDROCK_REPO}/install.sh | sudo bash
```

`BEDROCK_REPO` defaults to the bucket's `/dev` prefix; pin `/v0.8` (etc.) for a
frozen release, or a `http(s)://` / `file://` URL for a testbed.

The payload staged here is the CLI, the full `lib/` tree, the `bedrock-d`
daemon, the `bedrock_d/` saga package, the `mgmt/` FastAPI+Svelte tree, the
rqlited / weed / kopia binaries, every systemd unit, and the httpx + DRBD
offline caches. `init`/`join` add only the observability binaries (node_exporter,
vmagent, vlagent, victoria-metrics/logs), fetched from `${BEDROCK_REPO}/binaries`.

**Source files:** `installer/install.sh`, `installer/bedrock` (subcommand
`bootstrap`), `installer/lib/{hardware,os_setup,packages}.py`.

## Preconditions

- Fresh AlmaLinux 10 (the script warns but continues on other RHEL-likes).
- Root shell.
- `BEDROCK_REPO` reachable (HEAD on `${BEDROCK_REPO}/install.sh` — a probe on a
  known file, since S3 buckets 403 on a bare-prefix listing).
- `python3` available (dnf-installed if missing).

## Sequence

```
  T=0  ┌──── install.sh (bash) ──────────────────────────────────────┐
       │  1. assert root; warn if not AlmaLinux                       │
       │  2. mute console kernel log to KERN_WARNING (restore on exit)│
       │  3. home_reclaim_check  (default-Alma XFS-full recovery —    │
       │       see below; no-op on a correctly-laid-out disk)         │
       │  4. assert repo reachable (curl -fsI /install.sh)            │
       │  5. dnf install python3 python3-pip python3-cryptography curl│
       │  6. pip install httpx from the offline wheel cache (fatal    │
       │       if it fails — rqlite_client's HTTP transport needs it) │
       │  7. stage DRBD+ELRepo RPMs → /var/lib/bedrock-install/rpms   │
       │  8. curl → /usr/local/bin/bedrock  (Python CLI)              │
       │  9. sudoers drop-in: add /usr/local/{s,}bin to secure_path   │
       │ 10. curl → /usr/local/bin/bedrock-d + unit (NOT enabled)     │
       │ 11. curl → /usr/local/bin/{rqlited,weed,kopia} + units       │
       │     write weed master/volume/filer/s3 + rqlited(+arbiter)    │
       │     units; mkdir seaweedfs/rqlite/cluster data dirs          │
       │ 12. disable drbd + libvirtd (quorum-aware boot owns them)    │
       │ 13. curl → bedrock-vg-loop / cert-refresh / mdns / redirect  │
       │     one daemon-reload; enable vg-loop, mdns, redirect now    │
       │ 14. sshd drop-in: PerSourcePenalties off for cluster traffic │
       │ 15. curl → /usr/local/lib/bedrock/lib/*.py  (LIB_FILES)      │
       │ 16. extract bedrock_d.tar.gz → /usr/local/lib/bedrock/       │
       │     extract mgmt.tar.gz       → /opt/bedrock/                │
       │ 17. write /etc/bedrock/installer.env  (BEDROCK_REPO=...)     │
       │ 18. set hostname bedrock-<mac> if still localhost.localdomain│
       │ 19. enable cert-refresh.timer (lib/ now on disk)             │
       │ 20. exec /usr/local/bin/bedrock bootstrap                    │
       └─────────────────────────────────────────────────────────────┘
  ~T+5s┌──── bedrock bootstrap (Python) ─────────────────────────────┐
       │  1. hardware.detect()  →  /proc/cpuinfo, /proc/meminfo,      │
       │     ip -o -br link, df /                                     │
       │  2. packages.install_base()                                  │
       │     a. ELRepo release  (bundled RPM, else elrepo.org)        │
       │     b. DRBD kmod+utils  (bundled RPMs, else ELRepo)          │
       │     c. base set: qemu-kvm libvirt libvirt-daemon-kvm         │
       │        virt-install virt-v2v libguestfs-tools                │
       │        libguestfs-winsupport qemu-guest-agent lvm2 xfsprogs  │
       │        tuned python3-pip iputils cockpit cockpit-machines    │
       │     d. modprobe drbd  (+ /etc/modules-load.d/drbd.conf)      │
       │     e. enable --now cockpit.socket; allow root in cockpit    │
       │     f. pip install mgmt deps on EVERY node (fastapi uvicorn  │
       │        paramiko websockets pydantic python-multipart msgpack)│
       │        — any node may take the mgmt role on failover         │
       │  3. os_setup.configure_base(hw)                              │
       │     a. setenforce 0; SELINUX=permissive in config            │
       │     b. disable --now firewalld                               │
       │     c. enable --now chronyd                                  │
       │     d. /root/.ssh/config accept-new for 192.168.*,10.*,      │
       │        bedrock-*  (qemu+ssh:// migrate trust)                │
       │     e. ssh-keygen id_ed25519 + self-trust authorized_keys    │
       │  4. os_setup.configure_bridge(hw)                            │
       │     - if br0 exists: skip                                    │
       │     - else: nmcli con add bridge br0 (ipv4/6 auto, stp off)  │
       │         + bridge-slave <primary>; con up br0; old con        │
       │         autoconnect no + down                                │
       │  5. write /etc/bedrock/state.json {hardware, bootstrap_done} │
       │     (atomic via lib.state.save)                              │
       └─────────────────────────────────────────────────────────────┘
  T+~2m  print:  "bedrock init   — start a new cluster"
               "bedrock join    — join an existing cluster"
```

Note: libvirtd and DRBD are installed but left disabled. The base-package step
loads the `drbd` module but does not enable libvirtd; `bedrock-d`'s boot
orchestrator starts them imperatively once a quorum role is known, so nothing
acts before quorum.

Dependency notes (why this order):

- **httpx before anything cluster-y**: `rqlite_client.py` is the HTTP transport
  to the cluster-state store; without it `bedrock init` produces an empty
  rqlite. Installed offline from the bundled wheel cache, so the failure can't
  be a slow mirror.
- **`bedrock-d` / `bedrock-rqlited` units written but not enabled**: their env
  files don't exist until `init`/`join` materialise them; auto-starting at
  `multi-user.target` would just crash-loop until the operator runs init.
- **ELRepo before DRBD**: `kmod-drbd9x` only lives in ELRepo. The bundled RPMs
  short-circuit ELRepo's slow mirrors when present.
- **SELinux permissive before firewall off**: a relabel after firewalld-off is
  harmless; the reverse can briefly deny the NM dbus transition during bridge
  creation.
- **Bridge last**: every prior step works over the current NIC; the bridge move
  can briefly drop SSH. If it does, reconnect via console and
  `systemctl restart NetworkManager`.

### /home reclaim (default-AlmaLinux disks only)

AlmaLinux's default partitioning fills the disk with XFS LVs and zero free
extents, and XFS can't shrink. When the script sees a known OS VG
(`almalinux` / `vg_almalinux`) with no free PE and a `/home` LV holding only the
default install footprint, it offers to `lvremove` `/home` to free VG space for
Bedrock. It refuses on any operator data and requires typing the LV name to
confirm. The Bedrock ISO partitions correctly from the start, so this path is
only for boxes already installed from stock Alma media.

## Log lines emitted

`bedrock bootstrap` prints to stdout (captured by install.sh and echoed to the
operator). It does not push to VictoriaLogs — the mgmt stack isn't up yet:

```
  === Bedrock Bootstrap ===
  Node: <hostname>  (<cpu_model>)
    vCPUs: <n>  RAM: <mb>MB
    NICs: <up-nic-list>
    Storage: <n>GB root disk
  Installing base packages (KVM, DRBD, libvirt, exporters)...
    Installing ELRepo from bundled payload...
    Installing DRBD from bundled payload (N RPMs, ~K KB)...
    Installing N packages from network repos...
    Installing mgmt-app Python deps (fastapi, uvicorn, ...)...
    Base packages installed.
  Configuring OS (SELinux, firewall, hostname)...
  Configuring networking (br0 bridge)...
    Creating br0 on <primary-nic>...
  Bootstrap complete. Next steps:
```

(The "exporters" word in the package-step header is cosmetic — exporters are
installed later, by `init`/`join`, not here.) dnf and nmcli detail goes to the
journal: `journalctl -u NetworkManager`.

## Failure modes

| Symptom | Likely cause | Recovery |
|---|---|---|
| `die "Cannot reach repo at …"` | Repo down / LAN routing | `curl -fsI ${BEDROCK_REPO}/install.sh`. |
| `httpx install failed` (fatal) | Wheel cache missing / unreachable | Check `${BEDROCK_REPO}/wheels/MANIFEST.txt`; republish wheels. |
| `dnf install` hangs | Mirror slow / MTU | `dnf clean all && dnf install` directly. |
| `bedrock: command not found` under `sudo` | secure_path | Re-run; the sudoers drop-in adds `/usr/local/bin`. Or `. /etc/profile`. |
| bridge created but node unreachable | NM moved IP to br0 slower than DHCP expected | Wait 30 s; `nmcli dev show br0`; reboot is safe. |
| `has_virt=false` in hardware dump | No VT-x/AMD-V (or nested VM without `kvm-*nested=1`) | Enable in BIOS / host `modprobe kvm-intel nested=1`. |
| reclaim refused: "/home contains user data" | Non-default `/home` | Back up + clear `/home`, or reinstall from the Bedrock ISO. |

When run from the Bedrock ISO firstboot, a bootstrap failure leaves the
"installation in progress" MOTD in place and logs to `journalctl -u
bedrock-firstboot`.

## State after bootstrap

The node has the full payload staged, but no cluster:

- `/etc/bedrock/state.json` holds `bootstrap_done: true` and `hardware: {...}`.
- `/etc/bedrock/installer.env` holds `BEDROCK_REPO=...`.
- No cluster identity (no `cluster_uuid`); rqlite not running; no DRBD
  resources or VMs; `bedrock-d` / `bedrock-rqlited` installed but not enabled.

Next: [`init-cluster.md`](init-cluster.md) (first node) or
[`join-cluster.md`](join-cluster.md) (every other node).
