"""VM lifecycle — create, migrate, delete.

Workload types:
  cattle — local LV, no DRBD, no migration possible
  pet    — DRBD 2-way replicated, live migration supported
  vipet  — DRBD 3-way replicated (needs 3+ nodes)
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
from pathlib import Path
from . import workload


ALPINE_URL = "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/nocloud_alpine-3.21.0-x86_64-bios-cloudinit-r0.qcow2"
VG_NAME = "almalinux"  # LVM VG for VM disks — matches the physical lab convention
THIN_POOL = "thinpool"


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed:\n{r.stderr}")
    return r.stdout.strip()


def run_on(host: str, cmd: str, check=True):
    """Run command on a remote node via SSH."""
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "BatchMode=yes",
               f"root@{host}", cmd]
    r = subprocess.run(ssh_cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"ssh {host}: {cmd} failed:\n{r.stderr}")
    return r.stdout.strip()


def _cluster() -> dict:
    p = Path("/etc/bedrock/cluster.json")
    if p.exists():
        return json.loads(p.read_text())
    return {"nodes": {}}


def _api_get(state, path: str) -> dict:
    url = state.get("mgmt_url", "http://localhost:8080") + path
    r = urllib.request.urlopen(url, timeout=5)
    return json.loads(r.read())


def _api_post(state, path: str, body: dict = None) -> dict:
    url = state.get("mgmt_url", "http://localhost:8080") + path
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    r = urllib.request.urlopen(req, timeout=30)
    return json.loads(r.read())


def _ensure_thin_pool(host: str, size_gb: int = 80):
    """Make sure there's an LVM thin pool on the node. Creates a loop-backed
    pool if nothing suitable exists — works for nested testbed without extra
    disks. Default 80 GB sparse loop file on a 100 GB sim-node root disk is
    enough for a Windows install (25 GB disk) + a couple of Linux VMs.
    """
    # Does the thin pool already exist?
    out = run_on(host, f"lvs --noheadings -o lv_name {VG_NAME} 2>/dev/null || true",
                 check=False)
    if THIN_POOL in out.split():
        return
    # Does the VG exist?
    vg_out = run_on(host, f"vgs --noheadings -o vg_name 2>/dev/null || true", check=False)
    if VG_NAME not in vg_out.split():
        # Create a loop-backed VG for testing
        print(f"  Creating loop-backed VG on {host} ({size_gb} GB sparse)...")
        run_on(host, f"""
            truncate -s {size_gb}G /var/lib/bedrock-vg.img
            LOOP=$(losetup --find --show /var/lib/bedrock-vg.img)
            pvcreate -f -y $LOOP >/dev/null
            vgcreate {VG_NAME} $LOOP >/dev/null
        """)
    # Create thin pool — use all remaining VG space (minus 1G slack)
    print(f"  Creating thin pool on {host}...")
    run_on(host, f"lvcreate -y -l '95%FREE' --thinpool {THIN_POOL} {VG_NAME}")


def list_vms(state):
    """Show VMs across the cluster via mgmt API."""
    try:
        data = _api_get(state, "/api/cluster")
    except Exception as e:
        print(f"ERROR: cannot reach mgmt API: {e}")
        return
    vms = data.get("vms", {})
    if not vms:
        print("(no VMs)")
        return
    print(f"{'NAME':<20} {'STATE':<10} {'NODE':<30} {'DRBD':<20}")
    for name, vm in vms.items():
        print(f"{name:<20} {vm['state']:<10} {vm.get('running_on','-') or '-':<30} "
              f"{vm.get('drbd_resource','-') or '-':<20}")


def create_vm(state, name: str, vm_type: str, ram: int, disk: int):
    """Create a VM of the given workload type."""
    cluster = _cluster()
    nodes = cluster.get("nodes", {})
    node_count = len(nodes)

    ok, msg = workload.validate_type(vm_type, node_count)
    if not ok:
        print(f"ERROR: {msg}")
        return 1

    cfg = workload.WORKLOAD_TYPES[vm_type]
    replicas = cfg["replicas"]
    home_node_name = state.get("node_name")
    home_node = nodes.get(home_node_name, {})
    if not home_node:
        print(f"ERROR: this node ({home_node_name}) not in cluster config")
        return 1

    home_host = home_node["host"]

    print(f"Creating {vm_type} VM '{name}' (RAM={ram}MB, disk={disk}GB, replicas={replicas}) on {home_node_name}")

    _ensure_thin_pool(home_host)

    if vm_type == "cattle":
        # Single local LV + simple Alpine cloud image
        _create_cattle(home_host, name, ram, disk)
    elif vm_type == "pet":
        # DRBD 2-way: need home + one peer
        peers = [n for n in nodes if n != home_node_name][:1]
        if not peers:
            print("ERROR: need a peer node for pet VM")
            return 1
        _create_pet(home_host, nodes[peers[0]]["host"], home_node_name, peers[0], name, ram, disk)
    elif vm_type == "vipet":
        peers = [n for n in nodes if n != home_node_name][:2]
        if len(peers) < 2:
            print("ERROR: need 2 peer nodes for vipet VM")
            return 1
        _create_vipet(nodes, home_node_name, peers, name, ram, disk)
    print(f"  VM {name} created. Status: bedrock vm list")


def _download_alpine_on_node(host: str):
    """Download the Alpine cloud image to the node (once). Cached at /var/lib/bedrock/."""
    run_on(host, f"""
        mkdir -p /var/lib/bedrock
        test -f /var/lib/bedrock/alpine.qcow2 || \\
          curl -sfL -o /var/lib/bedrock/alpine.qcow2 "{ALPINE_URL}"
    """)


def _create_cattle(host: str, name: str, ram: int, disk: int):
    """Simple cattle VM: local thin LV + Alpine base, no DRBD, no migration."""
    lv_name = f"vm-{name}-disk0"
    print(f"  Creating thin LV {lv_name} ({disk}GB)...")
    run_on(host, f"lvcreate -y -V {disk}G --thin -n {lv_name} {VG_NAME}/{THIN_POOL} >/dev/null 2>&1")

    print("  Downloading Alpine image...")
    _download_alpine_on_node(host)

    print("  Writing image to LV...")
    run_on(host, f"qemu-img convert -f qcow2 -O raw /var/lib/bedrock/alpine.qcow2 /dev/{VG_NAME}/{lv_name}")

    print("  Defining VM...")
    run_on(host, f"""
        virt-install \\
          --name {name} \\
          --ram {ram} \\
          --vcpus 1 \\
          --disk path=/dev/{VG_NAME}/{lv_name},format=raw,bus=virtio,cache=none \\
          --network bridge=br0,model=virtio \\
          --os-variant alpinelinux3.18 \\
          --boot hd \\
          --graphics vnc,listen=0.0.0.0 \\
          --noautoconsole \\
          --import 2>&1 | tail -5
    """)


def _create_pet(host_a: str, host_b: str, name_a: str, name_b: str,
                vm_name: str, ram: int, disk: int):
    """DRBD 2-way replicated pet VM."""
    lv_name = f"vm-{vm_name}-disk0"
    minor = _next_drbd_minor(host_a)
    port = 7789 + minor

    print(f"  Creating thin LV {lv_name} on both hosts...")
    for h in (host_a, host_b):
        _ensure_thin_pool(h)
        run_on(h, f"lvcreate -y -V {disk}G --thin -n {lv_name} {VG_NAME}/{THIN_POOL} >/dev/null 2>&1")

    print(f"  Writing DRBD resource (minor={minor}, port={port})...")
    drbd_conf = _drbd_2way_conf(vm_name, minor, port, name_a, name_b, host_a, host_b)
    for h in (host_a, host_b):
        run_on(h, f"cat > /etc/drbd.d/vm-{vm_name}-disk0.res << 'EOF'\n{drbd_conf}\nEOF")
        run_on(h, f"drbdadm create-md --force --max-peers=7 vm-{vm_name}-disk0 2>&1 | tail -2", check=False)
        run_on(h, f"drbdadm up vm-{vm_name}-disk0", check=False)

    run_on(host_a, f"drbdadm primary --force vm-{vm_name}-disk0", check=False)

    print("  Loading Alpine image on primary...")
    _download_alpine_on_node(host_a)
    run_on(host_a, f"qemu-img convert -f qcow2 -O raw /var/lib/bedrock/alpine.qcow2 /dev/drbd{minor}")

    print("  Defining VM on both nodes...")
    vm_xml = _vm_xml_pet(vm_name, ram, minor)
    for h in (host_a, host_b):
        run_on(h, f"cat > /tmp/{vm_name}.xml << 'EOF'\n{vm_xml}\nEOF")
        run_on(h, f"virsh define /tmp/{vm_name}.xml")


def _create_vipet(nodes: dict, home_name: str, peer_names: list, vm_name: str,
                  ram: int, disk: int):
    """DRBD 3-way replicated vipet VM."""
    all_names = [home_name] + peer_names[:2]
    hosts = [nodes[n]["host"] for n in all_names]
    lv_name = f"vm-{vm_name}-disk0"
    minor = _next_drbd_minor(hosts[0])
    port = 7789 + minor

    print(f"  Creating thin LV on {len(hosts)} nodes...")
    for h in hosts:
        _ensure_thin_pool(h)
        run_on(h, f"lvcreate -y -V {disk}G --thin -n {lv_name} {VG_NAME}/{THIN_POOL} >/dev/null 2>&1")

    print(f"  Writing 3-way DRBD resource (minor={minor}, port={port})...")
    drbd_conf = _drbd_3way_conf(vm_name, minor, port, all_names, hosts)
    for h in hosts:
        run_on(h, f"cat > /etc/drbd.d/vm-{vm_name}-disk0.res << 'EOF'\n{drbd_conf}\nEOF")
        run_on(h, f"drbdadm create-md --force --max-peers=7 vm-{vm_name}-disk0 2>&1 | tail -2", check=False)
        run_on(h, f"drbdadm up vm-{vm_name}-disk0", check=False)

    run_on(hosts[0], f"drbdadm primary --force vm-{vm_name}-disk0", check=False)

    print("  Loading Alpine image on primary...")
    _download_alpine_on_node(hosts[0])
    run_on(hosts[0], f"qemu-img convert -f qcow2 -O raw /var/lib/bedrock/alpine.qcow2 /dev/drbd{minor}")

    print("  Defining VM on all 3 nodes...")
    vm_xml = _vm_xml_pet(vm_name, ram, minor)
    for h in hosts:
        run_on(h, f"cat > /tmp/{vm_name}.xml << 'EOF'\n{vm_xml}\nEOF")
        run_on(h, f"virsh define /tmp/{vm_name}.xml")


def _next_drbd_minor(host: str) -> int:
    """Find the next available DRBD minor (scans /etc/drbd.d/*.res + running devices)."""
    out = run_on(host, "ls /dev/drbd* 2>/dev/null | grep -oP 'drbd\\K[0-9]+' || true")
    used = {int(x) for x in out.split() if x.isdigit()}
    # Also scan configured resources
    out = run_on(host, "grep -hr 'minor ' /etc/drbd.d/*.res 2>/dev/null | grep -oP 'minor\\s+\\K[0-9]+' || true")
    used.update(int(x) for x in out.split() if x.isdigit())
    for i in range(1, 100):
        if i not in used:
            return i
    return 99


def _drbd_2way_conf(vm_name: str, minor: int, port: int,
                    node_a: str, node_b: str, ip_a: str, ip_b: str) -> str:
    res = f"vm-{vm_name}-disk0"
    return f"""resource {res} {{
    protocol C;
    disk {{ on-io-error detach; }}
    net {{
        allow-two-primaries no;
        after-sb-0pri discard-zero-changes;
        after-sb-1pri discard-secondary;
        after-sb-2pri disconnect;
    }}
    on {node_a} {{
        device /dev/drbd{minor} minor {minor};
        disk /dev/{VG_NAME}/vm-{vm_name}-disk0;
        node-id 0;
    }}
    on {node_b} {{
        device /dev/drbd{minor} minor {minor};
        disk /dev/{VG_NAME}/vm-{vm_name}-disk0;
        node-id 1;
    }}
    connection {{
        path {{
            host {node_a} address {ip_a}:{port};
            host {node_b} address {ip_b}:{port};
        }}
    }}
}}"""


def _drbd_3way_conf(vm_name: str, minor: int, port: int,
                    names: list, hosts: list) -> str:
    """DRBD 3-way config. Each pair of nodes has an explicit connection block
    (full mesh requires all 3 pairs: 0-1, 0-2, 1-2)."""
    res = f"vm-{vm_name}-disk0"
    on_blocks = "\n".join(
        f"""    on {names[i]} {{
        device /dev/drbd{minor} minor {minor};
        disk /dev/{VG_NAME}/vm-{vm_name}-disk0;
        node-id {i};
    }}""" for i in range(3)
    )
    # Explicit connection blocks for each pair in the mesh
    connection_blocks = []
    pairs = [(0, 1), (0, 2), (1, 2)]
    for a, b in pairs:
        connection_blocks.append(f"""    connection {{
        path {{
            host {names[a]} address {hosts[a]}:{port};
            host {names[b]} address {hosts[b]}:{port};
        }}
    }}""")
    conn_str = "\n".join(connection_blocks)
    return f"""resource {res} {{
    protocol C;
    disk {{ on-io-error detach; }}
    net {{ allow-two-primaries no; }}
{on_blocks}
{conn_str}
}}"""


def _vm_xml_pet(vm_name: str, ram: int, minor: int) -> str:
    import uuid as _u
    uid = str(_u.uuid4())
    return f"""<domain type='kvm'>
  <name>{vm_name}</name>
  <uuid>{uid}</uuid>
  <memory unit='MiB'>{ram}</memory>
  <currentMemory unit='MiB'>{ram}</currentMemory>
  <vcpu>1</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <devices>
    <emulator>/usr/libexec/qemu-kvm</emulator>
    <disk type='block' device='disk'>
      <driver name='qemu' type='raw' cache='none' io='native' discard='unmap'/>
      <source dev='/dev/drbd{minor}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='bridge'>
      <source bridge='br0'/>
      <model type='virtio'/>
    </interface>
    <graphics type='vnc' port='-1' autoport='yes' listen='0.0.0.0'/>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>"""


def migrate_vm(state, name: str, target: str):
    try:
        result = _api_post(state, f"/api/vms/{name}/migrate",
                          {"target_node": target} if target else None)
        print(f"Migration: {result}")
    except Exception as e:
        print(f"ERROR: {e}")


def delete_vm(state, name: str):
    """Destroy the VM on whichever node it runs on + cleanup disks on all defined nodes."""
    try:
        cluster = _api_get(state, "/api/cluster")
    except Exception as e:
        print(f"ERROR: {e}")
        return
    vm = cluster.get("vms", {}).get(name)
    if not vm:
        print(f"VM {name} not found.")
        return

    # Stop if running
    if vm["state"] == "running":
        _api_post(state, f"/api/vms/{name}/poweroff")
        time.sleep(2)

    # Undefine + wipe disk on every defined node
    nodes = _cluster().get("nodes", {})
    for nname in vm.get("defined_on", []):
        host = nodes.get(nname, {}).get("host", "")
        if not host: continue
        run_on(host, f"virsh undefine {name} --remove-all-storage 2>&1", check=False)
        run_on(host, f"drbdadm down vm-{name}-disk0 2>/dev/null", check=False)
        run_on(host, f"rm -f /etc/drbd.d/vm-{name}-disk0.res", check=False)
        run_on(host, f"lvremove -f {VG_NAME}/vm-{name}-disk0 2>/dev/null", check=False)

    print(f"VM {name} deleted.")
