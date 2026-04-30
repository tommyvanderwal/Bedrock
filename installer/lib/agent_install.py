"""Install agent stack on secondary nodes (`bedrock join`).

Registers with the cluster's mgmt API, deploys exporters.
"""

import json
import subprocess
import urllib.request
from pathlib import Path
from . import state, exporters, tier_storage


def _register(mgmt_url: str, name: str, host: str, drbd_ip: str, pubkey: str):
    payload = json.dumps({"name": name, "host": host, "drbd_ip": drbd_ip,
                          "role": "compute", "pubkey": pubkey}).encode()
    req = urllib.request.Request(
        f"{mgmt_url}/api/nodes/register", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read())


def _install_peer_pubkeys(pubkeys: list):
    """Add each peer pubkey to /root/.ssh/authorized_keys (dedup)."""
    if not pubkeys:
        return
    authz = Path("/root/.ssh/authorized_keys")
    authz.parent.mkdir(mode=0o700, exist_ok=True)
    existing = authz.read_text() if authz.exists() else ""
    lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
    for pk in pubkeys:
        pk = pk.strip()
        if pk and pk not in lines:
            lines.append(pk)
    authz.write_text("\n".join(lines) + "\n")
    authz.chmod(0o600)


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

    existing = cluster_info.get("nodes", [])
    node_name = hw.get("hostname", f"node{len(existing)+1}")
    mgmt_url = cluster_info.get("mgmt_url") or f"http://{witness}:8080"

    # Deploy exporters first — register makes mgmt rewrite scrape.yml to include us
    print("  Installing exporters...")
    exporters.install(repo)

    # Read our own pubkey to send with register so mgmt (and peers) can SSH in.
    pub_path = Path("/root/.ssh/id_ed25519.pub")
    my_pubkey = pub_path.read_text().strip() if pub_path.exists() else ""

    # Register BEFORE saving state. If the master is unreachable we want
    # to surface the failure cleanly and leave state.json untouched, so
    # `bedrock join` can be retried on the next attempt instead of
    # refusing with "Already a member" (the symptom L28 documented when
    # registration failed mid-flight). Only commit cluster_uuid + the
    # other cluster-membership fields after register succeeds.
    print(f"  Registering with mgmt at {mgmt_url}...")
    result = _register(mgmt_url, node_name, mgmt_ip, drbd_ip, my_pubkey)
    print(f"  Registered. Cluster now has {len(result.get('nodes', []))} nodes.")

    # Now safe to commit state — registration was accepted.
    s.update({
        "cluster_name": cluster_info.get("cluster_name", "bedrock"),
        "cluster_uuid": cluster_info.get("cluster_uuid", "unknown"),
        "role": "compute",
        "node_id": len(existing),
        "node_name": node_name,
        "witness_host": witness,
        "mgmt_url": mgmt_url,
        "mgmt_ip": mgmt_ip,
        "drbd_ip": drbd_ip,
    })
    state.save(s)

    # Install every peer's pubkey locally so mgmt + peers can SSH to this node.
    _install_peer_pubkeys(result.get("peer_pubkeys", []))

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

    # NFS mount the ISO library at /mnt/isos so --cdrom paths work identically
    # on every node in the cluster. Source is the mgmt node's host IP (parsed
    # from mgmt_url), NOT this node's own IP.
    print("  Installing NFS client + mounting ISO library...")
    subprocess.run("dnf install -y -q nfs-utils >/dev/null 2>&1",
                   shell=True, check=False)
    from urllib.parse import urlparse as _urlparse
    mgmt_host = _urlparse(s["mgmt_url"]).hostname or witness
    Path("/mnt/isos").mkdir(exist_ok=True)
    Path("/etc/systemd/system/mnt-isos.mount").write_text(
        "[Unit]\nDescription=Bedrock ISO library (NFS)\nAfter=network-online.target\n"
        "Wants=network-online.target\n\n"
        f"[Mount]\nWhat={mgmt_host}:/opt/bedrock/iso\nWhere=/mnt/isos\n"
        "Type=nfs\nOptions=ro,nolock,soft,_netdev\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    Path("/etc/systemd/system/mnt-isos.automount").write_text(
        "[Unit]\nDescription=Bedrock ISO library (automount)\n\n"
        "[Automount]\nWhere=/mnt/isos\nTimeoutIdleSec=300\n\n"
        "[Install]\nWantedBy=multi-user.target\n"
    )
    subprocess.run("systemctl daemon-reload", shell=True, check=False)
    subprocess.run("systemctl enable --now mnt-isos.automount >/dev/null 2>&1",
                   shell=True, check=False)

    # Storage tiers — N=1 setup on this node first (creates local LVs and
    # /bedrock/<tier> symlinks). Cluster-wide transition to N>=2 (Garage +
    # DRBD-NFS) is triggered separately via `bedrock storage promote`.
    print("  Setting up storage tiers (local LVs)...")
    try:
        tier_storage.setup_n1()
    except Exception as e:
        print(f"  WARN: tier setup failed: {e}")

    print()
    print(f"  Joined cluster {s['cluster_name']} as node {s['node_id']}.")
    print(f"  Dashboard: {s['mgmt_url']}")
    print(f"  Storage:   /bedrock/{{scratch,bulk,critical}} (local LVs)")
    print(f"  Promote to N>=2 from any node:  bedrock storage promote")
