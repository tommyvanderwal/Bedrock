#!/usr/bin/env python3
"""Bedrock testbed — spawn/manage nested sim nodes on the dev box.

Usage:
  spawn.py prereqs          # install libvirt, create networks, download image
  spawn.py up N             # scale to N running sim nodes (0 = destroy all)
  spawn.py down             # destroy all sim nodes
  spawn.py list             # list sim nodes + state
  spawn.py ssh NODE         # ssh into sim-NODE (1-based)
  spawn.py exec NODE CMD..  # run command on sim-NODE
  spawn.py reset            # destroy + wipe all sim node state
"""

import argparse
import os
import subprocess
import sys
import hashlib
import json
import shutil
import time
from pathlib import Path

TESTBED = Path(__file__).parent.resolve()
IMAGES_DIR = TESTBED / "images"
NETWORKS_DIR = TESTBED / "networks"
CLOUD_INIT_DIR = TESTBED / "cloud-init"
STATE_DIR = TESTBED / "state"

GOLDEN_IMG = IMAGES_DIR / "almalinux-9.qcow2"
ALMA_URL = "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2"

MAX_NODES = 4
NODE_RAM_MB = 12288
NODE_VCPUS = 4
NODE_DISK_GB = 30   # OS root disk (cloud image is XFS, no LVM)
NODE_DATA_DISK_GB = 100   # second disk: bedrock VG (thin pool for tiers + DRBD + Garage)

MGMT_NET = "bedrock-mgmt"
DRBD_NET = "bedrock-drbd"
DRBD_PREFIX = "10.99.0"  # node i gets DRBD_PREFIX + .{10+i-1}

# Static LAN IPs for the sims (br0). Reserve these on the home router
# to avoid DHCP collisions; the sims do NOT request leases. Only the
# home router is a DHCP server on the LAN.
MGMT_PREFIX = "192.168.2"      # node i gets MGMT_PREFIX + .{50+i-1}
MGMT_GATEWAY = "192.168.2.254"
MGMT_DNS = "192.168.2.254"


def mgmt_ip(i: int) -> str:
    return f"{MGMT_PREFIX}.{50 + i - 1}"

SSH_KEY = Path.home() / ".ssh" / "id_ed25519"
SSH_PUBKEY = Path.home() / ".ssh" / "id_ed25519.pub"


def run(cmd, check=True, capture=False):
    """Run a shell command. Returns (stdout, returncode)."""
    if isinstance(cmd, str):
        cmd = ["bash", "-c", cmd]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.stderr.write(f"Command failed: {cmd}\n{r.stderr}\n")
        sys.exit(r.returncode)
    return (r.stdout.strip() if capture else None, r.returncode)


def virsh(*args, capture=True):
    return run(["sudo", "virsh"] + list(args), check=False, capture=capture)


def node_name(i: int) -> str:
    return f"bedrock-sim-{i}"


def drbd_ip(i: int) -> str:
    return f"{DRBD_PREFIX}.{10 + i - 1}"


def ssh_key_exists() -> bool:
    return SSH_KEY.exists() and SSH_PUBKEY.exists()


def ensure_ssh_key():
    if not ssh_key_exists():
        print("Generating SSH key for testbed access...")
        run(f"ssh-keygen -t ed25519 -N '' -f {SSH_KEY}")


# ── Prereqs ────────────────────────────────────────────────────────────────

def cmd_prereqs(args):
    """Install libvirt, create networks, download image."""
    # Verify tools exist
    for tool in ("virsh", "virt-install", "cloud-localds", "qemu-img"):
        if not shutil.which(tool):
            print(f"FAIL: {tool} not found. Install libvirt + qemu + cloud-image-utils.")
            sys.exit(1)

    # Start libvirtd
    out, _ = run("systemctl is-active libvirtd", check=False, capture=True)
    if out != "active":
        print("Starting libvirtd...")
        run("sudo systemctl enable --now libvirtd")

    ensure_ssh_key()

    # Create networks
    existing, _ = virsh("net-list", "--all", "--name")
    for net_file in NETWORKS_DIR.glob("*.xml"):
        net_name = net_file.stem
        if net_name in existing.split():
            print(f"Network '{net_name}' exists")
        else:
            print(f"Creating network '{net_name}'...")
            virsh("net-define", str(net_file))
        # Make sure autostart + active
        virsh("net-autostart", net_name, capture=False)
        state_out, _ = virsh("net-info", net_name)
        if "Active:" in state_out and "yes" in state_out:
            pass
        else:
            virsh("net-start", net_name, capture=False)

    # Download golden image
    IMAGES_DIR.mkdir(exist_ok=True)
    if not GOLDEN_IMG.exists():
        print(f"Downloading AlmaLinux 9 cloud image to {GOLDEN_IMG}...")
        run(f"curl -L -o {GOLDEN_IMG} '{ALMA_URL}'")
    print(f"Golden image: {GOLDEN_IMG}")

    STATE_DIR.mkdir(exist_ok=True)
    print("Prereqs OK.")


# ── Cloud-init ISO generation ──────────────────────────────────────────────

def make_cloud_init(node_idx: int, all_indices: list[int]) -> Path:
    """Generate cloud-init ISO for a node. Returns path to the ISO."""
    hostname = node_name(node_idx)
    pubkey = SSH_PUBKEY.read_text().strip()

    hosts_entries = "\n".join(
        f"      {drbd_ip(j)} {node_name(j)}-drbd" for j in all_indices
    )

    # Password-hash for sim-node root. Set BEDROCK_SIM_PASSWD_HASH to override;
    # by default leave empty so only SSH key auth works (the key is injected
    # from ~/.ssh/id_*.pub via {SSH_PUBKEY} below).
    passwd_hash = os.environ.get("BEDROCK_SIM_PASSWD_HASH", "*")

    user_data_tmpl = (CLOUD_INIT_DIR / "user-data.tmpl").read_text()
    user_data = (user_data_tmpl
                 .replace("{HOSTNAME}", hostname)
                 .replace("{ROOT_PASSWD_HASH}", passwd_hash)
                 .replace("{SSH_PUBKEY}", pubkey)
                 .replace("{DRBD_IP}", drbd_ip(node_idx))
                 .replace("{MGMT_IP}", mgmt_ip(node_idx))
                 .replace("{MGMT_GATEWAY}", MGMT_GATEWAY)
                 .replace("{MGMT_DNS}", MGMT_DNS)
                 .replace("{HOSTS_ENTRIES}", hosts_entries))

    meta_data_tmpl = (CLOUD_INIT_DIR / "meta-data.tmpl").read_text()
    meta_data = meta_data_tmpl.replace("{HOSTNAME}", hostname)

    node_state = STATE_DIR / hostname
    node_state.mkdir(exist_ok=True)
    (node_state / "user-data").write_text(user_data)
    (node_state / "meta-data").write_text(meta_data)

    iso_path = node_state / "seed.iso"
    run(f"cloud-localds {iso_path} {node_state}/user-data {node_state}/meta-data")
    return iso_path


# ── Node lifecycle ─────────────────────────────────────────────────────────

def node_exists(i: int) -> bool:
    out, _ = virsh("list", "--all", "--name")
    return node_name(i) in out.split()


def create_node(i: int, all_indices: list[int]):
    hostname = node_name(i)
    node_state = STATE_DIR / hostname
    node_state.mkdir(exist_ok=True)

    # Create thin qcow2 overlay on golden image (root disk)
    disk_path = node_state / "root.qcow2"
    if not disk_path.exists():
        print(f"  Creating {NODE_DISK_GB}GB root qcow2 for {hostname}...")
        run(f"qemu-img create -f qcow2 -F qcow2 -b {GOLDEN_IMG} "
            f"{disk_path} {NODE_DISK_GB}G", capture=False)

    # Second qcow2: data disk for bedrock VG (tiers + DRBD + Garage)
    data_path = node_state / "data.qcow2"
    if not data_path.exists():
        print(f"  Creating {NODE_DATA_DISK_GB}GB data qcow2 for {hostname}...")
        run(f"qemu-img create -f qcow2 {data_path} {NODE_DATA_DISK_GB}G",
            capture=False)

    # Generate cloud-init ISO
    iso_path = make_cloud_init(i, all_indices)

    # virt-install the VM
    print(f"  Defining {hostname}...")
    run(["sudo", "virt-install",
         "--name", hostname,
         "--memory", str(NODE_RAM_MB),
         "--vcpus", str(NODE_VCPUS),
         "--cpu", "host-passthrough",
         "--disk", f"path={disk_path},format=qcow2,bus=virtio",
         "--disk", f"path={data_path},format=qcow2,bus=virtio",
         "--disk", f"path={iso_path},device=cdrom",
         "--network", f"network={MGMT_NET},model=virtio",
         "--network", f"network={DRBD_NET},model=virtio",
         "--os-variant", "almalinux9",
         "--graphics", "none",
         "--console", "pty,target_type=serial",
         "--import",
         "--noautoconsole",
         "--noreboot",
        ])
    # virt-install starts the domain but we want to start manually
    virsh("start", hostname, capture=False)


def destroy_node(i: int, wipe: bool = False):
    hostname = node_name(i)
    if not node_exists(i):
        return
    print(f"  Destroying {hostname}...")
    virsh("destroy", hostname, capture=False)
    virsh("undefine", hostname, "--remove-all-storage", "--nvram", capture=False)
    if wipe:
        node_state = STATE_DIR / hostname
        if node_state.exists():
            shutil.rmtree(node_state)


def list_nodes():
    for i in range(1, MAX_NODES + 1):
        if node_exists(i):
            state_out, _ = virsh("domstate", node_name(i))
            print(f"  {node_name(i)}: {state_out}")


# ── CLI commands ───────────────────────────────────────────────────────────

def cmd_up(args):
    target = int(args.count)
    if target < 0 or target > MAX_NODES:
        print(f"N must be 0..{MAX_NODES}")
        sys.exit(1)

    # Destroy nodes above target
    for i in range(target + 1, MAX_NODES + 1):
        if node_exists(i):
            destroy_node(i)

    # Create nodes up to target
    all_indices = list(range(1, target + 1))
    for i in range(1, target + 1):
        if not node_exists(i):
            print(f"Spawning {node_name(i)}...")
            create_node(i, all_indices)
        else:
            print(f"{node_name(i)}: already exists")

    print(f"\nTarget: {target} node(s). Current state:")
    list_nodes()


def cmd_down(args):
    for i in range(MAX_NODES, 0, -1):
        if node_exists(i):
            destroy_node(i)
    print("All sim nodes destroyed.")


def cmd_list(args):
    list_nodes()


def cmd_ssh(args):
    i = int(args.node)
    ip = get_mgmt_ip(i)
    if not ip:
        print(f"No IP found for {node_name(i)}. Is it up?")
        sys.exit(1)
    os.execvp("ssh", ["ssh", "-o", "StrictHostKeyChecking=no",
                      "-o", "UserKnownHostsFile=/dev/null",
                      f"root@{ip}"] + list(args.cmd or []))


def cmd_exec(args):
    i = int(args.node)
    ip = get_mgmt_ip(i)
    if not ip:
        print(f"No IP for {node_name(i)}")
        sys.exit(1)
    cmd_str = " ".join(args.cmd)
    os.execvp("ssh", ["ssh", "-o", "StrictHostKeyChecking=no",
                      "-o", "UserKnownHostsFile=/dev/null",
                      f"root@{ip}", cmd_str])


def cmd_reset(args):
    for i in range(MAX_NODES, 0, -1):
        if node_exists(i):
            destroy_node(i, wipe=True)
    # Clean state dir
    if STATE_DIR.exists():
        for child in STATE_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
    print("All sim nodes destroyed and state wiped.")


def get_mgmt_ip(i: int) -> str | None:
    """Get the bedrock-mgmt IP of a sim node.

    With cloud-init pinning a static IP per index, we can return it
    directly once the VM exists. The agent-based fallback below is kept
    for the no-static-IP edge case (image without our cloud-init).
    """
    hostname = node_name(i)
    if not node_exists(i):
        return None
    # Static IP per node index — set by cloud-init from MGMT_PREFIX.
    return mgmt_ip(i)

    # Try virsh domifaddr (works for NAT)
    out, _ = virsh("domifaddr", hostname)
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "ipv4":
            ip = parts[3].split("/")[0]
            # Skip DRBD net IPs (we want mgmt)
            if not ip.startswith(DRBD_PREFIX):
                return ip

    # For bridged networks: get MAC from XML, then look up in host ARP
    out, _ = virsh("domiflist", hostname)
    mgmt_mac = None
    for line in out.split("\n"):
        parts = line.split()
        if len(parts) >= 5 and parts[2] == MGMT_NET:
            mgmt_mac = parts[4].lower()
            break
    if not mgmt_mac:
        return None

    # Check existing ARP table first
    arp_out, _ = run("ip neigh", capture=True)
    for line in arp_out.split("\n"):
        if mgmt_mac in line.lower():
            return line.split()[0]

    # Trigger ARP by pinging the subnet (quick scan)
    run(f"ping -c 1 -W 1 -b 192.168.2.255 2>/dev/null || true", check=False)
    run(f"arp-scan -l -I br0 2>/dev/null || nmap -sn 192.168.2.0/24 -oG - 2>/dev/null > /tmp/nmap-out || true",
        check=False)
    # Retry ARP
    arp_out, _ = run("ip neigh", capture=True)
    for line in arp_out.split("\n"):
        if mgmt_mac in line.lower():
            return line.split()[0]

    # Fallback: parse nmap output
    if Path("/tmp/nmap-out").exists():
        nmap_content = Path("/tmp/nmap-out").read_text()
        # Pair IPs with MACs from nmap greppable output
        import re as _re
        m = _re.search(rf"Host:\s*(\d+\.\d+\.\d+\.\d+).*{_re.escape(mgmt_mac)}",
                       nmap_content, _re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Bedrock testbed node manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("prereqs").set_defaults(func=cmd_prereqs)

    up = sub.add_parser("up")
    up.add_argument("count", help="Number of nodes to run (0..4)")
    up.set_defaults(func=cmd_up)

    sub.add_parser("down").set_defaults(func=cmd_down)
    sub.add_parser("list").set_defaults(func=cmd_list)

    ssh_p = sub.add_parser("ssh")
    ssh_p.add_argument("node", help="Node index (1..4)")
    ssh_p.add_argument("cmd", nargs="*", help="Optional command")
    ssh_p.set_defaults(func=cmd_ssh)

    exec_p = sub.add_parser("exec")
    exec_p.add_argument("node")
    exec_p.add_argument("cmd", nargs="+")
    exec_p.set_defaults(func=cmd_exec)

    sub.add_parser("reset").set_defaults(func=cmd_reset)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
