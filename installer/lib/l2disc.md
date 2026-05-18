# `l2disc.py`

**Module purpose.** Passive listener for CDP / LLDP / MikroTik MNDP
on every NIC. Populates `Daemon.switch_neighbors` (used by netd's
status line) so the operator can see "node1's enp2s0 is on Switch
SW-RACK1 port Gi1/0/3" without manually walking the lab.

Read-only — no injection, no per-NIC reply. Just listens.

## Functions

- `open_mndp_socket() -> socket.socket` — UDP/5678 broadcast
  listener for MikroTik MNDP. Returns the socket.
- `drain_mndp(d, now)` — non-blocking recv loop, parses MNDP
  TLVs (system name, MAC, port id, software version) and
  upserts into `d.switch_neighbors[(nic, "mndp")]`.
- `parse_lldp(packet, nic) -> dict | None` — TLV parser for
  IEEE 802.1AB LLDPDUs.
- `parse_cdp(packet, nic) -> dict | None` — TLV parser for
  Cisco Discovery Protocol (raw 802.3 with proprietary OUI).

Caller in `netd.py` opens the sockets at run_daemon startup,
drains every tick.
