# installer/lib/l2disc.py

L2 neighbour discovery: works out which switch or router each NIC is plugged
into by passively listening for the link-layer advertisements that managed
network gear broadcasts. It supports three protocols — LLDP (802.1AB), CDP
(Cisco/Aruba/HP/Ubiquiti), and MNDP (MikroTik) — and normalises each into one
common neighbour dict so the calling daemon doesn't care which protocol fed it.
This module is pure decode + socket-open; it never transmits, so it cannot leak
its own presence onto a customer switch, and it owns no state, files, or
services. The calling daemon (netd thread of `bedrock-d`) owns the receive loop,
NIC attribution, TTL aging, and the cross-node rollup.

The normalised neighbour dict (keys present depend on what the device sent):

    {
      "protocol":    "lldp" | "cdp" | "mndp",
      "chassis_id":  "00:1f:2e:aa:bb:cc" | "office-sw-01",
      "system_name": "office-sw-01",
      "system_descr": "MikroTik RouterOS 7.13 (stable)",
      "port_id":     "Gi1/0/3" | "ether3",
      "port_descr":  "uplink-to-bedrock-A",
      "mgmt_ip":     "192.168.1.2",
      "platform":    "CRS354-48G-4S+2Q+",
      "ttl_s":       120,
      "src_mac":     "00:1f:2e:aa:bb:cc",
      "device_key":  "00:1f:2e:aa:bb:cc",
    }

## Functions / Classes

### `decode_lldp(frame: bytes) -> Optional[dict]`
Decode a raw LLDP Ethernet frame (Ethernet header included) into the neighbour dict.
- **In:** `frame` — bytes starting at the destination MAC, as `recv()` on an
  `AF_PACKET` socket delivers.
- **Out:** normalised dict or `None` on parse failure, wrong EtherType
  (not `0x88CC`), a destination MAC not matching the `01:80:c2:00:00` prefix, an
  over-long TLV, or a missing `chassis_id`. No side effects. Always sets
  `protocol="lldp"`, `src_mac`, and `device_key`.

### `open_lldp_socket(nic: str) -> socket.socket`
Open a non-blocking `AF_PACKET`/`SOCK_RAW` listener bound to the LLDP EtherType
on one NIC, joined to the nearest-bridge multicast group.
- **In:** `nic` — interface name.
- **Out:** a bound, non-blocking raw socket. Side effects: creates a kernel
  socket, binds it to `nic`, and calls `setsockopt(PACKET_ADD_MEMBERSHIP)` to
  join `01:80:c2:00:00:0e`. Caller owns/closes it.

### `decode_cdp(frame: bytes) -> Optional[dict]`
Decode an 802.3 SNAP-encapsulated CDP frame into the neighbour dict.
- **In:** `frame` — raw frame starting at the destination MAC.
- **Out:** normalised dict or `None` if too short, wrong destination MAC
  (not `01:00:0c:cc:cc:cc`), wrong LLC/SNAP/OUI/protocol-id header
  (`aa aa 03` / OUI `00 00 0c` / proto-id `0x2000`), or missing device-id. No
  side effects. Sets `protocol="cdp"`, `src_mac`, `ttl_s`, `device_key`.

### `open_cdp_socket(nic: str) -> socket.socket`
Open a non-blocking raw listener for CDP, bound to `ETH_P_802_2` so the kernel
delivers only LLC/SNAP-framed packets.
- **In:** `nic` — interface name.
- **Out:** bound non-blocking raw socket. Side effects: socket create + bind +
  `PACKET_ADD_MEMBERSHIP` for `01:00:0c:cc:cc:cc`. Caller owns/closes it.

### `decode_mndp(payload: bytes) -> Optional[dict]`
Decode an MNDP UDP payload (the datagram body, no Ethernet header) into the
neighbour dict.
- **In:** `payload` — UDP body received on port 5678.
- **Out:** normalised dict or `None` if shorter than the 4-byte header or
  missing `chassis_id`. No side effects. Sets `protocol="mndp"` and `device_key`;
  `src_mac` comes from the MAC TLV (no L2 access over UDP).

### `open_mndp_socket() -> socket.socket`
Open the single per-host non-blocking UDP socket bound to the MNDP broadcast
port (5678).
- **In:** none.
- **Out:** bound non-blocking UDP socket. Side effects: sets `SO_REUSEADDR`,
  `SO_BROADCAST`, and `IP_PKTINFO` (so the daemon can attribute each datagram to
  its arrival NIC); tries `SO_REUSEPORT` and ignores `OSError` if unsupported;
  binds `("", 5678)`. Caller owns/closes it.

### Private helpers
- `_mac(b)` / `_utf8(b)` — format the first 6 bytes as a colon MAC; decode bytes
  as UTF-8, stripping NULs and whitespace.
- `_is_mac(s)` — true only for a colon-separated 6-hex-byte string.
- `_pick_device_key(chassis_id, src_mac)` — choose the cross-protocol merge key.
- `_fmt_lldp_chassis` / `_fmt_lldp_port` — render an LLDP chassis/port TLV by
  subtype (MAC, IPv4 network address, or string).
- `_lldp_mgmt_addr(data)` — pull an IPv4 or IPv6 management address out of the
  LLDP management-address TLV.

## How it works

Each `decode_*` is a defensive TLV walker over an untrusted frame off the wire.
Every step re-checks bounds before slicing, and any malformed length aborts the
parse (LLDP returns `None` on an over-long TLV; CDP/MNDP `break` and keep what
they have so far). A decode that produces no `chassis_id` returns `None` —
without a chassis identity there is nothing to roll up.

The three framings differ at the front, then converge on TLVs:

```
LLDP   [ dst:01:80:c2:00:00:xx ][ src ][ 0x88CC ] then TLVs
       (LSB: 0e nearest-bridge / 03 nearest-non-TPMR / 00 customer)
       TLV header = 16 bits: type(7) | length(9)
       walk until END (type 0) or buffer end

CDP    [ dst:01:00:0c:cc:cc:cc ][ src ][ len ]
       [ LLC AA AA 03 ][ SNAP 00 00 0c 20 00 ]
       [ ver(1) ttl(1) cksum(2) ] then TLVs
       TLV header = 16-bit type + 16-bit length (length COVERS the header)

MNDP   (UDP body only) [ type(2) seq(2) ] then TLVs
       TLV header = 16-bit type + 16-bit length (length covers value only)
```

Length semantics differ between protocols and the parser honours each: LLDP's
9-bit length is the value length and the cursor advances header-then-value;
CDP's 16-bit length spans header+value (so `value` is `frame[off+4 : off+tlen]`
and the cursor advances by `tlen`, with a `tlen < 4` guard); MNDP's length is
value-only.

Subtyped fields are resolved in the LLDP helpers: a chassis-ID subtype 4 becomes
a MAC, subtype 5 (with leading `01`) an IPv4 address, everything else a string;
port-ID is the same shape with subtype 3 for MAC. The LLDP management-address
TLV yields IPv4 (subtype 1, 4 bytes) or IPv6 (subtype 2, 16 bytes); only the
first one seen is kept. CDP's address TLV walks a count-prefixed list of
protocol/address records and keeps the first 4-byte (IPv4) address as `mgmt_ip`.
CDP's device-id becomes both `chassis_id` and the default `system_name`.

Cross-protocol merge is the load-bearing part. One physical switch can advertise
on more than one protocol, and the daemon wants a single rollup entry per
device. `_pick_device_key` picks, in order: the `chassis_id` if it is itself a
MAC (LLDP subtype-4, MNDP MAC TLV), else the frame's L2 source MAC (every real
switch transmits from a real MAC even when its advertised chassis_id is a text
name, as with CDP), else whatever string is left. The key is lowercased so case
differences don't fragment the rollup.

```
device_key selection (first match wins):
  chassis_id is a MAC?  -> chassis_id.lower()
  src_mac is a MAC?     -> src_mac.lower()
  otherwise             -> chassis_id or src_mac or ""
```

For socket setup, both raw listeners bind per-NIC and explicitly join the
relevant multicast group via `PACKET_ADD_MEMBERSHIP`, so frames are seen even
when a switch L2-targets them. The CDP listener binds `ETH_P_802_2` rather than
`ETH_P_ALL`, narrowing delivery to LLC/SNAP frames. MNDP uses one shared UDP
socket for the whole host with `IP_PKTINFO`, leaving per-NIC attribution of each
broadcast datagram to the daemon. All sockets are non-blocking, fitting a single
select/poll receive loop.

Running the module directly (`python3 -m installer.lib.l2disc`) executes a
self-test that decodes synthetic LLDP, CDP, and MNDP frames, asserts the exact
output dicts, and asserts that a CDP frame and an MNDP datagram from the same MAC
produce an identical `device_key`.

## Why

The module only listens and never announces, so plugging a Bedrock node into a
customer network adds no L2 advertisement traffic of its own. Switch changes
(re-patched port, swapped switch) surface within one advertisement TTL cycle
(typically ≤ 120 s) as the neighbour dict's `ttl_s` and fields change.
