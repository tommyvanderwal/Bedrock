"""Package installation for Bedrock nodes."""

import subprocess

ELREPO_URL = "https://www.elrepo.org/elrepo-release-9.el9.elrepo.noarch.rpm"

BASE_PACKAGES = [
    "qemu-kvm",
    "libvirt",
    "libvirt-daemon-kvm",
    "virt-install",
    "libguestfs-tools",
    "qemu-guest-agent",
    "lvm2",
    "xfsprogs",
    "tuned",
    "python3-pip",
    "iputils",
    "cockpit",
    "cockpit-machines",
]

DRBD_PACKAGES = [
    "kmod-drbd9x",
    "drbd9x-utils",
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

    print("  Base packages installed.")
