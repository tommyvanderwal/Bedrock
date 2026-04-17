"""OS configuration — SELinux, firewall, bridge, hostname."""

import subprocess
import re
from pathlib import Path


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    return r.stdout.strip()


def configure_base(hw: dict):
    """SELinux permissive, firewall off, NTP active, cluster SSH auto-trust."""
    # SELinux permissive
    run("setenforce 0 2>/dev/null || true", check=False)
    try:
        content = Path("/etc/selinux/config").read_text()
        content = re.sub(r"^SELINUX=.*", "SELINUX=permissive", content, flags=re.MULTILINE)
        Path("/etc/selinux/config").write_text(content)
    except Exception:
        pass

    run("systemctl disable --now firewalld >/dev/null 2>&1 || true", check=False)
    run("systemctl enable --now chronyd >/dev/null 2>&1 || true", check=False)

    # Auto-accept unknown hosts on the cluster LAN + DRBD ring. virsh migrate
    # uses qemu+ssh:// to peers; without this, the first migration after a new
    # node joins fails with "Host key verification failed".
    ssh_dir = Path("/root/.ssh")
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    cfg = ssh_dir / "config"
    marker = "# bedrock-cluster-ssh"
    existing = cfg.read_text() if cfg.exists() else ""
    if marker not in existing:
        cfg.write_text(existing + f"\n{marker}\n"
            "Host 192.168.* 10.* bedrock-*\n"
            "    StrictHostKeyChecking accept-new\n"
            "    UserKnownHostsFile /root/.ssh/known_hosts\n")
        cfg.chmod(0o600)


def configure_bridge(hw: dict):
    """Create br0 bridge on the primary NIC if not already bridged.

    Skips if br0 already exists (e.g., from previous runs or cloud-init).
    """
    # Does br0 already exist?
    r = subprocess.run("ip link show br0", shell=True, capture_output=True)
    if r.returncode == 0:
        print("  br0 already exists, skipping bridge creation.")
        return

    # Find primary NIC
    from . import hardware
    primary = hardware.primary_nic(hw)
    if not primary:
        print("  No primary NIC found; skipping bridge setup.")
        return

    print(f"  Creating br0 on {primary}...")
    # Get current NM connection for the NIC
    current_con = run(f"nmcli -g NAME,DEVICE con show --active | grep ':{primary}$' | cut -d: -f1",
                      check=False) or "Wired connection 1"

    # Create bridge + bridge-slave, bring up, disconnect old
    run("nmcli con add type bridge ifname br0 con-name br0 "
        "ipv4.method auto ipv6.method auto connection.autoconnect yes stp off "
        ">/dev/null 2>&1")
    run(f"nmcli con add type bridge-slave ifname {primary} master br0 "
        f"con-name br0-{primary} >/dev/null 2>&1")
    run("nmcli con up br0 >/dev/null 2>&1 || true", check=False)
    run(f'nmcli con modify "{current_con}" connection.autoconnect no >/dev/null 2>&1 || true',
        check=False)
    run(f'nmcli con down "{current_con}" >/dev/null 2>&1 || true', check=False)
