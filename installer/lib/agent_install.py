"""Install agent stack on secondary nodes (`bedrock join`).

Registers with the cluster's mgmt API, deploys exporters.
"""

import json
import subprocess
import urllib.request
from pathlib import Path
from . import state, exporters


def _register(mgmt_url: str, name: str, host: str, drbd_ip: str):
    payload = json.dumps({"name": name, "host": host, "drbd_ip": drbd_ip,
                          "role": "compute"}).encode()
    req = urllib.request.Request(
        f"{mgmt_url}/api/nodes/register", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    r = urllib.request.urlopen(req, timeout=5)
    return json.loads(r.read())


def install(witness: str, cluster_info: dict, repo: str):
    s = state.load()
    hw = s.get("hardware", {})

    # Pick local IPs
    mgmt_ip = ""
    drbd_ip = ""
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["name"] == "br0" and n["ip"]:
            mgmt_ip = n["ip"]
        elif n["state"] == "UP" and n.get("ip", "").startswith("10.99."):
            drbd_ip = n["ip"]
    if not mgmt_ip:
        for n in hw.get("nics", []):
            if n["state"] == "UP" and n["ip"] and not n["ip"].startswith("10."):
                mgmt_ip = n["ip"]; break

    # Save state
    existing = cluster_info.get("nodes", [])
    s.update({
        "cluster_name": cluster_info.get("cluster_name", "bedrock"),
        "cluster_uuid": cluster_info.get("cluster_uuid", "unknown"),
        "role": "compute",
        "node_id": len(existing),
        "node_name": hw.get("hostname", f"node{len(existing)+1}"),
        "witness_host": witness,
        "mgmt_url": cluster_info.get("mgmt_url") or f"http://{witness}:8080",
        "mgmt_ip": mgmt_ip,
        "drbd_ip": drbd_ip,
    })
    state.save(s)

    # Deploy exporters first — register makes mgmt rewrite scrape.yml to include us
    print("  Installing exporters...")
    exporters.install(repo)

    print(f"  Registering with mgmt at {s['mgmt_url']}...")
    result = _register(s["mgmt_url"], s["node_name"], mgmt_ip, drbd_ip)
    print(f"  Registered. Cluster now has {len(result.get('nodes', []))} nodes.")

    # Pre-scan peer host keys so `virsh migrate` via qemu+ssh works on first try.
    peer_ips = result.get("peer_ips", [])
    if peer_ips:
        Path("/root/.ssh").mkdir(mode=0o700, exist_ok=True)
        for ip in peer_ips:
            subprocess.run(
                f"ssh-keyscan -H -T 3 {ip} >> /root/.ssh/known_hosts 2>/dev/null",
                shell=True, check=False)
        subprocess.run(
            "sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts",
            shell=True, check=False)
        print(f"  Pre-scanned {len(peer_ips)} peer host keys.")

    print()
    print(f"  Joined cluster {s['cluster_name']} as node {s['node_id']}.")
    print(f"  Dashboard: {s['mgmt_url']}")
