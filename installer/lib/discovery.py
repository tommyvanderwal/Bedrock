"""Cluster discovery — find Bedrock clusters on the LAN.

Primary mechanism is **mDNS multicast** (UDP/5353, group 224.0.0.251)
of ``bedrock.local`` querying for type ANY. Every Bedrock node runs
``bedrock-mdns`` which answers with an A record (its LAN IP) AND a
TXT record (``cluster_uuid``, ``cluster_name``, ``node_name``). The
joiner collects every response received within ~2 s, dedups by IP,
and presents a list to the operator.

Fall-back path stays for legacy / dev-box networks where mDNS is
blocked: subnet scan + hardcoded MikroTik witness IPs.
"""

from __future__ import annotations

import json
import socket
import ssl
import struct
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional


COMMON_WITNESS_IPS = [
    # Legacy: external witness on a MikroTik / appliance. Pre-rewrite
    # default; kept as a fallback so existing deployments still work.
    "192.168.2.253",
    "192.168.2.252",
    "192.168.2.254",
]
WITNESS_PORT     = 9443
MGMT_PORT_HTTPS  = 8443
MGMT_PORT_HTTP   = 8080
MDNS_GROUP       = "224.0.0.251"
MDNS_PORT        = 5353
MDNS_NAME        = b"bedrock.local"


# Self-signed-friendly context for HTTPS scans. The cert is issued for
# `<dashed-ip>.my.local-ip.co`, not the bare IP we're scanning, so name
# verification would always fail. Discovery only needs to fetch a public
# JSON endpoint — no secrets in transit — so disabling verification is safe.
_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class ClusterCandidate:
    """One Bedrock cluster found via discovery. ``ip`` is the LAN IP
    of whichever node answered (it does NOT matter which member of
    the cluster replies; the joiner just needs ONE reachable HTTPS
    endpoint to start the handshake). ``cluster_uuid`` / ``cluster_name``
    are from the responder's TXT record; empty if responder is older
    than mDNS-with-TXT."""
    ip: str
    cluster_uuid: str = ""
    cluster_name: str = ""
    node_name: str = ""

    def label(self) -> str:
        parts = [f"at {self.ip}"]
        if self.cluster_name:
            parts.append(f"name={self.cluster_name!r}")
        if self.cluster_uuid:
            parts.append(f"uuid={self.cluster_uuid[:8]}")
        if self.node_name:
            parts.append(f"node={self.node_name}")
        return "  ".join(parts)


# ─── stdlib helpers ────────────────────────────────────────────────


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


# ─── mDNS query (multicast, collect all responses for ~2 s) ────────


def _build_mdns_query(qtype: int = 255) -> bytes:
    """Build a multicast DNS query for ``bedrock.local`` of the given
    type (default 255 = ANY). The QU bit is left unset; standard
    multicast responses are fine."""
    # Header: ID=0 (mDNS), flags=0 (standard query, no flags),
    # 1 question, 0 answers, 0 ns, 0 ar.
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    qname = b"\x07bedrock\x05local\x00"
    qrest = struct.pack("!HH", qtype, 1)  # type, class IN
    return header + qname + qrest


def _parse_txt_rdata(rdata: bytes) -> dict:
    """TXT rdata is a series of length-prefixed strings. Each looks
    like ``key=value``. RFC 6763 §6."""
    out: dict = {}
    pos = 0
    while pos < len(rdata):
        ln = rdata[pos]
        pos += 1
        if ln == 0 or pos + ln > len(rdata):
            break
        s = rdata[pos:pos + ln].decode("utf-8", "replace")
        pos += ln
        if "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
    return out


def _read_name(buf: bytes, pos: int) -> tuple[Optional[bytes], int]:
    """Read a DNS name with one level of compression support
    (responses commonly compress the name in the answer RR to point
    at the question's qname)."""
    labels: list[bytes] = []
    jumped = False
    end_pos = pos
    while True:
        if pos >= len(buf):
            return None, pos
        ln = buf[pos]
        if ln == 0:
            pos += 1
            if not jumped:
                end_pos = pos
            return b".".join(labels).lower(), end_pos
        if ln & 0xC0:
            # Compression pointer
            if pos + 2 > len(buf):
                return None, pos
            ptr = struct.unpack_from("!H", buf, pos)[0] & 0x3FFF
            if not jumped:
                end_pos = pos + 2
            pos = ptr
            jumped = True
            continue
        if pos + 1 + ln > len(buf):
            return None, pos
        labels.append(buf[pos + 1:pos + 1 + ln])
        pos += 1 + ln


def _parse_mdns_response(buf: bytes) -> dict:
    """Extract A + TXT records from an mDNS response for our name.
    Returns ``{ips: list[str], txt: dict}``. Responders may include
    multiple A records (one per local IPv4) so we collect them all.
    Returns empty dict if the response doesn't match our name."""
    if len(buf) < 12:
        return {}
    # Read header
    _id, flags, qd, an, _ns, _ar = struct.unpack_from("!HHHHHH", buf, 0)
    if (flags & 0x8000) == 0:
        return {}   # not a response
    pos = 12
    # Skip questions
    for _ in range(qd):
        name, pos = _read_name(buf, pos)
        if name is None:
            return {}
        pos += 4   # qtype + qclass
    # Walk answer RRs
    out: dict = {"ips": [], "txt": {}}
    for _ in range(an):
        name, pos = _read_name(buf, pos)
        if name is None or pos + 10 > len(buf):
            return out
        rtype, rclass, _ttl, rdlength = struct.unpack_from("!HHIH", buf, pos)
        pos += 10
        rdata = buf[pos:pos + rdlength]
        pos += rdlength
        if name != MDNS_NAME:
            continue
        if rtype == 1 and rdlength == 4:          # A
            ip = socket.inet_ntoa(rdata)
            if ip not in out["ips"]:
                out["ips"].append(ip)
        elif rtype == 16:                          # TXT
            out["txt"].update(_parse_txt_rdata(rdata))
    return out


def discover_clusters(timeout: float = 2.0) -> list[ClusterCandidate]:
    """Send one multicast mDNS query for ``bedrock.local`` ANY,
    collect every response received within ``timeout`` seconds,
    return a deduplicated list (one entry per (ip, cluster_uuid)).

    Each response may carry MULTIPLE A records — one per local
    IPv4 the responder advertises. We emit ONE ClusterCandidate
    per ip-address, so a node reachable on both br0 and a USB4
    link-local appears as two candidates with the same cluster_uuid;
    the joiner picks whichever is reachable.

    Only clusters whose responder has populated cluster_uuid are
    "initialised"; bedrock-mdns running before init/join returns
    an A record with empty TXT — those still appear so the operator
    at least sees the node is up."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    # Bind ephemeral so we get unicast responses back.
    s.bind(("", 0))
    try:
        query = _build_mdns_query(qtype=255)
        s.sendto(query, (MDNS_GROUP, MDNS_PORT))
        s.settimeout(0.3)
        deadline = time.monotonic() + timeout
        seen: dict[tuple[str, str], ClusterCandidate] = {}
        while time.monotonic() < deadline:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            parsed = _parse_mdns_response(data)
            ips = parsed.get("ips") or [addr[0]]
            txt = parsed.get("txt") or {}
            for ip in ips:
                key = (ip, txt.get("cluster_uuid", ""))
                if key not in seen:
                    seen[key] = ClusterCandidate(
                        ip=ip,
                        cluster_uuid=txt.get("cluster_uuid", ""),
                        cluster_name=txt.get("cluster_name", ""),
                        node_name=txt.get("node_name", ""),
                    )
        # Order: non-link-local first (LAN paths are more stable),
        # then by cluster name, then by IP. Joiner walks the list
        # and TCP-tests each :8443 until one answers.
        return sorted(
            seen.values(),
            key=lambda c: (
                c.ip.startswith("169.254."),   # False (0) sorts first
                c.cluster_name,
                c.ip,
            ),
        )
    finally:
        try:
            s.close()
        except OSError:
            pass


def first_reachable(candidates: list[ClusterCandidate],
                    port: int = MGMT_PORT_HTTPS,
                    timeout: float = 0.5
                    ) -> Optional[ClusterCandidate]:
    """Walk ``candidates`` in order, TCP-connect-test each on
    ``port``, return the first that answers. Used by ``bedrock
    join --yes`` to auto-pick when multiple A records came back
    for the same cluster (LAN + link-local both advertised; LAN
    works → take it; LAN unreachable on a USB4-only link → fall
    through to the 169.254.x.y candidate)."""
    for c in candidates:
        if _can_reach(c.ip, port, timeout=timeout):
            return c
    return None


# ─── Legacy fallback: subnet scan + hardcoded witness IPs ───────────


def _get_local_subnet_hosts() -> List[str]:
    """Return candidate host IPs in our /24 subnet."""
    r = subprocess.run("ip -o -br addr show br0", shell=True,
                       capture_output=True, text=True)
    for line in r.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 3 and "." in parts[2]:
            ip = parts[2].split("/")[0]
            prefix = ".".join(ip.split(".")[:3]) + "."
            return [prefix + str(i) for i in range(1, 255)]
    return []


def find_witness() -> Optional[str]:
    """Single-IP fallback for old callers. Returns the first
    discovered cluster's IP, or scans subnet/hardcoded IPs if mDNS
    finds nothing. New callers should use ``discover_clusters()``
    for the full list + identity info."""
    candidates = discover_clusters()
    if candidates:
        return candidates[0].ip

    for host in COMMON_WITNESS_IPS:
        if _can_reach(host, WITNESS_PORT):
            try:
                r = urllib.request.urlopen(
                    f"http://{host}:{WITNESS_PORT}/health", timeout=2)
                if r.status == 200:
                    return host
            except Exception:
                pass

    for host in _get_local_subnet_hosts()[:50]:
        for port, scheme in ((MGMT_PORT_HTTPS, "https"),
                             (MGMT_PORT_HTTP, "http")):
            if not _can_reach(host, port, timeout=0.3):
                continue
            try:
                r = _open(f"{scheme}://{host}:{port}/cluster-info",
                          timeout=1)
                if r.status == 200:
                    return host
            except Exception:
                pass
    return None


def query_cluster(host: str) -> Optional[dict]:
    """Fetch /cluster-info from the master. Tries HTTPS 8443, then
    HTTP 8080, then a legacy witness path as last resort."""
    for port, scheme in ((MGMT_PORT_HTTPS, "https"),
                         (MGMT_PORT_HTTP, "http")):
        try:
            r = _open(f"{scheme}://{host}:{port}/cluster-info", timeout=3)
            return json.loads(r.read())
        except Exception:
            pass
    try:
        r = _open(f"http://{host}:{WITNESS_PORT}/cluster-info", timeout=3)
        return json.loads(r.read())
    except Exception:
        pass
    try:
        r = urllib.request.urlopen(
            f"http://{host}:{WITNESS_PORT}/status", timeout=3)
        status = json.loads(r.read())
        nodes = list(status.get("nodes", {}).keys())
        return {"cluster_name": "bedrock", "cluster_uuid": "unknown",
                "nodes": nodes, "witness_host": host}
    except Exception:
        return None


def register(witness: str, my_name: str, my_ip: str) -> bool:
    """Legacy registration call. Kept for old callers; the modern
    join flow uses the saga's /api/join/request handshake."""
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
        return True
