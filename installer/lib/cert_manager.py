"""Refresh the dashboard's TLS certificate from local-ip.co.

The dashboard URL on each node is

    https://<dashed-ip>.my.local-ip.co:8443/

where `<dashed-ip>` is the node's primary LAN IP with dots replaced by
dashes (e.g. `192-168-2-60.my.local-ip.co`). local-ip.co holds a free
wildcard certificate for `*.my.local-ip.co` and publishes both the
cert and the private key for anyone to download.

Why a public-key cert is OK here. The threat model that matters on a
small operator LAN is "someone is already MITM-ing my traffic" (ARP
spoofing, rogue DHCP, …). At that level the attacker could also serve
a self-signed cert and most users would click through. A
publicly-trusted cert that matches the hostname instead gives the
green padlock by default — operators stop training themselves to
ignore browser warnings, which is the *actual* security win.

Idempotency. `openssl x509 -checkend N` returns 0 if the cert is
valid at least N seconds from now. The script runs daily via a
systemd timer; if the existing cert is good for at least 30 more
days it does nothing. Otherwise it downloads the latest cert+key,
writes atomically, and restarts `bedrock-mgmt.service` so the
dashboard picks up the new files.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import urllib.request
from pathlib import Path


SERVER_PEM_URL = "https://local-ip.co/cert/server.pem"
SERVER_KEY_URL = "https://local-ip.co/cert/server.key"
CHAIN_PEM_URL  = "https://local-ip.co/cert/chain.pem"

TLS_DIR   = Path("/etc/bedrock/tls")
CERT_PATH = TLS_DIR / "cert.pem"
KEY_PATH  = TLS_DIR / "key.pem"

RENEW_DAYS = 30


def needs_refresh() -> bool:
    """True if cert is missing or expires within RENEW_DAYS."""
    if not CERT_PATH.exists() or not KEY_PATH.exists():
        return True
    r = subprocess.run(
        ["openssl", "x509", "-in", str(CERT_PATH), "-noout",
         "-checkend", str(RENEW_DAYS * 86400)],
        capture_output=True, text=True,
    )
    return r.returncode != 0


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=20) as r:
        return r.read()


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    tmp.replace(path)


def derive_hostname() -> str:
    """`<lan-ip-dashed>.my.local-ip.co`.

    Uses the kernel's outbound-route lookup to find the IP we'd use
    to reach the wider network — that's the LAN-facing IP regardless
    of how many other interfaces the node has."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 1))            # no packet sent
        ip = s.getsockname()[0]
    finally:
        s.close()
    return f"{ip.replace('.', '-')}.my.local-ip.co"


def refresh() -> None:
    cert  = download(SERVER_PEM_URL)
    key   = download(SERVER_KEY_URL)
    try:
        chain = download(CHAIN_PEM_URL)
    except Exception:
        chain = b""                           # cert.pem alone is fine
    full = cert + (b"\n" + chain if chain else b"")
    write_atomic(CERT_PATH, full, 0o644)
    write_atomic(KEY_PATH,  key,  0o600)
    print(f"cert-refresh: wrote {CERT_PATH} + {KEY_PATH}",
          file=sys.stderr, flush=True)


def restart_mgmt() -> None:
    subprocess.run(["systemctl", "restart", "bedrock-mgmt.service"],
                   check=False)


def main() -> int:
    if not needs_refresh():
        print("cert-refresh: existing cert good for > 30 days; nothing to do",
              file=sys.stderr, flush=True)
        return 0
    refresh()
    print(f"cert-refresh: dashboard available at "
          f"https://{derive_hostname()}:8443/",
          file=sys.stderr, flush=True)
    restart_mgmt()
    return 0


if __name__ == "__main__":
    sys.exit(main())
