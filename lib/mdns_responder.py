"""Tiny multicast-DNS responder for `bedrock.local`.

Why not Avahi: by default Avahi treats A-records as *unique* and runs
conflict-resolution probes that rename the loser to `bedrock-2.local`.
We want every cluster node to answer `bedrock.local` so any node
going down still leaves the alias reachable — the mDNS spec calls
this a *shared* record and explicitly allows multiple hosts to hold
the same name. The cache-flush bit in the answer's CLASS field
distinguishes unique vs shared; we always clear it.

Behaviour:
  * Listen on UDP/5353, joined to the link-local mDNS group
    224.0.0.251.
  * On any incoming query whose first question is for
    `bedrock.local` type A or TXT, build an answer with our current
    LAN IP and/or cluster identity TXT and send it back to the
    querier (unicast — works on every modern client and avoids
    extra multicast traffic).
  * TXT record carries ``cluster_uuid=…;cluster_name=…;
    node_name=…`` so a joiner can tell different Bedrock clusters
    apart on the same LAN.
  * No probing, no announcement, no goodbye. Pure responder.

Pure stdlib — no Avahi dependency, no DBus surface to keep in sync.
"""

from __future__ import annotations

import json
import socket
import struct
import sys
import time
from pathlib import Path
from typing import Optional


MDNS_GROUP = "224.0.0.251"
MDNS_PORT  = 5353

NAME       = b"bedrock"        # advertised label; ".local" appended below
TLD        = b"local"
TTL        = 120                # seconds; clients re-query before expiry
TYPE_A     = 1
TYPE_TXT   = 16
TYPE_ANY   = 255
CLASS_IN   = 1                  # cache-flush bit (0x8000) deliberately UNSET

STATE_FILE = Path("/etc/bedrock/state.json")


def _interface_ipv4_map() -> dict[int, str]:
    """Map ``{ifindex: ipv4_addr}`` for every UP interface that has
    a usable address. Excludes 127.0.0.0/8 and cluster identity /32s
    (RFC 6598 100.X.Y.N — meaningful only inside the cluster, useless
    to a fresh joiner). Refreshed periodically by ``run()`` to track
    DHCP renewals + USB4 hot-plug."""
    import subprocess
    import re
    out: dict[int, str] = {}
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return out
    for line in (r.stdout or "").splitlines():
        # Format: "<idx>: <iface> inet <ip>/<plen> ..."
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0].rstrip(":"))
        except ValueError:
            continue
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not m:
            continue
        ip, plen = m.group(1), int(m.group(2))
        if ip.startswith("127."):
            continue
        if ip.startswith("100.") and plen == 32:
            continue
        # Prefer the first non-link-local on each interface, but if
        # all we have is link-local (USB4 direct cable) keep that.
        existing = out.get(idx)
        if existing is None or (
                existing.startswith("169.254.") and
                not ip.startswith("169.254.")):
            out[idx] = ip
    return out


def own_lan_ip() -> str:
    """Single-IP helper (used by callers outside the responder, e.g.
    log lines). Returns the address most likely to reach the wider
    network, preferring non-link-local."""
    ips = list(_interface_ipv4_map().values())
    if not ips:
        return "0.0.0.0"
    non_ll = [ip for ip in ips if not ip.startswith("169.254.")]
    return non_ll[0] if non_ll else ips[0]


def cluster_identity() -> dict:
    """Read /etc/bedrock/state.json and return the cluster identity
    fields. Returns empty strings if state.json is missing or
    incomplete (pre-init/pre-join node). Joiners shouldn't see this
    node's mDNS as a join target if cluster_uuid is empty — they
    only want existing-cluster nodes."""
    try:
        s = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        s = {}
    return {
        "cluster_uuid": s.get("cluster_uuid", "") or "",
        "cluster_name": s.get("cluster_name", "") or "",
        "node_name":    s.get("node_name", "") or "",
    }


def open_mdns_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", MDNS_PORT))
    mreq = struct.pack("4sL",
                        socket.inet_aton(MDNS_GROUP),
                        socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    # Loop disabled — we don't need to hear our own multicasts.
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Tell the kernel to attach IP_PKTINFO ancillary data to every
    # recvmsg() so we know which interface a query arrived on. That
    # lets us reply with OUR ip on the same interface — the joiner
    # gets an address it can actually reach.
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)
    except (AttributeError, OSError):
        pass
    return s


def _parse_pktinfo(ancdata) -> Optional[int]:
    """Extract ``ipi_ifindex`` from a ``recvmsg`` ancillary tuple.
    Returns None if IP_PKTINFO wasn't present (caller falls back to
    default IP)."""
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level != socket.IPPROTO_IP:
            continue
        if cmsg_type != socket.IP_PKTINFO:
            continue
        # struct in_pktinfo { unsigned int ipi_ifindex;
        #                     struct in_addr ipi_spec_dst;
        #                     struct in_addr ipi_addr; }
        if len(cmsg_data) >= 4:
            return struct.unpack_from("I", cmsg_data)[0]
    return None


def read_qname(buf: bytes, pos: int) -> tuple[Optional[bytes], int]:
    """Read a DNS QNAME starting at pos. Returns (name, next_pos) or
    (None, pos) if the name is invalid / uses compression we don't
    handle (compression doesn't appear in question section of mDNS
    queries — refusing to follow it is fine)."""
    labels: list[bytes] = []
    while pos < len(buf):
        ln = buf[pos]
        if ln == 0:
            return b".".join(labels).lower(), pos + 1
        if ln & 0xC0:               # compression pointer
            return None, pos
        if pos + 1 + ln > len(buf):
            return None, pos
        labels.append(buf[pos + 1:pos + 1 + ln])
        pos += 1 + ln
    return None, pos


def parse_question(query: bytes) -> tuple[Optional[int], Optional[int]]:
    """Return ``(qtype, qclass_without_QU_bit)`` of the first question
    that's for ``bedrock.local``; or ``(None, None)`` if the query
    doesn't ask about us."""
    if len(query) < 12:
        return None, None
    qdcount = struct.unpack_from("!H", query, 4)[0]
    if qdcount < 1:
        return None, None
    name, end = read_qname(query, 12)
    if name != NAME + b"." + TLD:
        return None, None
    if end + 4 > len(query):
        return None, None
    qtype, qclass = struct.unpack_from("!HH", query, end)
    # Strip the QU bit (0x8000) which some clients set to request
    # unicast response — we always reply unicast.
    return qtype, (qclass & 0x7FFF)


def _qname_wire() -> bytes:
    """``bedrock.local`` encoded as length-prefixed labels + zero terminator."""
    return bytes([len(NAME)]) + NAME + bytes([len(TLD)]) + TLD + b"\x00"


def _txt_rdata(identity: dict) -> bytes:
    """Encode TXT rdata as a series of length-prefixed strings.

    Each key=value pair becomes one string. RFC 1035 §3.3.14 +
    RFC 6763 §6 (DNS-SD). Maximum 255 bytes per string; we pack
    short fields so that's never a constraint."""
    pairs: list[bytes] = []
    for k in ("cluster_uuid", "cluster_name", "node_name"):
        v = identity.get(k, "")
        if not v:
            continue
        chunk = f"{k}={v}".encode("utf-8", "replace")
        if len(chunk) > 255:
            chunk = chunk[:255]
        pairs.append(bytes([len(chunk)]) + chunk)
    if not pairs:
        # RFC 6763 §6.1: an empty TXT record has a single zero-length
        # string. Distinguishes "no data" from "name doesn't exist".
        pairs = [b"\x00"]
    return b"".join(pairs)


def build_response(qtype: int, ip_bytes: bytes,
                   identity: dict) -> bytes:
    """Build an mDNS response for ``bedrock.local``: one A record
    (our IP on the interface that received the query) + optionally
    a TXT record. ``qtype`` is the requested type (A, TXT, or ANY).

    ``ip_bytes`` may be ``b""`` — useful for TXT-only responses, or
    when we don't know which interface to reply on (caller picks)."""
    flags = 0x8400                  # QR=1, AA=1
    answers = []
    if qtype in (TYPE_A, TYPE_ANY) and ip_bytes:
        answers.append(
            _qname_wire()
            + struct.pack("!HHIH", TYPE_A, CLASS_IN, TTL, 4)
            + ip_bytes
        )
    if qtype in (TYPE_TXT, TYPE_ANY):
        rdata = _txt_rdata(identity)
        answers.append(
            _qname_wire()
            + struct.pack("!HHIH", TYPE_TXT, CLASS_IN, TTL, len(rdata))
            + rdata
        )
    if not answers:
        return b""
    # Transaction ID 0 per RFC 6762 §18.1.
    header = struct.pack("!HHHHHH", 0, flags, 0, len(answers), 0, 0)
    return header + b"".join(answers)


def run() -> int:
    sock = open_mdns_socket()
    iface_map = _interface_ipv4_map()
    identity = cluster_identity()
    last_refresh = time.monotonic()
    print(f"bedrock-mdns: ready — interfaces={iface_map!r} "
          f"cluster={identity.get('cluster_name','-')!r} "
          f"uuid={identity.get('cluster_uuid','-')[:8]!r}",
          file=sys.stderr, flush=True)

    while True:
        try:
            data, ancdata, _flags, addr = sock.recvmsg(2048, 256)
        except OSError:
            continue
        qtype, qclass = parse_question(data)
        if qtype is None or qclass != CLASS_IN:
            continue
        if qtype not in (TYPE_A, TYPE_TXT, TYPE_ANY):
            continue

        # Periodically re-detect IPs + re-read cluster identity so
        # DHCP renewals, USB4 hot-plug, and post-init identity
        # changes are picked up without restarting the service.
        now = time.monotonic()
        if now - last_refresh > 60:
            new_map = _interface_ipv4_map()
            if new_map != iface_map:
                iface_map = new_map
                print(f"bedrock-mdns: interfaces changed → "
                      f"{iface_map!r}", file=sys.stderr, flush=True)
            new_identity = cluster_identity()
            if new_identity != identity:
                identity = new_identity
                print(f"bedrock-mdns: identity changed → "
                      f"cluster={identity.get('cluster_name','-')!r}",
                      file=sys.stderr, flush=True)
            last_refresh = now

        # Look up our IP on the interface that received the query.
        # If IP_PKTINFO didn't land, or the interface has no IPv4 we
        # know about, fall back to the kernel's preferred address.
        ifidx = _parse_pktinfo(ancdata)
        my_ip = iface_map.get(ifidx) if ifidx is not None else None
        if not my_ip:
            my_ip = own_lan_ip()
        ip_bytes = socket.inet_aton(my_ip) if my_ip != "0.0.0.0" else b""

        resp = build_response(qtype, ip_bytes, identity)
        if not resp:
            continue
        try:
            sock.sendto(resp, addr)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(run())
