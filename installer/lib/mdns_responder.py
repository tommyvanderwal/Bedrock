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
    `bedrock.local` type A, build an answer with our current LAN IP
    and send it back to the querier (unicast — works on every modern
    client and avoids extra multicast traffic).
  * No probing, no announcement, no goodbye. Pure responder.

Implementation is ~110 lines of pure stdlib — no Avahi dependency,
no DBus surface to keep in sync.
"""

from __future__ import annotations

import socket
import struct
import sys
import time
from typing import Optional


MDNS_GROUP = "224.0.0.251"
MDNS_PORT  = 5353

NAME       = b"bedrock"        # advertised label; ".local" appended below
TLD        = b"local"
TTL        = 120                # seconds; clients re-query before expiry
TYPE_A     = 1
CLASS_IN   = 1                  # cache-flush bit (0x8000) deliberately UNSET


def own_lan_ip() -> str:
    """Kernel routing-table lookup for the IP we'd use to reach the
    wider network. Captures the LAN-facing IP without picking it from
    a hard-coded interface name."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 1))
        return s.getsockname()[0]
    finally:
        s.close()


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
    return s


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


def is_question_for_us(query: bytes) -> bool:
    """True iff the first question is `bedrock.local` type A class IN."""
    if len(query) < 12:
        return False
    qdcount = struct.unpack_from("!H", query, 4)[0]
    if qdcount < 1:
        return False
    name, end = read_qname(query, 12)
    if name != NAME + b"." + TLD:
        return False
    if end + 4 > len(query):
        return False
    qtype, qclass = struct.unpack_from("!HH", query, end)
    # Strip the QU bit (0x8000) which some clients set to request
    # unicast response — we always reply unicast, so accept either way.
    return qtype == TYPE_A and (qclass & 0x7FFF) == CLASS_IN


def build_response(query: bytes, ip_bytes: bytes) -> bytes:
    """Build an mDNS response for the first question in `query`."""
    # mDNS responses use transaction ID 0 per RFC 6762 §18.1.
    flags = 0x8400                  # QR=1, AA=1
    header = struct.pack("!HHHHHH", 0, flags, 0, 1, 0, 0)
    # Encode `bedrock.local` as length-prefixed labels.
    qname = bytes([len(NAME)]) + NAME + bytes([len(TLD)]) + TLD + b"\x00"
    answer = qname + struct.pack("!HHIH",
                                  TYPE_A, CLASS_IN, TTL, 4) + ip_bytes
    return header + answer


def run() -> int:
    sock = open_mdns_socket()
    ip = own_lan_ip()
    ip_bytes = socket.inet_aton(ip)
    last_refresh = time.monotonic()
    print(f"bedrock-mdns: answering bedrock.local → {ip}",
          file=sys.stderr, flush=True)

    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError:
            continue
        if not is_question_for_us(data):
            continue
        # Periodically re-detect our IP so DHCP renewals are picked up
        # without restarting the service.
        now = time.monotonic()
        if now - last_refresh > 60:
            new_ip = own_lan_ip()
            if new_ip != ip:
                ip = new_ip
                ip_bytes = socket.inet_aton(ip)
                print(f"bedrock-mdns: LAN ip changed → {ip}",
                      file=sys.stderr, flush=True)
            last_refresh = now
        try:
            sock.sendto(build_response(data, ip_bytes), addr)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(run())
