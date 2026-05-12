"""L2 neighbour discovery — what switch / router is each NIC attached to.

Receive-only listeners for three protocols that managed switches and
routers commonly advertise themselves on:

  * LLDP  (IEEE 802.1AB)  — vendor-neutral standard.   EtherType 0x88CC.
                            Multicast dest 01:80:c2:00:00:0e
                            (the "nearest bridge" group; some senders
                            also use 00:03 or 00:00).
  * CDP   (Cisco)          — also supported by Aruba, HP, Ubiquiti, …
                            802.3 SNAP encapsulation with OUI 00-00-0c
                            and protocol-id 0x2000. Multicast dest
                            01:00:0c:cc:cc:cc.
  * MNDP  (MikroTik)       — plain UDP broadcast on port 5678.

All three are TLV-encoded. The parser shapes the result into a
common dict so the calling daemon doesn't care which protocol fed it:

    {
        "protocol":    "lldp" | "cdp" | "mndp",
        "chassis_id":  "00:1f:2e:aa:bb:cc"  | "office-sw-01",
        "system_name": "office-sw-01",
        "system_descr": "MikroTik RouterOS 7.13 (stable)",
        "port_id":     "Gi1/0/3"            | "ether3",
        "port_descr":  "uplink-to-bedrock-A",
        "mgmt_ip":     "192.168.1.2",
        "platform":    "CRS354-48G-4S+2Q+",
        "ttl_s":       120,
    }

We never send our own announcements back — pure observation. Switch
state changes (operator swaps a port, replaces a switch) get noticed
within one TTL cycle (≤ 120 s typical).
"""

from __future__ import annotations

import socket
import struct
from typing import Optional


# ── Constants ────────────────────────────────────────────────────────

LLDP_ETHERTYPE      = 0x88CC
LLDP_MULTICAST_MAC  = b"\x01\x80\xc2\x00\x00\x0e"
CDP_MULTICAST_MAC   = b"\x01\x00\x0c\xcc\xcc\xcc"
MNDP_UDP_PORT       = 5678

# Linux AF_PACKET hooks not exposed in stdlib socket module.
ETH_P_802_2           = 0x0004   # any LLC-framed 802.3 packet
SOL_PACKET            = 263
PACKET_ADD_MEMBERSHIP = 1
PACKET_MR_MULTICAST   = 0

# LLDP TLV types (7-bit).
_LLDP_END           = 0
_LLDP_CHASSIS_ID    = 1
_LLDP_PORT_ID       = 2
_LLDP_TTL           = 3
_LLDP_PORT_DESCR    = 4
_LLDP_SYS_NAME      = 5
_LLDP_SYS_DESCR     = 6
_LLDP_MGMT_ADDR     = 8

# CDP TLV types (16-bit).
_CDP_DEVICE_ID      = 0x0001
_CDP_ADDRESSES      = 0x0002
_CDP_PORT_ID        = 0x0003
_CDP_SW_VERSION     = 0x0005
_CDP_PLATFORM       = 0x0006

# MNDP TLV types (16-bit).
_MNDP_MAC           = 0x0001
_MNDP_IDENTITY      = 0x0005
_MNDP_VERSION       = 0x0007
_MNDP_PLATFORM      = 0x0008
_MNDP_BOARD         = 0x000C
_MNDP_IFACE_NAME    = 0x0010


# ── Helpers ──────────────────────────────────────────────────────────

def _mac(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b[:6])


def _utf8(b: bytes) -> str:
    return b.decode("utf-8", errors="replace").rstrip("\x00").strip()


_HEX = set("0123456789abcdefABCDEF")
def _is_mac(s: str) -> bool:
    """True if `s` is a colon-separated 6-byte MAC string."""
    if not isinstance(s, str):
        return False
    parts = s.split(":")
    if len(parts) != 6:
        return False
    return all(len(p) == 2 and all(c in _HEX for c in p) for p in parts)


def _pick_device_key(chassis_id: str, src_mac: str) -> str:
    """Best identifier for cross-protocol merging.

    A switch is one physical device; ideally we want one rollup entry
    per device regardless of which protocol it advertises with. The
    only identifier guaranteed to exist on a real switch is its MAC
    address. Some protocols put a MAC directly in chassis_id (LLDP
    subtype 4, MNDP MAC TLV); others put a string device name (CDP).
    Where we can read the L2 source MAC from the frame we use that
    as the fallback — every real switch transmits frames from a real
    MAC even when its advertised chassis_id is a string.

    Returns the lowercase MAC form so case mismatches don't fragment
    the rollup."""
    if _is_mac(chassis_id):
        return chassis_id.lower()
    if _is_mac(src_mac):
        return src_mac.lower()
    # Last resort: use whatever string we got. Two different protocols
    # advertising different string chassis_ids will not merge here, but
    # at least each protocol's view stays consistent across nodes.
    return chassis_id or src_mac or ""


def _fmt_lldp_chassis(subtype: int, data: bytes) -> str:
    # subtype 4 = MAC address (most common). 5 = network address.
    # 1, 2, 3, 6, 7 = string-ish identifiers.
    if subtype == 4 and len(data) >= 6:
        return _mac(data)
    if subtype == 5 and len(data) >= 5 and data[0] == 1:
        return socket.inet_ntoa(data[1:5])
    return _utf8(data)


def _fmt_lldp_port(subtype: int, data: bytes) -> str:
    # subtype 3 = MAC; 4 = network address; rest are string-ish.
    if subtype == 3 and len(data) >= 6:
        return _mac(data)
    if subtype == 4 and len(data) >= 5 and data[0] == 1:
        return socket.inet_ntoa(data[1:5])
    return _utf8(data)


def _lldp_mgmt_addr(data: bytes) -> Optional[str]:
    # struct: addr_str_len(1) + subtype(1) + address(N) + …
    if len(data) < 2:
        return None
    addr_str_len = data[0]
    if addr_str_len < 2 or 1 + addr_str_len > len(data):
        return None
    subtype = data[1]
    address = data[2:1 + addr_str_len]
    if subtype == 1 and len(address) == 4:
        return socket.inet_ntoa(address)
    if subtype == 2 and len(address) == 16:
        return socket.inet_ntop(socket.AF_INET6, address)
    return None


# ── LLDP ─────────────────────────────────────────────────────────────

def decode_lldp(frame: bytes) -> Optional[dict]:
    """Decode an LLDP Ethernet frame (with ethernet header). Returns
    the normalised neighbour dict, or None on parse failure / wrong
    frame type."""
    if len(frame) < 14:
        return None
    dst = frame[:6]
    # All LLDP multicast destinations share the 01:80:c2:00:00 prefix
    # (LSB varies: 0e nearest-bridge, 03 nearest-non-tpmr, 00 customer).
    if dst[:5] != b"\x01\x80\xc2\x00\x00":
        return None
    ethertype = struct.unpack_from("!H", frame, 12)[0]
    if ethertype != LLDP_ETHERTYPE:
        return None

    src_mac = _mac(frame[6:12])
    out: dict = {"protocol": "lldp", "src_mac": src_mac}
    offset = 14
    while offset + 2 <= len(frame):
        header = struct.unpack_from("!H", frame, offset)[0]
        ttype = (header >> 9) & 0x7F
        tlen  = header & 0x1FF
        offset += 2
        if ttype == _LLDP_END:
            break
        if offset + tlen > len(frame):
            return None
        value = frame[offset:offset + tlen]
        offset += tlen
        if ttype == _LLDP_CHASSIS_ID and tlen >= 1:
            out["chassis_id"] = _fmt_lldp_chassis(value[0], value[1:])
        elif ttype == _LLDP_PORT_ID and tlen >= 1:
            out["port_id"] = _fmt_lldp_port(value[0], value[1:])
        elif ttype == _LLDP_TTL and tlen == 2:
            out["ttl_s"] = struct.unpack("!H", value)[0]
        elif ttype == _LLDP_PORT_DESCR:
            out["port_descr"] = _utf8(value)
        elif ttype == _LLDP_SYS_NAME:
            out["system_name"] = _utf8(value)
        elif ttype == _LLDP_SYS_DESCR:
            out["system_descr"] = _utf8(value)
        elif ttype == _LLDP_MGMT_ADDR:
            mip = _lldp_mgmt_addr(value)
            if mip and "mgmt_ip" not in out:
                out["mgmt_ip"] = mip

    if "chassis_id" not in out:
        return None
    out["device_key"] = _pick_device_key(out["chassis_id"], src_mac)
    return out


def open_lldp_socket(nic: str) -> socket.socket:
    """AF_PACKET listener bound to LLDP ethertype on this NIC.
    Joins the nearest-bridge multicast group so we see frames even
    when the switch directs them L2-targeted."""
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                       socket.htons(LLDP_ETHERTYPE))
    s.bind((nic, 0))
    s.setblocking(False)
    ifindex = socket.if_nametoindex(nic)
    mreq = struct.pack("iHH8s", ifindex, PACKET_MR_MULTICAST, 6,
                        LLDP_MULTICAST_MAC + b"\x00\x00")
    s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
    return s


# ── CDP ──────────────────────────────────────────────────────────────

def decode_cdp(frame: bytes) -> Optional[dict]:
    """Decode an 802.3 SNAP-encapsulated CDP frame."""
    if len(frame) < 14 + 8 + 4:
        return None
    if frame[:6] != CDP_MULTICAST_MAC:
        return None
    # 802.3 framing: dst(6) + src(6) + length(2) + LLC(3) + SNAP(5)
    #   LLC:  DSAP 0xAA  SSAP 0xAA  Ctrl 0x03
    #   SNAP: OUI 00-00-0c  ProtoID 0x2000
    if frame[14:17] != b"\xaa\xaa\x03":
        return None
    if frame[17:20] != b"\x00\x00\x0c":
        return None
    if frame[20:22] != b"\x20\x00":
        return None
    src_mac = _mac(frame[6:12])
    cdp_offset = 22
    if cdp_offset + 4 > len(frame):
        return None
    # version(1) + ttl(1) + checksum(2)
    ttl = frame[cdp_offset + 1]
    cdp_offset += 4

    out: dict = {"protocol": "cdp", "src_mac": src_mac, "ttl_s": int(ttl)}
    while cdp_offset + 4 <= len(frame):
        ttype, tlen = struct.unpack_from("!HH", frame, cdp_offset)
        if tlen < 4 or cdp_offset + tlen > len(frame):
            break
        value = frame[cdp_offset + 4:cdp_offset + tlen]
        cdp_offset += tlen
        if ttype == _CDP_DEVICE_ID:
            name = _utf8(value)
            out["chassis_id"] = name
            out.setdefault("system_name", name)
        elif ttype == _CDP_PORT_ID:
            out["port_id"] = _utf8(value)
        elif ttype == _CDP_SW_VERSION:
            out["system_descr"] = _utf8(value)
        elif ttype == _CDP_PLATFORM:
            out["platform"] = _utf8(value)
        elif ttype == _CDP_ADDRESSES and len(value) >= 4:
            count = struct.unpack_from("!I", value, 0)[0]
            ao = 4
            for _ in range(count):
                if ao + 5 > len(value):
                    break
                _proto_type = value[ao]
                proto_len = value[ao + 1]
                ao += 2 + proto_len
                if ao + 2 > len(value):
                    break
                addr_len = struct.unpack_from("!H", value, ao)[0]
                ao += 2
                if ao + addr_len > len(value):
                    break
                addr = value[ao:ao + addr_len]
                ao += addr_len
                if addr_len == 4 and "mgmt_ip" not in out:
                    out["mgmt_ip"] = socket.inet_ntoa(addr)

    if "chassis_id" not in out:
        return None
    # CDP's chassis_id is the device name (text). The frame's L2 source
    # MAC is the closest thing to a chassis identifier we get on the wire.
    out["device_key"] = _pick_device_key(out["chassis_id"], src_mac)
    return out


def open_cdp_socket(nic: str) -> socket.socket:
    """AF_PACKET listener for CDP. Binds to ETH_P_802_2 — the kernel
    delivers only 802.3-framed (LLC/SNAP) packets, which is what CDP
    uses, sparing us the firehose of ETH_P_ALL on every NIC."""
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                       socket.htons(ETH_P_802_2))
    s.bind((nic, 0))
    s.setblocking(False)
    ifindex = socket.if_nametoindex(nic)
    mreq = struct.pack("iHH8s", ifindex, PACKET_MR_MULTICAST, 6,
                        CDP_MULTICAST_MAC + b"\x00\x00")
    s.setsockopt(SOL_PACKET, PACKET_ADD_MEMBERSHIP, mreq)
    return s


# ── MNDP ─────────────────────────────────────────────────────────────

def decode_mndp(payload: bytes) -> Optional[dict]:
    """Decode an MNDP UDP payload (sent by MikroTik routers/switches
    every ~30 s to UDP port 5678, broadcast)."""
    if len(payload) < 4:
        return None
    # 4-byte MNDP header: type(2) + sequence(2). Both are largely
    # informational for our purposes.
    offset = 4
    out: dict = {"protocol": "mndp"}
    while offset + 4 <= len(payload):
        ttype, tlen = struct.unpack_from("!HH", payload, offset)
        offset += 4
        if offset + tlen > len(payload):
            break
        value = payload[offset:offset + tlen]
        offset += tlen
        if ttype == _MNDP_MAC and len(value) == 6:
            out["chassis_id"] = _mac(value)
            out["src_mac"]    = _mac(value)   # UDP, no L2 access — best guess
        elif ttype == _MNDP_IDENTITY:
            out["system_name"] = _utf8(value)
        elif ttype == _MNDP_VERSION:
            out["system_descr"] = _utf8(value)
        elif ttype == _MNDP_PLATFORM:
            out["platform"] = _utf8(value)
        elif ttype == _MNDP_BOARD:
            out.setdefault("platform", _utf8(value))
        elif ttype == _MNDP_IFACE_NAME:
            out["port_id"] = _utf8(value)

    if "chassis_id" not in out:
        return None
    out["device_key"] = _pick_device_key(out["chassis_id"],
                                          out.get("src_mac", ""))
    return out


def open_mndp_socket() -> socket.socket:
    """UDP socket bound to MNDP broadcast port. Single per-host
    socket; IP_PKTINFO so the daemon can attribute each datagram to
    the NIC it arrived on (broadcast from a MikroTik on the LAN side
    shouldn't be tagged as 'seen on enp3s0')."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    IP_PKTINFO = 8
    s.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
    s.bind(("", MNDP_UDP_PORT))
    s.setblocking(False)
    return s


# ── Self-test (run `python3 -m installer.lib.l2disc`) ─────────────────

if __name__ == "__main__":
    # Synthetic LLDP frame: 7-byte preamble in real life isn't included
    # here; we start at the destination MAC like raw recv() gives us.
    eth = (
        LLDP_MULTICAST_MAC                  # dst
        + b"\x00\x1f\x2e\xaa\xbb\xcc"      # src
        + b"\x88\xcc"                       # ethertype
    )
    def _tlv(ttype: int, value: bytes) -> bytes:
        return struct.pack("!H", (ttype << 9) | len(value)) + value
    payload = b"".join([
        _tlv(_LLDP_CHASSIS_ID, b"\x04" + b"\x00\x1f\x2e\xaa\xbb\xcc"),
        _tlv(_LLDP_PORT_ID,    b"\x05" + b"Gi1/0/3"),
        _tlv(_LLDP_TTL,        struct.pack("!H", 120)),
        _tlv(_LLDP_SYS_NAME,   b"office-sw-01"),
        _tlv(_LLDP_SYS_DESCR,  b"Aruba 2930F"),
        _tlv(_LLDP_PORT_DESCR, b"uplink-to-bedrock-A"),
        _tlv(_LLDP_MGMT_ADDR,
              b"\x05\x01" + socket.inet_aton("192.168.1.2") +
              b"\x02" + b"\x00\x00\x00\x00" + b"\x00"),
        _tlv(_LLDP_END,        b""),
    ])
    decoded = decode_lldp(eth + payload)
    assert decoded == {
        "protocol":    "lldp",
        "src_mac":     "00:1f:2e:aa:bb:cc",
        "device_key":  "00:1f:2e:aa:bb:cc",
        "chassis_id":  "00:1f:2e:aa:bb:cc",
        "port_id":     "Gi1/0/3",
        "ttl_s":       120,
        "system_name": "office-sw-01",
        "system_descr": "Aruba 2930F",
        "port_descr":  "uplink-to-bedrock-A",
        "mgmt_ip":     "192.168.1.2",
    }, f"unexpected LLDP decode: {decoded!r}"

    # Synthetic CDP frame
    cdp_payload = (
        b"\x02\x78\x00\x00"   # version 2 + TTL 120 + checksum
        + struct.pack("!HH", _CDP_DEVICE_ID, 4 + len(b"office-cisco-1"))
        + b"office-cisco-1"
        + struct.pack("!HH", _CDP_PORT_ID,   4 + len(b"GigabitEthernet1/0/3"))
        + b"GigabitEthernet1/0/3"
        + struct.pack("!HH", _CDP_PLATFORM,  4 + len(b"cisco WS-C2960X-24TS-L"))
        + b"cisco WS-C2960X-24TS-L"
    )
    cdp_eth = (
        CDP_MULTICAST_MAC
        + b"\x00\x1f\x2e\xaa\xbb\xcc"
        + struct.pack("!H", len(cdp_payload) + 8)
        + b"\xaa\xaa\x03"
        + b"\x00\x00\x0c\x20\x00"
        + cdp_payload
    )
    decoded = decode_cdp(cdp_eth)
    assert decoded == {
        "protocol":     "cdp",
        "src_mac":      "00:1f:2e:aa:bb:cc",
        "device_key":   "00:1f:2e:aa:bb:cc",
        "ttl_s":        120,
        "chassis_id":   "office-cisco-1",
        "system_name":  "office-cisco-1",
        "port_id":      "GigabitEthernet1/0/3",
        "platform":     "cisco WS-C2960X-24TS-L",
    }, f"unexpected CDP decode: {decoded!r}"

    # Synthetic MNDP payload (no Ethernet header — UDP gives us the body).
    mndp = (
        b"\x00\x00\x00\x00"   # MNDP header (type + sequence)
        + struct.pack("!HH", _MNDP_MAC,        6) + b"\x00\x1f\x2e\xaa\xbb\xcc"
        + struct.pack("!HH", _MNDP_IDENTITY,   8) + b"router-1"
        + struct.pack("!HH", _MNDP_VERSION,    6) + b"7.13.4"
        + struct.pack("!HH", _MNDP_PLATFORM,   8) + b"MikroTik"
        + struct.pack("!HH", _MNDP_BOARD,      9) + b"hEX-S-GHz"
        + struct.pack("!HH", _MNDP_IFACE_NAME, 6) + b"ether2"
    )
    decoded = decode_mndp(mndp)
    assert decoded == {
        "protocol":     "mndp",
        "src_mac":      "00:1f:2e:aa:bb:cc",
        "device_key":   "00:1f:2e:aa:bb:cc",
        "chassis_id":   "00:1f:2e:aa:bb:cc",
        "system_name":  "router-1",
        "system_descr": "7.13.4",
        "platform":     "MikroTik",
        "port_id":      "ether2",
    }, f"unexpected MNDP decode: {decoded!r}"

    # Cross-protocol merge: CDP and MNDP from the same device should
    # produce the same device_key when the CDP frame's L2 source MAC
    # matches the MNDP MAC TLV. This is what makes the dashboard
    # rollup show one entry per physical device.
    assert decode_cdp(cdp_eth)["device_key"] == decode_mndp(mndp)["device_key"], \
        "CDP + MNDP from same MAC should produce identical device_key"

    print("l2disc: self-test OK (LLDP + CDP + MNDP parsers)")
