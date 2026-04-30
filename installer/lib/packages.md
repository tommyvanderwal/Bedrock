# `packages.py` — package installation, every-node baseline

Companion document for [`packages.py`](packages.py). This module is
called from `bedrock bootstrap` (the first command run on every node,
mgmt master and peer alike) and is responsible for *all* the OS-level
package installation Bedrock needs.

The deliberate design is **no per-role package sets**. Every node
gets the same packages. The mgmt FastAPI app's Python deps are
installed on every node, not just the initial master, because any
node may take over the mgmt role via
[`tier_storage.transfer_mgmt_role()`](tier_storage.md#transfer_mgmt_role)
and must be ready to start `bedrock-mgmt.service` without a runtime
`pip install`. (See lessons-log
[L17](../../docs/lessons-log.md#l17).)

---

## Top-of-file summary

`install_base()` is the only entry point. It is **idempotent** — safe
to re-run any time (`rpm -q` skip-list and `pip install` are both
idempotent). It does five things, in order:

1. Install ELRepo (only if missing) — needed for the DRBD kernel
   module (`kmod-drbd9x` is published there, not in stock AlmaLinux).
2. `dnf install` everything in `BASE_PACKAGES + DRBD_PACKAGES` that
   isn't already installed.
3. Load the `drbd` kernel module + persist via
   `/etc/modules-load.d/drbd.conf`.
4. Enable + start `libvirtd` and `cockpit.socket`. Strip `root` from
   `/etc/cockpit/disallowed-users` so the operator can log in to the
   web console.
5. `pip3 install` the mgmt-app Python deps (`MGMT_PYTHON_PACKAGES`).

Pre-conditions:
- AlmaLinux 9 (or RHEL 9 family).
- Network access to the dnf repos and pypi.org.
- `dnf` and `pip3` are present (pip3 is in `BASE_PACKAGES` so the
  first run uses the system pip; later runs use the dnf-installed
  one — same binary either way).
- Caller has root.

Post-conditions:
- Every package in `BASE_PACKAGES + DRBD_PACKAGES` is `rpm -q`
  satisfiable.
- `drbd` module is loaded and persisted at boot.
- `libvirtd` and `cockpit.socket` are enabled + active.
- Every package in `MGMT_PYTHON_PACKAGES` resolves via
  `python3 -c 'import <name>'`.

---

## ASCII diagram — install flow

```
                       bedrock bootstrap (root)
                                  │
                                  ▼
                       packages.install_base()
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                   │
              ▼                   ▼                   ▼
    ┌───────────────────┐ ┌────────────────┐ ┌────────────────┐
    │ ELRepo            │ │ BASE_PACKAGES  │ │ DRBD_PACKAGES  │
    │ (rpm install)     │ │ qemu-kvm       │ │ kmod-drbd9x    │
    │ — needed for DRBD │ │ libvirt        │ │ drbd9x-utils   │
    │   kernel module   │ │ lvm2 xfsprogs  │ │ (from ELRepo)  │
    │                   │ │ nfs-utils      │ │                │
    │                   │ │ python3-pip    │ │                │
    │                   │ │ cockpit        │ │                │
    │                   │ │ ...            │ │                │
    └───────────────────┘ └────────────────┘ └────────────────┘
                                  │
                                  ▼
                  ┌────────────────────────────────┐
                  │  modprobe drbd                 │
                  │  /etc/modules-load.d/drbd.conf │
                  └────────────────────────────────┘
                                  │
                                  ▼
                  ┌────────────────────────────────┐
                  │  systemctl enable --now        │
                  │   libvirtd cockpit.socket      │
                  │  rm root from disallowed-users │
                  └────────────────────────────────┘
                                  │
                                  ▼
                  ┌────────────────────────────────┐
                  │  pip3 install                  │
                  │   MGMT_PYTHON_PACKAGES         │
                  │   (every node, per L17)        │
                  └────────────────────────────────┘
                                  │
                                  ▼
                              done
```

The same flow runs on:
- The first node before `bedrock init`.
- Every subsequent node before `bedrock join`.

There is no "compute-only" subset. A node that's currently a peer
might be the master tomorrow.

---

## Design invariants

1. **Every node has the full mgmt-app dep set installed at bootstrap
   time.** A `bedrock-mgmt.service` startup must never trigger a
   network round-trip to pypi. This is what makes `transfer_mgmt_role`
   safe.

2. **`install_base()` is idempotent.** `_rpm_installed()` skip-list
   keeps `dnf install` to-do list minimal on re-run; `pip install`
   no-ops on already-satisfied versions. The function can be re-run
   after a partial bootstrap failure without corrupting state.

3. **No version pinning by default.** Pinning would force us to chase
   ELRepo / pypi version drift. The mgmt FastAPI app is version-loose
   on purpose so the bedrock CLI evolves with whatever versions
   AlmaLinux 9 ships at the time of install. If that bites us we'll
   pin specific packages with a documented reason.

4. **Failures on optional steps don't abort.** Cockpit enablement and
   the disallowed-users edit use `check=False` — the bootstrap should
   still complete on systems where cockpit isn't packaged or the
   disallowed-users file isn't present.

5. **DRBD module loads at install time, not at first DRBD operation.**
   `modprobe drbd` here surfaces missing-module errors during
   `bedrock bootstrap` (where the operator is watching) rather than
   during the first cluster transition (where they aren't).

---

## Where state lives

| What | Where | Owner | Changes when |
|---|---|---|---|
| Installed packages | rpm DB (`/var/lib/rpm`) | dnf | `install_base()` runs |
| ELRepo repo file | `/etc/yum.repos.d/elrepo.repo` | dnf | First `install_base()` |
| DRBD module persist | `/etc/modules-load.d/drbd.conf` | this module | First `install_base()` |
| libvirtd state | systemd | systemd | Enabled by `install_base()` |
| Cockpit state | systemd | systemd | Enabled by `install_base()` |
| Cockpit allow-root | `/etc/cockpit/disallowed-users` | this module | First `install_base()` |
| Python deps | site-packages (system or `~/.local`) | pip | Every `install_base()` |

`install_base()` does not write to `state.json` or `cluster.json`;
its effect is observable via `rpm -q` and `python3 -c 'import …'`.

---

## Operations explained

### `install_base()`

The only entry point. Pre/post-conditions and idempotency are
covered above; here are the per-step exact commands a reviewer can
verify against:

```bash
# Step 1 — ELRepo
dnf install -y -q https://www.elrepo.org/elrepo-release-9.el9.elrepo.noarch.rpm

# Step 2 — base + DRBD packages (only the missing ones)
dnf install -y -q <space-separated list filtered by `rpm -q`>

# Step 3 — DRBD module
modprobe drbd 2>/dev/null || true
echo drbd > /etc/modules-load.d/drbd.conf

# Step 4 — services
systemctl enable --now libvirtd
systemctl enable --now cockpit.socket
sed -i '/^root$/d' /etc/cockpit/disallowed-users 2>/dev/null

# Step 5 — Python deps for the mgmt app
pip3 install -q fastapi uvicorn paramiko websockets pydantic python-multipart
```

Crash safety: every step is independently idempotent. A power loss
during step 2 leaves a partially-populated rpm DB; the next
`install_base()` skips already-installed entries and finishes the
list. Same for step 5 with pip.

---

## Known issues / current limitations

### 1. `pip3 install` runs as root and writes to system site-packages

We don't use a venv. The mgmt app runs as root (it manages systemd,
DRBD, libvirt, exports NFS, …) so a venv would just add overhead
without isolation benefit. Acceptable trade-off; revisit if we ever
unprivilege the mgmt app.

### 2. No package version pinning

Per invariant 3, this is deliberate. The risk is a future Fedora /
ELRepo / pypi version-drift breaking compatibility. Mitigation: clean
runs in the testbed catch this immediately — see
[`docs/scenarios/`](../../docs/scenarios/).

### 3. ELRepo URL is hard-coded for el9

`ELREPO_URL` points at the el9 release rpm. AlmaLinux 10 / RHEL 10
will need a different URL. Not addressed yet because we target el9
explicitly for v1.0.

### 4. No mirror-fallback

If `dnf install` can't reach the upstream repos, install_base() fails.
Air-gapped install is not currently supported. Tracked as future
work; would require a local mirror config option.

---

## Why each design choice

### Why install mgmt deps on every node (not just the master)?

Lessons-log [L17](../../docs/lessons-log.md#l17) is the long answer.
Short version: `transfer_mgmt_role` must work without a runtime
network dependency. Pre-installing the deps everywhere is cheap
(~30 MB total) and removes a class of failure.

### Why not split into `install_compute` and `install_mgmt`?

Per Tommy: "any one [node] could in principle become the master."
The role split would force us to maintain the symmetry manually
(`if node becomes master: install mgmt deps`). One package set
removes the bug surface.

### Why include `nfs-utils` in BASE_PACKAGES?

The bulk and critical tiers are NFS-exported by whichever node holds
the master role. Any node can become master, so any node may need
to *export* NFS, not just *mount* it. `nfs-utils` provides both the
client (`mount.nfs`) and server (`nfsd`, `exportfs`).

### Why install `libguestfs-winsupport` even on Linux-only nodes?

Bedrock supports both Linux and Windows VMs. virt-v2v + virt-customize
need winsupport to handle Windows guests during conversion / first-
boot customization. We can't predict at bootstrap time whether the
operator will create Windows VMs, so we install it on every node.

### Why enable Cockpit?

Cockpit on `:9090` is the "second pane of glass" we ship for free —
operators get a system-level web UI without any extra setup. It's
also a useful escape hatch if the bedrock-mgmt dashboard is broken:
oncall can still SSH/Cockpit into the box and see system state.

### Why `pip3 install` (no `--user`, no venv)?

System-wide install puts modules where systemd-run services can
import them without per-user PYTHONPATH gymnastics. Root-only,
single-purpose box — venv adds friction without benefit.

---

## Sources

### AlmaLinux / RHEL packaging
- [AlmaLinux 9 package set](https://repo.almalinux.org/almalinux/9/) —
  baseline for `BASE_PACKAGES`.
- [ELRepo — kmod-drbd9x package](https://elrepo.org/tiki/HomePage) —
  source for DRBD 9 kernel module on el9.

### DRBD module loading
- [`modules-load.d(5)`](https://man7.org/linux/man-pages/man5/modules-load.d.5.html)
  — persists the `drbd` module across reboots.
- [`modprobe(8)`](https://man7.org/linux/man-pages/man8/modprobe.8.html)
  — load the module immediately so DRBD operations work without a
  reboot.

### libvirt / Cockpit
- [libvirtd service](https://libvirt.org/manpages/libvirtd.html) —
  enabled at bootstrap so virsh works system-wide.
- [Cockpit installation](https://cockpit-project.org/running) —
  `cockpit.socket` activates the daemon on first connection.

### Python packages
- [pip — `pip install`](https://pip.pypa.io/en/stable/cli/pip_install/)
  — `-q` quiet mode, idempotent against already-satisfied requirements.
- [FastAPI](https://fastapi.tiangolo.com/) — mgmt dashboard backend.
- [Uvicorn](https://www.uvicorn.org/) — ASGI server for FastAPI.
- [Paramiko](https://www.paramiko.org/) — SSH client used by the
  mgmt app to fan out commands to peers.

### Bedrock project
- [`docs/lessons-log.md` — L17](../../docs/lessons-log.md#l17) — why
  mgmt deps are now installed on every node.
- [`tier_storage.md` — `transfer_mgmt_role`](tier_storage.md#transfer_mgmt_role)
  — the operation that requires every node to be mgmt-ready.
- [`installer/bedrock` — `cmd_bootstrap`](../bedrock) — the entry
  point that calls `install_base()`.
