# installer/lib/os_setup.py

OS-level host preparation run during install: it relaxes SELinux, turns the host
firewall off, ensures NTP is running, wires up root SSH (cluster auto-trust plus a
per-node Ed25519 key with self-trust), and converts the primary NIC into the `br0`
bridge that VMs attach to. It is one of the `LIB_FILES` staged by
`installer/install.sh`; the install path calls `configure_base(hw)` and
`configure_bridge(hw)` with the hardware-inventory dict produced by the `hardware`
module.

## Functions / Classes

### `run(cmd, check=True) -> str`
Run a shell command and return its trimmed stdout.
- **In:** `cmd` → shell string (run with `shell=True`); `check` → if true, raise
  `RuntimeError` on non-zero exit.
- **Out:** stripped stdout. Side effect: the subprocess itself. Raises
  `RuntimeError(f"{cmd} failed: {stderr}")` when `check` and the command fails.

### `configure_base(hw: dict) -> None`
Put the host into the baseline state Bedrock expects: SELinux permissive, firewall
disabled, NTP on, SSH self-trust.
- **In:** `hw` → hardware-inventory dict (accepted for signature symmetry; not read
  here).
- **Out:** `None`. Side effects: `setenforce 0`; rewrites `SELINUX=` to
  `permissive` in `/etc/selinux/config`; `systemctl disable --now firewalld`;
  `systemctl enable --now chronyd`; appends a `# bedrock-cluster-ssh` stanza to
  `/root/.ssh/config`; generates `/root/.ssh/id_ed25519` if absent; appends the
  public key to `/root/.ssh/authorized_keys`.

### `configure_bridge(hw: dict) -> None`
Create the `br0` bridge over the primary NIC unless it already exists.
- **In:** `hw` → hardware-inventory dict; the primary NIC is resolved via
  `hardware.primary_nic(hw)`.
- **Out:** `None`. Side effects (via `nmcli`): adds a `br0` bridge connection
  (DHCP v4/v6, autoconnect, STP off), adds a `br0-<nic>` bridge-slave for the
  primary NIC, brings `br0` up, and disables + brings down the NIC's prior
  NetworkManager connection. Prints and returns early if `br0` exists or no
  primary NIC is found.

## How it works

`configure_base` runs four mostly-independent setup blocks, each tolerant of
failure so a partially-configured host still completes:

- SELinux: `setenforce 0` (best-effort) for the running kernel, then a regex
  rewrite of `/etc/selinux/config` so the next boot stays permissive. The file
  edit is wrapped in a bare `except` — a missing/odd config file is ignored.
- Firewall and clock: `firewalld` disabled-and-stopped, `chronyd`
  enabled-and-started; both `|| true` so absence of the unit is non-fatal.
- SSH cluster trust: an idempotent stanza keyed off the `# bedrock-cluster-ssh`
  marker is appended to `/root/.ssh/config`. It sets `StrictHostKeyChecking
  accept-new` for `192.168.*`, `10.*`, and `bedrock-*` hosts so that
  `qemu+ssh://` live migration to a freshly joined peer does not stall on host-key
  verification.
- SSH identity: a per-node `id_ed25519` keypair is generated if missing, and its
  public key is appended to `authorized_keys` (guarded by a substring check) so
  the node can SSH to itself. The same key mesh is extended to peers at join time.

```
/root/.ssh/                     (dir mode 0700)
  config            # bedrock-cluster-ssh stanza: accept-new for cluster hosts
  id_ed25519        # per-node private key
  id_ed25519.pub
  authorized_keys   # contains own pubkey -> self-SSH works
```

`configure_bridge` is guard-first: if `ip link show br0` succeeds the bridge is
left untouched (handles re-runs and cloud-init pre-bridging). Otherwise it asks
`hardware.primary_nic(hw)` for the NIC — which prefers the first UP NIC with an
IP, else the first UP NIC, else `""` — and a missing NIC prints a skip and
returns. The NIC's active NetworkManager connection name is looked up (falling
back to `"Wired connection 1"`), then the bridge is built and the old connection
is deactivated:

```
before:   <nic> -- "Wired connection 1" (DHCP)

after:    <nic> -- bridge-slave "br0-<nic>" -- br0 (DHCP, autoconnect, STP off)
          "Wired connection 1": autoconnect no, down
```

Bringing `br0` up and tearing the old connection down are both `|| true`, since on
the live link the slave attach can momentarily drop connectivity. Disabling the
old profile's autoconnect stops it from reclaiming the device after reboot.

## Why

SELinux permissive and firewall-off match Bedrock's appliance model where the node
owns its network and the host firewall would only fight cluster traffic. The
per-node Ed25519 key with self-trust exists because the mgmt dashboard reaches
every node — including the local one — over paramiko SSH for `virsh`/`drbdadm`/`lvs`.
