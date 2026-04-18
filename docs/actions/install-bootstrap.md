# Install a node (`curl | bash` → `bedrock bootstrap`)

One-shot script that turns a fresh **AlmaLinux 9 minimal** box into a Bedrock
node. It downloads the CLI + libs, installs packages, configures OS, and
prints the next step (`init` or `join`). It does **not** yet touch the
cluster — that's a later subcommand.

**Triggered by:** an operator running, as root:

```bash
BEDROCK_REPO=http://<repo-host>:8000 \
  curl -sSL http://<repo-host>:8000/install.sh | bash
```

**Source files:** `installer/install.sh`, `installer/bedrock` (subcommand
`bootstrap`), `installer/lib/{hardware,os_setup,packages}.py`.

## Preconditions

- Fresh AlmaLinux 9 (the script warns but continues on other RHEL-likes).
- Root shell.
- Reachability to `BEDROCK_REPO` (the install repo, typically a dev box
  serving `installer/` over HTTP port 8000).
- `python3` available (will be dnf-installed if missing).

## Sequence

```
  T=0  ┌──── install.sh (bash, ~100 lines) ─────────────────────────┐
       │ 1. assert root, assert repo reachable (curl -fsSL /)        │
       │ 2. dnf install -y python3 python3-pip curl                  │
       │ 3. mkdir /usr/local/lib/bedrock/lib                         │
       │ 4. curl → /usr/local/bin/bedrock  (Python CLI)              │
       │ 5. curl → /usr/local/lib/bedrock/lib/{hardware,os_setup,    │
       │       packages,exporters,mgmt_install,agent_install,vm,     │
       │       workload,discovery,state,__init__}.py                 │
       │ 6. write /etc/bedrock/installer.env  (BEDROCK_REPO=...)     │
       │ 7. exec /usr/local/bin/bedrock bootstrap                    │
       └─────────────────────────────────────────────────────────────┘
  ~T+5s┌──── bedrock bootstrap (Python) ─────────────────────────────┐
       │ 1. hardware.detect()  →  /proc/cpuinfo, /proc/meminfo,      │
       │    ip -br link, df /                                        │
       │ 2. packages.install_base()                                  │
       │    a. dnf install elrepo-release                            │
       │    b. dnf install qemu-kvm libvirt libvirt-daemon-kvm       │
       │       virt-install libguestfs-tools qemu-guest-agent        │
       │       lvm2 xfsprogs tuned python3-pip iputils               │
       │       cockpit cockpit-machines  kmod-drbd9x drbd9x-utils    │
       │    c. modprobe drbd  (+ /etc/modules-load.d/drbd.conf)      │
       │    d. systemctl enable --now libvirtd cockpit.socket        │
       │    e. remove root from /etc/cockpit/disallowed-users        │
       │ 3. os_setup.configure_base(hw)                              │
       │    a. setenforce 0, sed /etc/selinux/config → permissive    │
       │    b. systemctl disable --now firewalld                     │
       │    c. systemctl enable --now chronyd                        │
       │    d. write /root/.ssh/config  (accept-new for 192.168.*,   │
       │       10.*, bedrock-*)                                      │
       │ 4. os_setup.configure_bridge(hw)                            │
       │    - if br0 exists: skip                                    │
       │    - else: nmcli con add type bridge br0                    │
       │         nmcli con add type bridge-slave <primary> br0       │
       │         nmcli con down "<old nic con>"  +  autoconnect no   │
       │ 5. save /etc/bedrock/state.json  { hardware, bootstrap_done }│
       └─────────────────────────────────────────────────────────────┘
  T+~2m  print:  "bedrock init   — start a new cluster"
               "bedrock join    — join an existing cluster"
```

Dependency notes (why this order):

- **ELRepo before DRBD**: `kmod-drbd9x` only lives in ELRepo.
- **libvirtd before first use**: `qemu-guest-agent` and `virt-install` have
  daemon-start side effects; enabling libvirtd explicitly makes later
  `virsh` calls deterministic.
- **SELinux permissive before firewall off**: a kernel relabel after
  firewalld-off is harmless; the reverse can briefly leave AVCs denying
  the NM dbus transition during bridge creation.
- **Bridge last**: all previous steps work over the current NIC; the
  bridge move can briefly drop the SSH session. Script is tolerant —
  if bridge creation breaks connectivity, the operator reconnects via
  console and `systemctl restart NetworkManager`.

## Log lines emitted

`bedrock bootstrap` prints to stdout (captured by install.sh's subshell and
echoed to the operator). It does **not** push to VictoriaLogs — the mgmt
stack isn't up yet. Lines include:

```
  Node: <hostname>  (<cpu_model>)
    vCPUs: <n>  RAM: <mb>MB
    NICs: <list>
    Storage: <n>GB root disk
  Installing base packages (KVM, DRBD, libvirt, exporters)...
    Installing ELRepo...
    Installing 10 packages...
    Base packages installed.
  Configuring OS (SELinux, firewall, hostname)...
  Configuring networking (br0 bridge)...
    Creating br0 on <primary-nic>...
  Bootstrap complete.
```

dnf and nmcli logs go to the systemd journal: `journalctl -u dnf-automatic`,
`journalctl -u NetworkManager`.

## Failure modes

| Symptom | Likely cause | Recovery |
|---|---|---|
| `die "Cannot reach repo at …"` | Repo server down / LAN routing | Check repo server (`curl http://<repo>:8000/install.sh`). |
| `dnf install` hangs | Mirror slow / network MTU issue | `dnf clean all && dnf install` directly. |
| `bedrock: command not found` after install.sh exits 0 | `/usr/local/bin` not in root's PATH | Re-source profile: `. /etc/profile`. |
| bridge created but node unreachable | NM moved IP to br0 slower than DHCP lease expected | Wait 30 s; `nmcli dev show br0`; reboot is safe. |
| `has_virt=false` in hardware dump | No VT-x/AMD-V (or nested VM without `kvm-*nested=1`) | Enable in BIOS / host `modprobe kvm-intel nested=1`. |

## What's left to do after bootstrap

The node has everything installed, but no cluster yet:

- `/etc/bedrock/state.json` contains `bootstrap_done: true`, `hardware: {...}`.
- `/etc/bedrock/cluster.json` does **not** exist yet.
- No DRBD resources, no VMs, no running mgmt services.

Next: [`init-cluster.md`](init-cluster.md) (first node) or
[`join-cluster.md`](join-cluster.md) (every other node).
