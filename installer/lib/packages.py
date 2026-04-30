"""Package installation for Bedrock nodes.

Every Bedrock node — whether it's the initial mgmt master or a peer
that joined via `bedrock join` — gets the FULL package set installed
here. This includes the Python deps the mgmt FastAPI app needs
(paramiko, fastapi, uvicorn, websockets, pydantic, python-multipart),
because any node may take over the mgmt role via
`tier_storage.transfer_mgmt_role()` and must be ready to start
`bedrock-mgmt.service` immediately. (See lessons-log L17.)
"""

import subprocess

ELREPO_URL = "https://www.elrepo.org/elrepo-release-9.el9.elrepo.noarch.rpm"

BASE_PACKAGES = [
    "qemu-kvm",
    "libvirt",
    "libvirt-daemon-kvm",
    "virt-install",
    "virt-v2v",
    "libguestfs-tools",
    "libguestfs-winsupport",
    "qemu-guest-agent",
    "lvm2",
    "xfsprogs",
    "tuned",
    "python3-pip",
    "iputils",
    "cockpit",
    "cockpit-machines",
    "nfs-utils",          # NFS server + client; any node may export tier-bulk/critical
]

DRBD_PACKAGES = [
    "kmod-drbd9x",
    "drbd9x-utils",
]

# Python packages required by the mgmt FastAPI app (mgmt/app.py).
# Installed on EVERY node so any node can take over the mgmt role
# without runtime pip install. Pinning is intentionally loose; bedrock
# CLI evolves with whatever fastapi/pydantic versions are current.
MGMT_PYTHON_PACKAGES = [
    "fastapi",
    "uvicorn",
    "paramiko",
    "websockets",
    "pydantic",
    "python-multipart",
]


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    return r.stdout.strip()


def _rpm_installed(pkg: str) -> bool:
    r = subprocess.run(["rpm", "-q", pkg], capture_output=True)
    return r.returncode == 0


def install_base():
    """Install base packages required on every Bedrock node."""
    # ELRepo (needed for DRBD)
    if not _rpm_installed("elrepo-release"):
        print("  Installing ELRepo...")
        run(f"dnf install -y -q {ELREPO_URL} >/dev/null 2>&1")

    to_install = [p for p in BASE_PACKAGES + DRBD_PACKAGES if not _rpm_installed(p)]
    if to_install:
        print(f"  Installing {len(to_install)} packages...")
        run(f"dnf install -y -q {' '.join(to_install)} >/dev/null 2>&1")

    # Load DRBD module
    run("modprobe drbd 2>/dev/null || true", check=False)
    run("echo drbd > /etc/modules-load.d/drbd.conf", check=False)

    # Enable libvirtd
    run("systemctl enable --now libvirtd >/dev/null 2>&1")

    # Enable cockpit for web console access on port 9090
    run("systemctl enable --now cockpit.socket >/dev/null 2>&1", check=False)
    # Allow root login to cockpit (default: blocked)
    run("sed -i '/^root$/d' /etc/cockpit/disallowed-users 2>/dev/null", check=False)

    # Install mgmt-app Python deps on EVERY node so any node can take
    # over the mgmt role via transfer_mgmt_role() without a runtime
    # pip install. (Lessons-log L17.)
    print(f"  Installing mgmt-app Python deps "
          f"({', '.join(MGMT_PYTHON_PACKAGES)})...")
    run(f"pip3 install -q {' '.join(MGMT_PYTHON_PACKAGES)} "
        f"2>&1 | tail -2", check=False)

    print("  Base packages installed.")
