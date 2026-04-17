"""Hardware detection — CPU, RAM, NICs, storage."""

import os
import socket
import subprocess


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def detect() -> dict:
    """Return a dict of detected hardware info."""
    hw = {
        "hostname": socket.gethostname(),
        "cpu_model": "",
        "vcpus": os.cpu_count() or 1,
        "ram_mb": 0,
        "nics": [],
        "root_disk_gb": 0,
        "has_virt": False,
    }

    # CPU model + virt flags (scan whole file, not just first occurrence)
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if not hw["cpu_model"] and line.startswith("model name"):
                    hw["cpu_model"] = line.split(":", 1)[1].strip()
                if line.startswith("flags"):
                    if " svm " in line or " vmx " in line:
                        hw["has_virt"] = True
    except FileNotFoundError:
        pass

    # RAM
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    kb = int(line.split()[1])
                    hw["ram_mb"] = kb // 1024
                    break
    except FileNotFoundError:
        pass

    # NICs (physical only — skip lo, virtual bridges, veth)
    for line in run("ip -o -br link").split("\n"):
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        state = parts[1]
        mac = parts[2] if len(parts) > 2 else ""
        if name == "lo" or name.startswith(("virbr", "veth", "docker", "br-", "tap", "vnet")):
            continue
        # Get IP if present
        ip = ""
        ip_line = run(f"ip -o -br addr show {name}")
        if ip_line:
            ip_parts = ip_line.split()
            if len(ip_parts) >= 3 and "/" in ip_parts[2]:
                ip = ip_parts[2].split("/")[0]
        hw["nics"].append({"name": name, "state": state, "mac": mac, "ip": ip})

    # Root disk size (primary block device)
    out = run("df -BG --output=size / | tail -1").replace("G", "").strip()
    try:
        hw["root_disk_gb"] = int(out)
    except ValueError:
        hw["root_disk_gb"] = 0

    # Find the block device of /
    root_dev_out = run("df --output=source / | tail -1")
    hw["root_device"] = root_dev_out

    return hw


def primary_nic(hw: dict) -> str:
    """Return the name of the primary (UP, has IP) NIC."""
    for n in hw["nics"]:
        if n["state"] == "UP" and n["ip"]:
            return n["name"]
    for n in hw["nics"]:
        if n["state"] == "UP":
            return n["name"]
    return ""


if __name__ == "__main__":
    import json
    print(json.dumps(detect(), indent=2))
