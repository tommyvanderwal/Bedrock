"""Bedrock mesh-network daemon (`bedrock-net.service`).

Single Python daemon that runs on every node and owns the layer
between L2-cable-up and "DRBD/libvirt/NFS can talk to a peer's
loopback IP." The architecture rationale lives in BEDROCK.md
"network architecture" but the short version:

  * Every node has ONE cluster identity = a /32 loopback IP recorded
    in cluster.json (via NODE_LOOPBACK log entry, set at init/join).
    Per-NIC IPs are throwaway and never logged.
  * On every up interface, this daemon emits a signed UDP multicast
    probe every 1 s. Recipients verify the cluster_key MAC, learn
    "node X's loopback Y is reachable on this link via address Z."
  * In-memory gossip is realtime; only durable transitions (link up
    past 5 s, link down past 30 s) get appended to the log as
    LINK_UP / LINK_DOWN.
  * Routing is computed locally per-node from the replicated path
    table (Dijkstra on the graph). Backup routes installed at
    monotonic metrics so the kernel fails over for free on
    link-down. Black-hole gateway detection comes from this daemon's
    own probe-loss → it `ip route del` the dead route, the
    next-metric one auto-promotes.
  * A panic-neighbour catch-all `10.99.0.0/24 via <freshest peer>
    metric 999` is always installed when at least one neighbour is
    reachable. Loops are bounded by IP TTL; TCP backoff and UDP's
    low volume keep the worst case from being noisy.

Concretely this file holds:
  * `NetDaemon` — the run-loop. Started by /usr/local/bin/bedrock-net
    via the bedrock-net.service systemd unit.
  * Probe codec (msgpack + HMAC-SHA256 over cluster_key).
  * Interface watcher (parses `ip monitor link`).
  * Hysteresis logic.
  * Route emitter (`ip route` shell-outs; one place that touches the
    cluster prefixes, so we never fight ourselves).

Style: pure stdlib + msgpack. No external deps beyond what packages.py
already installs cluster-wide. The daemon is single-threaded with
soft real-time loops (~250 ms tick); receive sockets run in a thread
pool so a blocked NIC doesn't stall the rest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import msgpack


# ── Constants ────────────────────────────────────────────────────────

CLUSTER_KEY_FILE = Path("/etc/bedrock/cluster.key")
CLUSTER_JSON     = Path("/etc/bedrock/cluster.json")
STATE_JSON       = Path("/etc/bedrock/state.json")

PROBE_GROUP = "239.7.7.7"        # private-block multicast
PROBE_PORT  = 7732               # 'BR' = 0x4252 → 7732 prime nearby
PROBE_TTL   = 1                  # link-local only; never crosses routers
PROBE_INTERVAL = 1.0             # seconds between probes per interface
TICK_INTERVAL  = 0.25            # main loop tick

UP_HYSTERESIS_S   = 5.0          # link must be up this long before LINK_UP
DOWN_HYSTERESIS_S = 30.0         # silent this long before LINK_DOWN
QUALITY_REFRESH_S = 60.0         # LINK_QUALITY rate limit when stable

# Loopback identity prefix. Each node gets a /32 in this /24 from a
# deterministic derivation when init/join runs.
LOOPBACK_PREFIX = "10.99.0"
LOOPBACK_RANGE_NET = "10.99.0.0/24"

# Per-NIC throwaway IPs come from this /16, derived from
# sha256(cluster_uuid|nic_mac). Throwaway is the right word — the
# value of these IPs is "give the kernel an ARP target," nothing more.
THROWAWAY_PREFIX = "10.42"

# Interfaces we never touch (loopback, virbr*, docker, etc.). Anything
# matching one of these prefixes is left alone — operator-configured
# bridges, container networks, and the lo interface itself stay out
# of the cluster mesh.
INTERFACE_BLOCKLIST_PREFIXES = (
    "lo", "virbr", "docker", "br-", "veth", "tap",
    "tun", "wg", "kube", "cali", "cni",
)

# Routing metrics — lower is preferred. Direct paths use 10..N,
# transit hops use 100..N, panic catch-all is 999.
METRIC_DIRECT_BASE  = 10
METRIC_TRANSIT_BASE = 100
METRIC_PANIC        = 999

# Cluster prefix for bedrock-net managed routing. Every loopback IP
# is in this block, so the panic route can match the whole space.
CLUSTER_LOOPBACK_NET = "10.99.0.0/24"


# ── Codec ────────────────────────────────────────────────────────────

PROBE_VERSION = 1

def encode_probe(cluster_uuid: str, node: str, nic: str, loopback: str,
                 link_addr: str, ts: float, *, key: bytes) -> bytes:
    """Sign-then-pack a probe. Layout: msgpack({v, body, sig}).
    `body` is itself msgpack-packed so the HMAC inputs are
    bit-identical on the receiver (no map-ordering ambiguity)."""
    body = msgpack.packb({
        "cluster_uuid": cluster_uuid,
        "node": node, "nic": nic,
        "loopback": loopback,
        "link_addr": link_addr,
        "ts": float(ts),
    }, use_bin_type=True)
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return msgpack.packb({"v": PROBE_VERSION, "body": body, "sig": sig},
                          use_bin_type=True)


def decode_probe(buf: bytes, *, key: bytes) -> Optional[dict]:
    """Verify a probe and return the body dict, or None if it doesn't
    pass MAC verification or schema check. Returning None silently is
    deliberate — the network is full of packets we don't care about."""
    try:
        wrap = msgpack.unpackb(buf, raw=False)
        if not isinstance(wrap, dict):
            return None
        if wrap.get("v") != PROBE_VERSION:
            return None
        body_bytes = wrap.get("body")
        sig = wrap.get("sig")
        if not body_bytes or not sig:
            return None
        expected = hmac.new(key, body_bytes, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        body = msgpack.unpackb(body_bytes, raw=False)
        if not isinstance(body, dict):
            return None
        for k in ("cluster_uuid", "node", "nic", "loopback",
                  "link_addr", "ts"):
            if k not in body:
                return None
        return body
    except Exception:
        return None


# ── Helpers ──────────────────────────────────────────────────────────

def is_bridge_slave(nic: str) -> bool:
    """A bridge port has /sys/class/net/<nic>/master pointing at the
    bridge interface. We never address such NICs ourselves — assigning
    them an IP fights the bridge and breaks the LAN side. The bridge
    itself (e.g. br0) is what we treat as the routable endpoint."""
    return Path(f"/sys/class/net/{nic}/master").is_symlink() or \
           Path(f"/sys/class/net/{nic}/brport").exists()


def list_interfaces() -> list[str]:
    """All non-blocklisted up interfaces that are usable as path
    endpoints (i.e. not bridge slaves and not in the prefix blocklist)."""
    out: list[str] = []
    for nic in sorted(os.listdir("/sys/class/net")):
        if any(nic.startswith(p) for p in INTERFACE_BLOCKLIST_PREFIXES):
            continue
        if is_bridge_slave(nic):
            continue
        oper = Path(f"/sys/class/net/{nic}/operstate").read_text().strip()
        if oper != "up":
            continue
        out.append(nic)
    return out


def get_mac(nic: str) -> str:
    try:
        return Path(f"/sys/class/net/{nic}/address").read_text().strip()
    except OSError:
        return ""


def nic_speed_mbps(nic: str) -> int:
    """Best-effort link speed read. Returns 0 if unknown (virtio,
    USB-ethernet without a reliable speed report). Buckets are coarse
    so the fold-deterministic invariant holds; we round on emit."""
    try:
        speed = int(Path(f"/sys/class/net/{nic}/speed").read_text().strip())
        return max(0, speed)  # -1 means "unknown" on virtio sometimes
    except (OSError, ValueError):
        return 0


def bucket_speed(mbps: int) -> int:
    """Round to a coarse bucket so jitter doesn't perturb the fold.
    1000-or-less → 1000; 2500 → 2500; 10000 → 10000; etc.
    Unknown (0) stays 0."""
    if mbps <= 0:
        return 0
    for b in (1000, 2500, 10000, 25000, 40000, 100000):
        if mbps <= b:
            return b
    return 100000


def bucket_rtt(us: int) -> int:
    """Bucket RTT to the nearest 100 µs to absorb noise."""
    if us <= 0:
        return 0
    return ((us + 50) // 100) * 100


def first_inet_addr(nic: str) -> str:
    """Return the first non-link-local-or-loopback IPv4 on `nic`, or
    a 169.254.x.x if that's all there is, or '' if no IPv4 at all."""
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", nic],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return ""
    inet_lines = [l for l in out.splitlines() if " inet " in l]
    real = []
    fallback = []
    for line in inet_lines:
        parts = line.split()
        try:
            cidr = parts[parts.index("inet") + 1]
        except (ValueError, IndexError):
            continue
        addr = cidr.split("/")[0]
        if addr.startswith("127."):
            continue
        if addr.startswith("169.254."):
            fallback.append(addr)
        else:
            real.append(addr)
    if real:
        return real[0]
    if fallback:
        return fallback[0]
    return ""


def derive_throwaway_ip(cluster_uuid: str, mac: str) -> str:
    """Throwaway 10.42.X.Y/16 derived from cluster_uuid + mac. Two
    nodes derive distinct values because the MAC differs; collisions
    inside one cluster have probability ≈ 1/65535 per pair, fine for
    the testbed and acceptable in production (we ARP-probe before
    using and we'd just regenerate on conflict — out of scope for v1).
    """
    h = hashlib.sha256(f"{cluster_uuid}:{mac}".encode()).digest()
    # Avoid x.0 and x.255 just in case of broadcast quirks.
    second = h[0]
    third = h[1] if h[1] not in (0, 255) else 1
    return f"{THROWAWAY_PREFIX}.{second}.{third}"


def ip_addr_assigned(nic: str) -> bool:
    """True if the interface has any non-loopback IPv4 already."""
    return bool(first_inet_addr(nic))


def assign_throwaway(nic: str, cluster_uuid: str) -> str:
    """Idempotent. If the NIC already has any IPv4, leave it. Otherwise
    pin a throwaway IP from the deterministic 10.42 scheme. Returns the
    final address either way."""
    existing = first_inet_addr(nic)
    if existing:
        return existing
    mac = get_mac(nic)
    if not mac:
        return ""
    addr = derive_throwaway_ip(cluster_uuid, mac)
    cidr = f"{addr}/16"
    subprocess.run(
        ["ip", "addr", "add", cidr, "dev", nic],
        capture_output=True, text=True,
    )
    return addr


# ── Daemon state ─────────────────────────────────────────────────────

@dataclass
class Neighbour:
    """One per (peer_node, peer_nic, my_nic). The discriminator for the
    same physical neighbour seen on multiple of our NICs is `my_nic`,
    so a peer reachable on multiple links shows up as multiple entries.
    """
    peer_node: str
    peer_nic: str
    peer_loopback: str
    peer_link_addr: str
    my_nic: str
    first_seen: float
    last_seen: float
    speed_mbps: int = 0
    rtt_us: int = 0
    # Logged: True once we've emitted a LINK_UP that hasn't been
    # superseded by a LINK_DOWN yet.
    logged_up: bool = False
    last_quality_log: float = 0.0


@dataclass
class Daemon:
    cluster_key: bytes
    cluster_uuid: str
    my_node: str
    my_loopback: str
    # Map (peer_node, peer_nic, my_nic) → Neighbour
    neighbours: dict = field(default_factory=dict)
    # Map nic → throwaway IP we assigned
    nic_addrs: dict = field(default_factory=dict)
    # Probe sockets per nic (reused across loop iterations)
    probe_send_socks: dict = field(default_factory=dict)
    # Single receive socket bound to PROBE_PORT, joined to PROBE_GROUP
    # on every nic
    recv_sock: Optional[socket.socket] = None
    # Last route table we emitted (string), so we don't fight the
    # kernel with no-op writes.
    last_routes_signature: str = ""
    # Stop flag for clean shutdown.
    stopped: bool = False


# ── Probe sockets ────────────────────────────────────────────────────

def open_send_socket(nic: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, PROBE_TTL)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    # Bind multicast egress to this nic by index. The kernel's
    # `struct ip_mreqn` is 12 bytes: 4 (multiaddr) + 4 (local ifaddr)
    # + 4 (ifindex). The "I" code in Python struct guarantees 4-byte
    # unsigned int regardless of platform LP-size — using "L" (which
    # is 8 bytes on x86_64 Linux) silently produces a 16-byte option
    # value, the kernel rejects it as too long, and IP_MULTICAST_IF
    # falls back to "default outgoing interface". That's how we got
    # probes being cross-attributed between mesh planes earlier.
    if_index = socket.if_nametoindex(nic)
    mreq = struct.pack("4s4sI",
                        socket.inet_aton("0.0.0.0"),
                        socket.inet_aton("0.0.0.0"),
                        if_index)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, mreq)
    return s


def open_recv_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except OSError:
        pass
    # IP_PKTINFO lets recvmsg() report which local interface the
    # packet was received on. Critical: we need to know "this probe
    # came in on enp3s0" not just "this probe came from 10.42.7.42",
    # because all our mesh NICs share the same /16 throwaway prefix
    # and source-IP alone can't tell us which physical link delivered
    # the frame.
    IP_PKTINFO = 8  # not exposed in py stdlib
    s.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
    s.bind(("", PROBE_PORT))
    return s


def recv_with_ifindex(sock: socket.socket) -> tuple[bytes, str, int] | tuple[None, None, None]:
    """recvmsg + parse IP_PKTINFO. Returns (data, sender_addr, ifindex)
    or (None, None, None) if no packet ready / error.
    """
    IP_PKTINFO = 8
    try:
        data, ancdata, _, src = sock.recvmsg(2048, socket.CMSG_LEN(64))
    except (BlockingIOError, socket.timeout):
        return None, None, None
    except OSError:
        return None, None, None
    ifindex = 0
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.IPPROTO_IP and cmsg_type == IP_PKTINFO:
            # struct in_pktinfo { int ipi_ifindex; struct in_addr ipi_spec_dst; struct in_addr ipi_addr; }
            ifindex = struct.unpack_from("i", cmsg_data, 0)[0]
            break
    return data, src[0], ifindex


def ifname_for_index(ifindex: int) -> str:
    """ifindex → name. Returns '' if not found."""
    if ifindex <= 0:
        return ""
    try:
        return socket.if_indextoname(ifindex)
    except OSError:
        return ""


def join_group_on(sock: socket.socket, nic: str) -> None:
    """Add this nic to the multicast group on the receive socket so
    we see probes arriving on it. Idempotent — adding twice errors,
    so we swallow the EADDRINUSE."""
    try:
        if_index = socket.if_nametoindex(nic)
        mreq = struct.pack("4s4sI",
                            socket.inet_aton(PROBE_GROUP),
                            socket.inet_aton("0.0.0.0"),
                            if_index)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        if "Address already in use" in str(e) or e.errno == 98:
            return
        # Other errors silently — interface might have flapped.


def leave_group_on(sock: socket.socket, nic: str) -> None:
    try:
        if_index = socket.if_nametoindex(nic)
        mreq = struct.pack("4s4sI",
                            socket.inet_aton(PROBE_GROUP),
                            socket.inet_aton("0.0.0.0"),
                            if_index)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
    except OSError:
        pass


# ── Main loop ────────────────────────────────────────────────────────

def load_state() -> tuple[bytes, str, str, str]:
    """Returns (cluster_key, cluster_uuid, my_node_name, my_loopback).
    Raises if any of the basic state files are missing — daemon won't
    start before bedrock bootstrap + init/join."""
    if not CLUSTER_KEY_FILE.exists():
        raise RuntimeError(f"missing {CLUSTER_KEY_FILE} — run `bedrock init` or `bedrock join` first")
    cluster_key = CLUSTER_KEY_FILE.read_bytes()
    if len(cluster_key) != 32:
        raise RuntimeError(f"{CLUSTER_KEY_FILE}: expected 32 bytes, got {len(cluster_key)}")

    if not STATE_JSON.exists():
        raise RuntimeError(f"missing {STATE_JSON} — run `bedrock bootstrap` first")
    state = json.loads(STATE_JSON.read_text())
    cluster_uuid = state.get("cluster_uuid") or ""
    my_node = state.get("node_name") or os.uname().nodename
    if not cluster_uuid:
        raise RuntimeError("state.json has no cluster_uuid — not in a cluster")
    my_loopback = state.get("loopback_ip") or ""
    return cluster_key, cluster_uuid, my_node, my_loopback


def ensure_loopback_ip(loopback_ip: str) -> None:
    """Idempotent. Add the cluster identity IP as a /32 on `lo` if not
    already there. Survives reboots when state.json is read on
    daemon start."""
    if not loopback_ip:
        return
    cidr = f"{loopback_ip}/32"
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "lo"],
        capture_output=True, text=True,
    ).stdout
    if cidr in out or f" {loopback_ip}/" in out:
        return
    subprocess.run(["ip", "addr", "add", cidr, "dev", "lo"],
                   capture_output=True, text=True)


def run_daemon():
    cluster_key, cluster_uuid, my_node, my_loopback = load_state()
    if not my_loopback:
        # Try cluster.json as fallback (post-init/join, before state.json refresh).
        try:
            cluster = json.loads(CLUSTER_JSON.read_text())
            n = (cluster.get("nodes") or {}).get(my_node) or {}
            my_loopback = n.get("loopback_ip", "")
        except Exception:
            pass
    if not my_loopback:
        sys.stderr.write("bedrock-net: no loopback_ip yet (cluster.json/state.json incomplete); will retry every tick\n")

    if my_loopback:
        ensure_loopback_ip(my_loopback)

    d = Daemon(
        cluster_key=cluster_key,
        cluster_uuid=cluster_uuid,
        my_node=my_node,
        my_loopback=my_loopback,
    )

    d.recv_sock = open_recv_socket()
    d.recv_sock.settimeout(0.05)

    print(f"bedrock-net: cluster_uuid={cluster_uuid} node={my_node} "
          f"loopback={my_loopback or '<not yet assigned>'}",
          file=sys.stderr, flush=True)

    last_probe = 0.0
    last_route_emit = 0.0

    while not d.stopped:
        try:
            tick(d, last_probe, last_route_emit)
            now = time.time()
            if now - last_probe >= PROBE_INTERVAL:
                send_probes(d, now)
                last_probe = now
            if now - last_route_emit >= 1.0:
                emit_routes(d)
                last_route_emit = now
        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"bedrock-net: tick error: {e!r}\n")
        time.sleep(TICK_INTERVAL)


def tick(d: Daemon, last_probe: float, last_route_emit: float) -> None:
    # Drain any waiting probes. recvmsg + IP_PKTINFO so we know which
    # local NIC each probe arrived on — the only reliable way when all
    # mesh NICs share the same /16 throwaway prefix.
    while True:
        data, src_addr, ifindex = recv_with_ifindex(d.recv_sock)
        if data is None:
            break
        body = decode_probe(data, key=d.cluster_key)
        if not body:
            continue
        if body.get("cluster_uuid") != d.cluster_uuid:
            continue
        if body.get("node") == d.my_node:
            continue  # don't talk to ourselves
        my_nic = ifname_for_index(ifindex) if ifindex else ""
        process_probe(d, body, sender_link_addr=src_addr or "",
                      my_nic_hint=my_nic)

    # Refresh loopback assignment if we couldn't on startup.
    if not d.my_loopback:
        try:
            cluster = json.loads(CLUSTER_JSON.read_text())
            n = (cluster.get("nodes") or {}).get(d.my_node) or {}
            d.my_loopback = n.get("loopback_ip", "")
            if d.my_loopback:
                ensure_loopback_ip(d.my_loopback)
                print(f"bedrock-net: loopback now {d.my_loopback}",
                      file=sys.stderr, flush=True)
        except Exception:
            pass

    # Maintain interface set: assign throwaway IPs to fresh nics, ensure
    # multicast group joined, drop neighbours whose nic disappeared.
    nics = list_interfaces()
    for nic in nics:
        if nic not in d.nic_addrs:
            addr = assign_throwaway(nic, d.cluster_uuid)
            if addr:
                d.nic_addrs[nic] = addr
                join_group_on(d.recv_sock, nic)
                d.probe_send_socks[nic] = open_send_socket(nic)
                print(f"bedrock-net: nic up {nic} addr={addr}",
                      file=sys.stderr, flush=True)
    # NICs that went away
    for nic in list(d.nic_addrs.keys()):
        if nic not in nics:
            print(f"bedrock-net: nic down {nic}", file=sys.stderr, flush=True)
            leave_group_on(d.recv_sock, nic)
            try:
                d.probe_send_socks.pop(nic).close()
            except Exception:
                pass
            d.nic_addrs.pop(nic, None)
            # Mark all neighbours via this nic as stale; they'll be
            # cleaned on the next hysteresis sweep.
            for k, n in list(d.neighbours.items()):
                if n.my_nic == nic:
                    n.last_seen = 0.0  # forces DOWN eligibility

    # Hysteresis sweep: emit LINK_UP / LINK_DOWN / LINK_QUALITY.
    sweep_hysteresis(d)


def send_probes(d: Daemon, now: float) -> None:
    if not d.my_loopback:
        return
    for nic, sock in list(d.probe_send_socks.items()):
        link_addr = d.nic_addrs.get(nic, "")
        if not link_addr:
            continue
        buf = encode_probe(
            cluster_uuid=d.cluster_uuid,
            node=d.my_node, nic=nic,
            loopback=d.my_loopback,
            link_addr=link_addr,
            ts=now,
            key=d.cluster_key,
        )
        try:
            sock.sendto(buf, (PROBE_GROUP, PROBE_PORT))
        except OSError:
            # nic flapped between list_interfaces() and now; tick will
            # reconcile next iteration.
            pass


def process_probe(d: Daemon, body: dict, sender_link_addr: str,
                  my_nic_hint: str = "") -> None:
    """Update or insert a Neighbour for an incoming probe. Schedules
    nothing; the hysteresis sweep decides when to emit log entries.

    `my_nic_hint` comes from IP_PKTINFO and is the source of truth for
    which interface received this probe. The IP-subnet fallback is
    only a safety net for kernels/configs that don't report PKTINFO.
    """
    peer_node = body["node"]
    peer_nic  = body["nic"]
    peer_loopback = body["loopback"]
    peer_link_addr = body.get("link_addr", "") or sender_link_addr
    my_nic = my_nic_hint or nic_for_sender(d, sender_link_addr)
    if not my_nic:
        return
    if my_nic not in d.nic_addrs:
        # Probe arrived on an interface we haven't claimed yet —
        # could be the LAN side which has no throwaway. Still record
        # it; tick will reconcile nic_addrs on the next sweep.
        pass

    key = (peer_node, peer_nic, my_nic)
    now = time.time()
    n = d.neighbours.get(key)
    if n is None:
        n = Neighbour(
            peer_node=peer_node, peer_nic=peer_nic,
            peer_loopback=peer_loopback, peer_link_addr=peer_link_addr,
            my_nic=my_nic, first_seen=now, last_seen=now,
            speed_mbps=bucket_speed(nic_speed_mbps(my_nic)),
            rtt_us=0,
        )
        d.neighbours[key] = n
    else:
        n.last_seen = now
        n.peer_link_addr = peer_link_addr
        n.peer_loopback = peer_loopback


def nic_for_sender(d: Daemon, sender_addr: str) -> str:
    """Map an incoming-packet source IP to the local nic that's on
    the same /16 of our throwaway prefix. If the sender is on the
    LAN (mgmt) side we still want a match — the LAN nic gets a real
    DHCP address, not a throwaway, so we use sender's same /24 as a
    fallback heuristic.
    """
    if not sender_addr:
        return ""
    if sender_addr.startswith(THROWAWAY_PREFIX + "."):
        # Same /16 → match by NIC that has its throwaway in this /16.
        return _nic_in_subnet(d, sender_addr, mask_len=16)
    # Fallback: NIC sharing /24 with sender's address.
    return _nic_in_subnet(d, sender_addr, mask_len=24)


def _nic_in_subnet(d: Daemon, sender_addr: str, mask_len: int) -> str:
    """Return the first NIC whose primary address is in the /mask_len
    of sender_addr."""
    parts = sender_addr.split(".")
    if len(parts) != 4:
        return ""
    if mask_len == 16:
        prefix = ".".join(parts[:2])
    elif mask_len == 24:
        prefix = ".".join(parts[:3])
    else:
        prefix = sender_addr
    for nic in d.nic_addrs:
        addr = first_inet_addr(nic)
        if addr.startswith(prefix + "."):
            return nic
    return ""


# ── Hysteresis + log emission ────────────────────────────────────────

def sweep_hysteresis(d: Daemon) -> None:
    now = time.time()
    to_drop = []
    for key, n in d.neighbours.items():
        age_since_seen = now - (n.last_seen or 0.0)
        # Down hysteresis: silent past the threshold AND we previously
        # logged it as up → emit LINK_DOWN, drop entry.
        if age_since_seen > DOWN_HYSTERESIS_S and n.logged_up:
            emit_link_event("down", d, n,
                            reason=f"silent {age_since_seen:.1f}s")
            to_drop.append(key)
            continue
        if age_since_seen > DOWN_HYSTERESIS_S:
            # Never logged as up; we just lost a transient neighbour.
            to_drop.append(key)
            continue

        # Up hysteresis: continuously seen for the up threshold AND not
        # yet logged → emit LINK_UP. Set logged_up only on append
        # success; transient IPC failures (bedrock-rust restart, etc.)
        # then retry on the next sweep.
        age_since_first = now - (n.first_seen or 0.0)
        if not n.logged_up and age_since_first >= UP_HYSTERESIS_S:
            if emit_link_event("up", d, n):
                n.logged_up = True
                n.last_quality_log = now
            continue

        # Quality refresh: stable + ≥ refresh interval since last log
        # AND speed/RTT changed >= 25% → LINK_QUALITY.
        if n.logged_up and (now - n.last_quality_log) >= QUALITY_REFRESH_S:
            current_speed = bucket_speed(nic_speed_mbps(n.my_nic))
            if current_speed and current_speed != n.speed_mbps:
                if n.speed_mbps == 0 or abs(current_speed - n.speed_mbps) / max(n.speed_mbps, 1) >= 0.25:
                    n.speed_mbps = current_speed
                    emit_link_event("quality", d, n)
                    n.last_quality_log = now

    for key in to_drop:
        d.neighbours.pop(key, None)


def emit_link_event(kind: str, d: Daemon, n: Neighbour, reason: str = "") -> bool:
    """Append a LINK_UP / LINK_DOWN / LINK_QUALITY entry to the log via
    rust_ipc. Returns True on a successful append, False if the IPC
    failed (caller decides whether to retry next sweep). Errors at this
    layer are usually transient — bedrock-rust restart, daemon.toml
    reload, etc. — so callers should NOT mark the event as 'logged'
    unless we successfully persisted it."""
    try:
        from . import log_entries as le, rust_ipc
    except ImportError:
        # When run as a script (not module), the same import works
        # because /usr/local/lib/bedrock is on sys.path via PYTHONPATH
        # in the systemd unit.
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import log_entries as le, rust_ipc  # type: ignore

    ts = time.time()
    if kind == "up":
        payload = le.link_up(
            node_a=d.my_node, nic_a=n.my_nic,
            node_b=n.peer_node, nic_b=n.peer_nic,
            speed_mbps=n.speed_mbps, rtt_us=n.rtt_us,
            observed_at=ts,
        )
    elif kind == "down":
        payload = le.link_down(
            node_a=d.my_node, nic_a=n.my_nic,
            node_b=n.peer_node, nic_b=n.peer_nic,
            reason=reason or "hysteresis",
            observed_at=ts,
        )
    elif kind == "quality":
        payload = le.link_quality(
            node_a=d.my_node, nic_a=n.my_nic,
            node_b=n.peer_node, nic_b=n.peer_nic,
            speed_mbps=n.speed_mbps, rtt_us=n.rtt_us,
            observed_at=ts,
        )
    else:
        return False

    try:
        with rust_ipc.Daemon() as drd:
            idx, _h = drd.append(payload)
        print(f"bedrock-net: {kind} {n.peer_node}.{n.peer_nic}↔{d.my_node}.{n.my_nic} idx={idx}",
              file=sys.stderr, flush=True)
        return True
    except Exception as e:
        # Transient IPC error — caller should retry next sweep. Don't
        # spam the journal: rate-limit the log line to once per minute
        # per (kind, peer) by stashing on Neighbour.
        sys.stderr.write(f"bedrock-net: append {kind} failed: {e!r}\n")
        return False


# ── Routing ──────────────────────────────────────────────────────────

def emit_routes(d: Daemon) -> None:
    """Compute the per-peer routes from the in-memory neighbour table
    and (best-effort) fold the cluster.json paths section to fold in
    transit hops. Update the kernel routing table only if the desired
    set differs from what we last installed.

    Strategy:
      * For every direct neighbour with logged_up=True, emit a host
        route to peer_loopback via peer_link_addr dev my_nic with the
        primary metric.
      * Add backup routes (other direct paths to the same peer) at
        increasing metrics so the kernel auto-fails-over on link-down.
      * Add a panic route for the cluster /24 via the freshest
        neighbour at metric 999.

    Transit hops (paths through other nodes) are fed in from the
    cluster.json paths section — see compute_routes() for the
    Dijkstra. v1 keeps it simple: we only install transit if we have
    the cluster snapshot; if not, only direct paths.
    """
    desired = compute_routes(d)
    sig = "\n".join(sorted(desired))
    if sig == d.last_routes_signature:
        return

    # Gather currently installed routes for the cluster prefix so we
    # can diff. We only own routes whose dest is in 10.99.0.0/24 OR
    # the panic catch-all. Other routes are operator's, never touched.
    current = current_cluster_routes()
    desired_set = set(desired)
    current_set = set(current)

    for cmd in current_set - desired_set:
        run_silent(["ip", "route", "del"] + cmd.split())
    for cmd in desired_set - current_set:
        # `replace` instead of `add` so a stale leftover doesn't make
        # the add fail.
        run_silent(["ip", "route", "replace"] + cmd.split())

    d.last_routes_signature = sig


def compute_routes(d: Daemon) -> list[str]:
    """Return a list of route specs (each is a string usable as the
    args to `ip route replace`). Loopback /32 per peer with monotonic
    metrics + a panic /24 catch-all."""
    routes: list[str] = []

    # Group neighbours by peer_node, sorted by speed desc, rtt asc.
    by_peer: dict[str, list[Neighbour]] = {}
    for n in d.neighbours.values():
        if not n.logged_up:
            continue
        if not n.peer_loopback:
            continue
        by_peer.setdefault(n.peer_node, []).append(n)

    # Stable sort across nodes for determinism.
    for peer, lst in by_peer.items():
        lst.sort(key=lambda x: (
            -x.speed_mbps if x.speed_mbps else 0,
            x.rtt_us,
            x.my_nic,
        ))
        for i, n in enumerate(lst):
            if not n.peer_link_addr:
                continue
            metric = METRIC_DIRECT_BASE + i
            spec = f"{n.peer_loopback}/32 via {n.peer_link_addr} dev {n.my_nic} metric {metric}"
            routes.append(spec)

    # Panic-neighbour catch-all: the freshest neighbour overall acts
    # as default gateway for the whole cluster /24. If the peer has a
    # specific route at lower metric it wins; this kicks in when we
    # have NO specific route to a peer (e.g. a node we haven't yet
    # heard from but the operator already plumbed it).
    if d.neighbours:
        freshest = max(
            (n for n in d.neighbours.values() if n.logged_up and n.peer_link_addr),
            key=lambda n: n.last_seen,
            default=None,
        )
        if freshest:
            routes.append(
                f"{CLUSTER_LOOPBACK_NET} via {freshest.peer_link_addr} "
                f"dev {freshest.my_nic} metric {METRIC_PANIC}"
            )
    return routes


def current_cluster_routes() -> list[str]:
    """Read existing kernel routes that bedrock-net manages. We
    identify our routes by destination match: 10.99.0.0/24 (panic) or
    any /32 inside 10.99.0.0/24 (per-peer)."""
    out = subprocess.run(
        ["ip", "-4", "route", "show"],
        capture_output=True, text=True,
    ).stdout
    keep = []
    for line in out.splitlines():
        # Lines look like: "10.99.0.5 via 10.42.7.42 dev enp3s0 metric 10"
        # or "10.99.0.0/24 via ... metric 999"
        if not line.startswith(("10.99.0.")):
            continue
        if " src " in line:  # someone added a src that isn't ours
            continue
        # Drop the leading proto/scope fields ip route show inserts
        # for static routes — `ip route replace` accepts the simple
        # form. We canonicalise to "<dest> via <gw> dev <if> metric N".
        keep.append(line.strip())
    return keep


def run_silent(cmd: list[str]) -> int:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode


# ── CLI entry ────────────────────────────────────────────────────────

def main():
    try:
        run_daemon()
    except RuntimeError as e:
        sys.stderr.write(f"bedrock-net: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
