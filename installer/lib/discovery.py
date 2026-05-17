"""Cluster discovery — find the witness on the LAN."""

import json
import socket
import ssl
import urllib.request
from typing import Optional, List

COMMON_WITNESS_IPS = [
    # Try a few likely locations
    "192.168.2.253",  # MikroTik switch
    "192.168.2.252",  # witness container on MikroTik
    "192.168.2.254",  # gateway
]
WITNESS_PORT = 9443
MGMT_PORT_HTTPS = 8443  # primary: mgmt serves HTTPS once cert-refresh has run
MGMT_PORT_HTTP  = 8080  # legacy: only used pre-cert (now rare)

# Self-signed-friendly context for HTTPS scans. The cert is issued for
# `<dashed-ip>.my.local-ip.co`, not the bare IP we're scanning, so name
# verification would always fail. Discovery only needs to fetch a public
# JSON endpoint — no secrets in transit — so disabling verification is safe.
_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE


def _open(url: str, timeout: float = 2.0):
    if url.startswith("https://"):
        return urllib.request.urlopen(url, timeout=timeout, context=_INSECURE_CTX)
    return urllib.request.urlopen(url, timeout=timeout)


def _can_reach(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _get_local_subnet_hosts() -> List[str]:
    """Return candidate host IPs in our /24 subnet."""
    import subprocess
    r = subprocess.run("ip -o -br addr show br0", shell=True, capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 3 and "." in parts[2]:
            ip = parts[2].split("/")[0]
            prefix = ".".join(ip.split(".")[:3]) + "."
            return [prefix + str(i) for i in range(1, 255)]
    return []


def find_witness() -> Optional[str]:
    """Try common witness locations. Returns 'host:port' or just 'host'.

    Looks for:
      - bedrock-witness on port 9443 (external)
      - bedrock-mgmt /cluster-info on port 8080 (self-hosted discovery)
    """
    # Quick check: common IPs with witness
    for host in COMMON_WITNESS_IPS:
        if _can_reach(host, WITNESS_PORT):
            try:
                r = urllib.request.urlopen(f"http://{host}:{WITNESS_PORT}/health", timeout=2)
                if r.status == 200:
                    return host
            except Exception:
                pass

    # Scan subnet for mgmt nodes — HTTPS 8443 first (current default once
    # cert-refresh has run), HTTP 8080 fallback for pre-cert installs.
    subnet_hosts = _get_local_subnet_hosts()[:50]  # first 50 IPs quickly
    for host in subnet_hosts:
        for port, scheme in ((MGMT_PORT_HTTPS, "https"), (MGMT_PORT_HTTP, "http")):
            if not _can_reach(host, port, timeout=0.3):
                continue
            try:
                r = _open(f"{scheme}://{host}:{port}/cluster-info", timeout=1)
                if r.status == 200:
                    return host
            except Exception:
                pass

    return None


def query_cluster(host: str) -> Optional[dict]:
    """Query for cluster info. Tries witness (9443), HTTPS mgmt (8443),
    then legacy HTTP mgmt (8080)."""
    # Try witness
    try:
        r = _open(f"http://{host}:{WITNESS_PORT}/cluster-info", timeout=3)
        return json.loads(r.read())
    except Exception:
        pass
    # Try mgmt dashboard /cluster-info — HTTPS first, HTTP fallback.
    for port, scheme in ((MGMT_PORT_HTTPS, "https"), (MGMT_PORT_HTTP, "http")):
        try:
            r = _open(f"{scheme}://{host}:{port}/cluster-info", timeout=3)
            return json.loads(r.read())
        except Exception:
            pass
    # Fallback: witness /status only
    try:
        r = urllib.request.urlopen(f"http://{host}:{WITNESS_PORT}/status", timeout=3)
        status = json.loads(r.read())
        nodes = list(status.get("nodes", {}).keys())
        return {
            "cluster_name": "bedrock",
            "cluster_uuid": "unknown",
            "nodes": nodes,
            "witness_host": host,
        }
    except Exception:
        return None


def register(witness: str, my_name: str, my_ip: str) -> bool:
    """Register this node with the cluster witness."""
    try:
        req = urllib.request.Request(
            f"http://{witness}:{WITNESS_PORT}/register",
            data=json.dumps({"name": my_name, "ip": my_ip}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        r = urllib.request.urlopen(req, timeout=5)
        return r.status == 200
    except Exception:
        return True  # witness may not support /register yet; heartbeat will register
