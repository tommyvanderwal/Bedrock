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

# Bedrock installer ISO — built by installer/iso-build/build-iso.sh.
# Testbed uses the offline variant (no S3 dependency at install
# time). For dev-build naming see docs/install-and-iso.md.
BEDROCK_ISO = (TESTBED.parent / "installer/iso-build/output/"
                                "bedrock-installer-dev-offline.iso")
# (Old cloud-image path — unused by default but useful when iterating
# on bedrock-bootstrap WITHOUT rebuilding the ISO every time. Spawn
# with BEDROCK_TESTBED_USE_CLOUD_IMG=1 to switch back.)
GOLDEN_IMG = IMAGES_DIR / "almalinux-10.qcow2"
ALMA_URL = "https://repo.almalinux.org/almalinux/10/cloud/x86_64/images/AlmaLinux-10-GenericCloud-latest.x86_64.qcow2"
USE_CLOUD_IMG = os.environ.get("BEDROCK_TESTBED_USE_CLOUD_IMG") == "1"

MAX_NODES = 4
NODE_RAM_MB = 12288
NODE_VCPUS = 4
# Single disk per sim — mirrors the real-lab mini-PC layout (one
# NVMe per node). AlmaLinux's cloud image installs the OS into XFS
# straight on the partition (no LVM); bedrock-bootstrap converts the
# layout to a single VG + thin pool taking the rest of the disk.
# 130 GB gives the OS ~16 GB headroom plus ~110 GB of thin-pool space
# for tiers + VM disks — the mini-PC equivalent at scale-down.
NODE_DISK_GB = 130

MGMT_NET = "bedrock-mgmt"
DRBD_NET = "bedrock-drbd"
# Mesh networks: every sim plugs into all three. Each is an isolated L2
# segment, so every sim pair shares a path through each of them. Yank
# any one with `virsh net-destroy bedrock-mesh-<n>` to simulate a cable
# pull on that plane; restore with `net-start`. Three planes is enough
# to exercise multi-path discovery, prio ordering, and chaos failover
# without making the libvirt domain XML unreasonable.
MESH_NETS = ["bedrock-mesh-1", "bedrock-mesh-2", "bedrock-mesh-3"]

# Static LAN IPs for the sims (br0). The home router's DHCP pool ends
# at .200, so .201-.210 is collision-safe. Sims do NOT request DHCP
# leases — the home router stays the only DHCP server on the LAN.
MGMT_PREFIX = "192.168.2"      # node i gets MGMT_PREFIX + .{200+i}
MGMT_GATEWAY = "192.168.2.254"
MGMT_DNS = "192.168.2.254"


def mgmt_ip(i: int) -> str:
    return f"{MGMT_PREFIX}.{200 + i}"

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

    # Legacy cloud-init path (the bedrock-install ISO path is the
    # primary one). DRBD now rides the cluster loopback /32 (mesh-
    # routed), so we no longer pre-seed per-node DRBD-NIC hosts —
    # entries below are placeholders for the now-unused template
    # field.
    hosts_entries = "\n".join(
        f"      # {node_name(j)} mesh loopback assigned at join time"
        for j in all_indices
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
                 .replace("{DRBD_IP}", "")
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

    if USE_CLOUD_IMG:
        # Cloud-image fast path (BEDROCK_TESTBED_USE_CLOUD_IMG=1) —
        # boots straight to a configured AlmaLinux, then bedrock-
        # bootstrap carves the LVM PV from the unallocated tail.
        # Useful when iterating on bedrock-bootstrap itself without
        # rebuilding the ISO.
        if not GOLDEN_IMG.exists():
            print(f"  cloud image missing, run `spawn.py prereqs` first")
            sys.exit(1)
        disk_path = node_state / "root.qcow2"
        if not disk_path.exists():
            print(f"  Creating {NODE_DISK_GB}GB qcow2 (cloud-image overlay) for {hostname}...")
            run(f"qemu-img create -f qcow2 -F qcow2 -b {GOLDEN_IMG} "
                f"{disk_path} {NODE_DISK_GB}G", capture=False)
        iso_path = make_cloud_init(i, all_indices)
        print(f"  Defining {hostname} (cloud-image)...")
        run(["sudo", "virt-install",
             "--name", hostname,
             "--memory", str(NODE_RAM_MB),
             "--vcpus", str(NODE_VCPUS),
             "--cpu", "host-passthrough",
             "--disk", f"path={disk_path},format=qcow2,bus=virtio,discard=unmap",
             "--disk", f"path={iso_path},device=cdrom",
             "--network", f"network={MGMT_NET},model=virtio",
             "--network", f"network={DRBD_NET},model=virtio",
             *[a for mesh in MESH_NETS
                 for a in ("--network", f"network={mesh},model=virtio")],
             "--os-variant", "almalinux10",
             "--graphics", "none",
             "--console", "pty,target_type=serial",
             "--import",
             "--noautoconsole",
             "--noreboot",
            ])
        virsh("start", hostname, capture=False)
        return

    # Default: install via the bedrock-install ISO — same install
    # path real hardware will use. virt-install boots the ISO,
    # anaconda runs the kickstart, partitions per single-disk-VG-
    # thinpool layout, %post stages the bedrock payload + arms
    # bedrock-firstboot.service, reboots, firstboot runs install.sh
    # against the local payload. Result: a node ready for
    # `bedrock init` or `bedrock join` with no manual steps.
    if not BEDROCK_ISO.exists():
        print(f"  bedrock-install ISO missing: {BEDROCK_ISO}")
        print(f"  build it first: ../installer/iso-build/build-iso.sh")
        sys.exit(1)

    disk_path = node_state / "root.qcow2"
    if not disk_path.exists():
        print(f"  Creating {NODE_DISK_GB}GB blank qcow2 for {hostname}...")
        run(f"qemu-img create -f qcow2 {disk_path} {NODE_DISK_GB}G",
            capture=False)

    print(f"  Defining {hostname} (bedrock-install ISO)...")
    # Network NIC order matters for predictable interface naming inside the
    # guest: enp1s0 = mgmt LAN, enp2s0 = legacy DRBD bridge (kept for
    # transitional state — bedrock-net will treat it as just another mesh
    # plane), enp3s0/4/5 = bedrock-mesh-{1,2,3}. Every sim is on every mesh
    # plane, giving 4 paths between every pair plus the LAN.
    net_args = []
    net_args += ["--network", f"network={MGMT_NET},model=virtio"]
    net_args += ["--network", f"network={DRBD_NET},model=virtio"]
    for mesh in MESH_NETS:
        net_args += ["--network", f"network={mesh},model=virtio"]
    run(["sudo", "virt-install",
         "--name", hostname,
         "--memory", str(NODE_RAM_MB),
         "--vcpus", str(NODE_VCPUS),
         "--cpu", "host-passthrough",
         "--disk", f"path={disk_path},format=qcow2,bus=virtio,discard=unmap",
         "--cdrom", str(BEDROCK_ISO),
         *net_args,
         "--os-variant", "almalinux10",
         "--graphics", "none",
         "--console", "pty,target_type=serial",
         "--noautoconsole",
         "--noreboot",
        ])
    # virt-install with --cdrom auto-starts the domain and runs the
    # installer. After --noreboot, anaconda's `reboot --eject` shuts
    # the VM off; we explicitly start it again to boot from the
    # newly-installed disk.
    # (No virsh start here — it would race with anaconda still
    # finishing its post-install. Operator runs `spawn.py up N` again
    # after install completes, OR uses `spawn.py wait <i>` — added
    # below.)


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

def disable_mesh_snooping():
    """Linux bridges with multicast_snooping=1 + no IGMP querier silently
    drop multicast to ports that haven't sent IGMP joins. The mesh
    bridges in the testbed have no querier (libvirt isolated networks
    don't run one), so probes don't cross the bridge between vnets.

    Safe to call repeatedly — virsh net-start resets the bridge knobs,
    so the chaos harness also calls this on each restore."""
    for n in MESH_NETS:
        # MESH_NETS list has libvirt names like "bedrock-mesh-1"; the
        # bridge is br-bedmesh<n> per the XML.
        bridge = "br-bed" + n.replace("bedrock-", "").replace("-", "")
        path = f"/sys/class/net/{bridge}/bridge/multicast_snooping"
        try:
            with open(path) as f:
                if f.read().strip() != "0":
                    subprocess.run(["sudo", "sh", "-c", f"echo 0 > {path}"],
                                   capture_output=True, check=False)
        except Exception:
            pass
    # Also for bedrock-drbd which is another isolated bridge.
    for path in ("/sys/class/net/br-beddrbd/bridge/multicast_snooping",):
        try:
            with open(path) as f:
                if f.read().strip() != "0":
                    subprocess.run(["sudo", "sh", "-c", f"echo 0 > {path}"],
                                   capture_output=True, check=False)
        except Exception:
            pass


def cmd_up(args):
    target = int(args.count)
    if target < 0 or target > MAX_NODES:
        print(f"N must be 0..{MAX_NODES}")
        sys.exit(1)
    # Make sure mesh bridges forward multicast (probe traffic).
    disable_mesh_snooping()

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


def cmd_wait(args):
    """Wait for anaconda install to complete on each running sim.

    Anaconda's `reboot --eject` in the kickstart shuts the VM off
    when the install finishes. We poll for that, then start the VM
    again so it boots from the just-installed disk and runs the
    bedrock-firstboot.service.
    """
    import time as _time
    target = int(args.count)
    if target < 1 or target > MAX_NODES:
        print(f"count must be 1..{MAX_NODES}")
        sys.exit(1)
    deadline = _time.monotonic() + 1800  # 30 minutes
    for i in range(1, target + 1):
        name = node_name(i)
        if not node_exists(i):
            print(f"  {name}: not defined, skipping")
            continue
        print(f"  {name}: waiting for anaconda to finish (reboot --eject)…")
        while _time.monotonic() < deadline:
            out, _ = virsh("domstate", name)
            if out.strip() == "shut off":
                print(f"  {name}: install complete, starting…")
                virsh("start", name, capture=False)
                break
            _time.sleep(15)
        else:
            print(f"  {name}: TIMEOUT after 30 min — anaconda may be stuck")
            continue
    print("All sims started for first-boot. Open serial console with:")
    for i in range(1, target + 1):
        print(f"  spawn.py ssh {i}        # once SSH is up")


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

    Resolution order:
      1. `virsh domifaddr --source arp` — works for any running sim
         that's exchanged ARP on the bridge. Most reliable when
         bedrock-mgmt is bridged to the host LAN and DHCP from the
         home router landed somewhere unpredictable (not the
         hardcoded .201+i convention).
      2. `virsh domifaddr --source agent` — needs qemu-guest-agent
         installed in the guest. Falls back here if ARP empty.
      3. Hardcoded mgmt_ip(i) — last resort, only correct if the
         home router happens to honour the .201-.210 reservation.
    """
    hostname = node_name(i)
    if not node_exists(i):
        return None

    out, _ = virsh("domifaddr", hostname, "--source", "arp", capture=True)
    for line in (out or "").splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[0].startswith("vnet") and "/" in cols[-1]:
            ip = cols[-1].split("/")[0]
            if ip and not ip.startswith(("169.254.", "127.")) and "." in ip:
                # First non-link-local, non-loopback IPv4 wins —
                # the bedrock-mgmt NIC's vnet is the first one
                # in the XML, so its ARP entry is first.
                return ip

    # Look up the MGMT_NET MAC up-front so we can filter the
    # agent's per-NIC output (sim guests run libvirt themselves and
    # claim a 192.168.122.1 virbr0 that mustn't shadow the real
    # mgmt-bridge IP).
    iflist, _ = virsh("domiflist", hostname, capture=True)
    mgmt_mac = None
    for line in (iflist or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2] == MGMT_NET:
            mgmt_mac = parts[4].lower()
            break

    out, _ = virsh("domifaddr", hostname, "--source", "agent", capture=True)
    last_mac = ""
    for line in (out or "").splitlines():
        cols = line.split()
        if len(cols) >= 4 and ":" in (cols[1] if len(cols) >= 4 else ""):
            last_mac = cols[1].lower()
        if len(cols) >= 4 and "/" in cols[-1]:
            ip = cols[-1].split("/")[0]
            if (ip and not ip.startswith(("169.254.", "127.", "100."))
                and "." in ip):
                if mgmt_mac and last_mac and last_mac != mgmt_mac:
                    continue
                return ip

    # Fallback: host's `ip neigh` keyed on the bedrock-mgmt NIC MAC.
    # virsh domifaddr --source arp uses libvirt's own ARP cache which
    # can be stale; the host's neighbour table is always current.
    out, _ = virsh("domiflist", hostname, capture=True)
    mgmt_mac = None
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[2] == MGMT_NET:
            mgmt_mac = parts[4].lower()
            break
    if mgmt_mac:
        arp_out, _ = run(["ip", "neigh"], capture=True)
        for line in (arp_out or "").splitlines():
            cols = line.split()
            if len(cols) >= 5 and cols[4].lower() == mgmt_mac:
                ip = cols[0]
                if ip and not ip.startswith(("169.254.", "127.")):
                    return ip

        # Stale ARP cache (e.g. sim's DHCP lease changed after a
        # restart). Force a quick parallel ping-sweep of the bedrock
        # bridge's /24 to populate the host's neighbour table, then
        # re-check. Without this, an unreachable virsh-reported IP
        # gets returned by the fallback below and every sssh fails
        # silently against the stale address.
        import subprocess as _sp
        sweep = _sp.Popen(
            "for o in $(seq 30 230); do "
            "ping -c1 -W1 192.168.2.$o >/dev/null 2>&1 & done; wait",
            shell=True,
        )
        try:
            sweep.wait(timeout=8)
        except _sp.TimeoutExpired:
            sweep.kill()
        arp_out, _ = run(["ip", "neigh"], capture=True)
        for line in (arp_out or "").splitlines():
            cols = line.split()
            if len(cols) >= 5 and cols[4].lower() == mgmt_mac:
                ip = cols[0]
                if ip and not ip.startswith(("169.254.", "127.")):
                    return ip

    # Last-resort fallback: convention .201+i. Often wrong post-test
    # (DHCP renumbers); only correct on the very first install.
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

    wait_p = sub.add_parser("wait",
        help="Wait until a sim's anaconda install finishes "
             "(VM shuts off via reboot --eject), then start the VM "
             "to boot the just-installed system + run firstboot.")
    wait_p.add_argument("count", help="Number of nodes to wait for (1..4)")
    wait_p.set_defaults(func=cmd_wait)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
