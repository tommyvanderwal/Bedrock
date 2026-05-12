"""Bedrock mesh-network daemon (`bedrock-net.service`).

See `netd.md` (next to this file) for the implementation reference
(function inventory, state shapes, kernel state touched, invariants).
See `docs/06-mesh-network.md` for the high-level design rationale
and operational verification.

Single Python daemon that runs on every node and owns the layer
between L2-cable-up and "DRBD/libvirt/NFS can talk to a peer's
loopback IP." The short version:

  * Every node has ONE cluster identity = a /32 loopback IP recorded
    in cluster.json (via NODE_LOOPBACK log entry, set at init/join).
    Per-NIC IPs ARE logged (as link_addr_a/b in LINK_UP/LINK_QUALITY)
    so DRBD's multi-path config can list them in path blocks.
  * On every up non-blocklisted interface, this daemon emits a signed
    UDP multicast probe every 1 s. Recipients verify the cluster_key
    HMAC, learn "node X's loopback Y is reachable on this link via
    address Z." The interface blocklist filters lo / virbr / docker /
    br-* / veth / tap / tun / wg / kube / cali / cni prefixes and
    any interface that's enslaved to a bridge (bridge ports get no
    /32 because the bridge itself is the routable endpoint).
  * In-memory gossip is realtime; only durable transitions get
    appended to the bedrock-rust log: LINK_UP after 5 s continuous,
    LINK_DOWN after 30 s silent, LINK_QUALITY is rate-limited first
    (max once per QUALITY_REFRESH_S = 60 s) AND change-sensitive
    second (≥25% speed bucket change). Order matters: we never
    emit a quality event sooner than the gate, even on a big change.
  * Single-writer rule: only the mgmt master appends LINK_* entries
    to the log. Followers return success from emit_link_event()
    without appending so their hysteresis state still advances (and
    they don't retry forever) — but the log itself stays
    single-writer. Without this, follower writes diverge the hash
    chain from master's and break replication of subsequent
    membership entries.
  * Routing: each tick rebuilds the desired routes from the
    in-memory neighbour table (no Dijkstra in v1 — we do direct
    routing per peer, one /32 per direct link in metric order plus
    a panic /24 catch-all; transit routing through other nodes is a
    v1.x add). Backup routes installed at monotonic metrics so the
    kernel fails over for free on link-down. Black-hole gateway
    detection comes from this daemon's own probe-loss → it
    `ip route del` the dead route, the next-metric one auto-promotes.
  * Panic-neighbour catch-all `<cluster /24> via <freshest peer>
    metric 999` is always installed when at least one neighbour is
    reachable. Cluster /24 is derived from cluster_uuid (RFC 6598
    100.64.0.0/10). Loops are bounded by IP TTL; TCP backoff and
    UDP's low volume keep the worst case from being noisy.

Concretely this file holds:
  * `Daemon` dataclass — the run-loop state. Started by
    /usr/local/bin/bedrock-net via the bedrock-net.service systemd
    unit.
  * Probe codec (msgpack + HMAC-SHA256 over cluster_key).
  * Interface set maintenance — every tick walks /sys/class/net to
    find up non-blocklisted NICs; no kernel-event subscription. NICs
    coming up/down are detected within one tick (~250 ms).
  * Link-local IP assignment via NetworkManager (preferred — creates
    a per-NIC `bedrock-mesh-<nic>` profile with ipv4.method=link-
    local so NM does the RFC 3927 ARP probe + claim) with a kernel-
    only fallback (`ip addr add` of a MAC-derived 169.254.x.y) when
    nmcli isn't available.
  * Hysteresis logic (sweep_hysteresis).
  * Cross-segment LL collision detection + RFC 3927 ARP defense
    countermeasure (_detect_and_handle_ll_collision +
    arp_force_renumber).
  * Route emitter (`ip route` shell-outs via emit_routes() /
    compute_routes(); one place that touches the cluster prefixes,
    so we never fight ourselves).

DRBD config regen on path-table change lives in
`installer/lib/tier_storage.py::regen_drbd_configs_from_snapshot`,
called by mgmt/orchestrator.py's subscriber. Not in this file.

Style: pure stdlib + msgpack. No external deps beyond what
packages.py installs cluster-wide. The daemon is single-threaded
with a ~250 ms main-loop tick; receive happens inline via recvmsg
(non-blocking, drained until empty each tick).
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

# Loopback identity range — derived per-cluster from cluster_uuid via
# cluster_addr.cluster_loopback_prefix(). Lives in RFC 6598 Shared
# Address Space (100.64.0.0/10) so it can't collide with operator
# LANs. Computed at daemon startup; used here only for legacy log
# strings — the actual values come from Daemon state at runtime.

# Per-NIC link-layer IPs come from IPv4 link-local (169.254.0.0/16,
# RFC 3927). NetworkManager handles the actual assignment — ARP
# probes for collisions, picks a free address in the LL block,
# retries on conflict, persists the choice across reboots. We just
# create a per-NIC `con` profile with `ipv4.method=link-local`.
#
# Why not assign IPs ourselves: the LL block is reserved by IANA so
# operators' real LANs can't be in it (unlike picking a random RFC
# 1918 prefix and hoping); NM's RFC-3927 implementation has the
# probe-retry-stable-across-reboot guarantees we'd otherwise have
# to write from scratch; and we keep one source of truth for what
# IP each NIC has — the kernel.
LINK_LOCAL_PROFILE_PREFIX = "bedrock-mesh-"

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
    """Return the first IPv4 on `nic` — preferring real (DHCP-assigned
    or operator-static) over link-local — or '' if no IPv4 at all.
    Loopback addresses are excluded."""
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


def _nmcli_con_exists(name: str) -> bool:
    r = subprocess.run(
        ["nmcli", "-t", "-f", "NAME", "con", "show"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return False
    return name in r.stdout.splitlines()


def ensure_link_local(nic: str) -> str:
    """Make sure `nic` has an IPv4 address — creating a NetworkManager
    connection profile with `ipv4.method=link-local` if no profile
    yet exists for this NIC. NM does the actual assignment: ARP
    probes the LL block, picks a free 169.254.x.y, retries on
    collision, persists the choice across reboots. We just trigger
    the request.

    Idempotent. If the NIC already has an IPv4 (DHCP from the LAN
    bridge, operator-configured static, or NM has already brought up
    a previously-created link-local profile), leave it alone — that
    address is what the cluster mesh will use.

    Returns the resulting IPv4 address, or '' if NM isn't installed
    or the NIC didn't pick up an address within the wait window."""
    existing = first_inet_addr(nic)
    if existing:
        return existing

    if not subprocess.run(["which", "nmcli"], capture_output=True).returncode == 0:
        # No NM — fall back to a kernel-only LL claim. RFC 3927 says
        # ARP-probe before claiming, but at minimum we can stick *some*
        # address on so the mesh layer can send/receive. The hash
        # gives us a stable seed; the chance of a real collision on
        # an isolated mesh segment is small in practice. Operators
        # with no NM SHOULD install systemd-networkd and configure
        # `LinkLocalAddressing=ipv4`, which we'll detect on a future
        # boot.
        mac = get_mac(nic)
        if not mac:
            return ""
        h = hashlib.sha256(mac.encode()).digest()
        # Skip the reserved RFC 3927 boundary subnets.
        second = max(1, min(254, h[0]))
        third  = h[1] if h[1] not in (0, 255) else 1
        addr = f"169.254.{second}.{third}"
        subprocess.run(["ip", "addr", "add", f"{addr}/16", "dev", nic],
                       capture_output=True, text=True)
        return addr

    profile = LINK_LOCAL_PROFILE_PREFIX + nic
    if not _nmcli_con_exists(profile):
        subprocess.run([
            "nmcli", "con", "add",
            "type", "ethernet",
            "ifname", nic,
            "con-name", profile,
            "ipv4.method", "link-local",
            "ipv6.method", "ignore",
            "connection.autoconnect", "yes",
        ], capture_output=True, text=True, timeout=10)

    # Bring it up. `nmcli con up` blocks until the connection is
    # active OR fails; since LL doesn't need DHCP we expect activation
    # within 1–2 s after the ARP probe completes.
    subprocess.run(
        ["nmcli", "con", "up", profile],
        capture_output=True, text=True, timeout=15,
    )

    # Wait up to ~15 s for an address to appear (covers RFC 3927's
    # 4 ARP probes × 1 s probe interval + announcement window).
    for _ in range(30):
        addr = first_inet_addr(nic)
        if addr:
            return addr
        time.sleep(0.5)
    return ""


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
    # Smoothed RTT from ICMP latency measurement (protocol 2). Zero
    # means "no sample yet"; first non-outlier sample seeds the EWMA.
    rtt_us: int = 0
    rtt_var_us: int = 0
    rtt_outlier_streak: int = 0
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
    # Map nic → IPv4 address (link-local from NM, or DHCP for the LAN side)
    nic_addrs: dict = field(default_factory=dict)
    # Probe sockets per nic (reused across loop iterations)
    probe_send_socks: dict = field(default_factory=dict)
    # Single receive socket bound to PROBE_PORT, joined to PROBE_GROUP
    # on every nic
    recv_sock: Optional[socket.socket] = None
    # Last route table we emitted (string), so we don't fight the
    # kernel with no-op writes.
    last_routes_signature: str = ""
    # Cooldown for the ARP-defense collision countermeasure: keyed by
    # (peer_link_addr, my_nic) → last-fire timestamp. Each entry has
    # a 30 s "respect-renumber-window" before we'll fire again. Stops
    # us from sending fresh ARP rounds every sweep while the loser is
    # still mid-NM-renumber. Multiple discoverers in parallel are
    # still fine — each maintains its own cooldown.
    last_arp_renumber: dict = field(default_factory=dict)
    # ICMP latency-probing state (protocol 2): one pinger per local
    # NIC. The pinger owns a non-blocking unprivileged ICMP socket
    # bound to that NIC's link-local address.
    icmp_pingers: dict = field(default_factory=dict)
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
    # came in on enp3s0" not just "this probe came from 169.254.X.Y",
    # because all our mesh NICs share the same /16 (link-local) and
    # source-IP alone can't tell us which physical link delivered
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
    last_icmp = 0.0

    while not d.stopped:
        try:
            tick(d, last_probe, last_route_emit)
            now = time.time()
            now_mono_ns = time.monotonic_ns()
            if now - last_probe >= PROBE_INTERVAL:
                send_probes(d, now)
                last_probe = now
            # Protocol 2: ICMP latency. Drain replies every tick (cheap
            # — non-blocking recv); send a fresh round every
            # ICMP_INTERVAL_S.
            icmp_drain_replies(d, now_mono_ns)
            if now - last_icmp >= ICMP_INTERVAL_S:
                icmp_send_round(d, now_mono_ns)
                last_icmp = now
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
    # local NIC each probe arrived on — the only reliable way when
    # multiple mesh NICs share the same /16 (link-local).
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

    # Maintain interface set: drive NetworkManager to assign IPv4
    # link-local on fresh NICs (or pick up whatever address the OS
    # already has), ensure multicast group joined, drop neighbours
    # whose nic disappeared.
    nics = list_interfaces()
    for nic in nics:
        if nic not in d.nic_addrs:
            addr = ensure_link_local(nic)
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
        # could be a NIC whose NM activation lags our discovery.
        # Still record the neighbour; tick will reconcile next sweep.
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
    """Best-effort fallback for receivers that don't get IP_PKTINFO
    (kernel/config quirk). Maps an incoming-packet source IP to a
    local NIC by shared subnet. The primary path is always the
    PKTINFO ifindex from recvmsg — this fallback is here so we
    degrade gracefully if PKTINFO is missing for some reason.
    Link-local senders share a /16 (169.254.0.0/16); LAN senders
    share a /24 with our DHCP NIC; everything else is a guess.
    """
    if not sender_addr:
        return ""
    if sender_addr.startswith("169.254."):
        return _nic_in_subnet(d, sender_addr, mask_len=16)
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


def _peer_in_local_subnet(d: Daemon, peer_addr: str) -> bool:
    """True if peer_addr is on the same /24 as one of our own NIC IPs.
    Used to skip installing a /32 host route when the kernel's
    connected-route handles it already (typical LAN bridge case)."""
    return bool(_nic_in_subnet(d, peer_addr, 24))


def arp_force_renumber(target_addr: str, dev: str) -> None:
    """RFC 3927 §2.5 countermeasure for a cross-segment IPv4 link-local
    collision.

    Two peers on different L2 segments — say peer_X on mesh-1 and
    peer_Y on mesh-2 — independently negotiated the same 169.254.X.Y.
    Within-segment ARP didn't catch it because the segments are
    isolated; bedrock-net detected it by a /32 host-route conflict on
    THIS node (which has interfaces on both segments).

    The cleanest way to break the tie is to use APIPA's own defense
    mechanism: emit a gratuitous ARP announcement on the loser's
    segment claiming `target_addr`. The loser's stack sees a
    different MAC asserting its own IP, defends once per RFC 3927,
    sees the announcement persist on retry, and renumbers via fresh
    ARP-probe round.

    We use OUR OWN MAC for the announcement. Two IPs on one MAC is
    legal — the loser's stack only cares that the MAC differs from
    its own, not whether the announcer "really" owns the address.
    Logged loud so an operator inspecting the journal sees what
    happened.

    Sends three announcements 0.5 s apart — first triggers the
    loser's one-shot defense, second confirms the conflict (renumber
    path), third is plain belt-and-suspenders for the case where one
    frame gets lost on the wire. Total time on segment: ~1.5 s, ~3×
    42 B = 126 B.
    """
    if not target_addr.startswith("169.254."):
        return  # not a link-local target; nothing to renumber
    my_mac_str = get_mac(dev)
    if not my_mac_str:
        return
    try:
        my_mac = bytes.fromhex(my_mac_str.replace(":", ""))
    except ValueError:
        return
    bcast = b"\xff" * 6
    addr_bytes = socket.inet_aton(target_addr)

    # Gratuitous ARP announcement (op=2/reply, sender_ip == target_ip):
    #   "I am <target_addr> at <my_mac>".
    # RFC 3927 §2.4 + §2.5: any host claiming the same address sees
    # this and either defends once (which we ignore) or renumbers.
    arp = struct.pack(
        "!HHBBH6s4s6s4s",
        1,       # htype = Ethernet
        0x0800,  # ptype = IPv4
        6, 4,    # hlen / plen
        2,       # op = reply
        my_mac, addr_bytes,   # sender hw / proto
        bcast,   addr_bytes,  # target hw / proto (broadcast)
    )
    frame = bcast + my_mac + b"\x08\x06" + arp

    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806))
    try:
        s.bind((dev, 0x0806))
        for _ in range(3):
            s.send(frame)
            time.sleep(0.5)
    finally:
        s.close()


# Per-/32 deduplication state — addrs we've already either installed
# or engaged the renumber countermeasure on this sweep. Reset each
# call to compute_routes(); persisting across sweeps would re-engage
# every tick while the colliding peer is still mid-renumber.
def _detect_and_handle_ll_collision(routes: list[str],
                                     seen: dict[str, tuple],
                                     n: "Neighbour", d: "Daemon") -> bool:
    """Decide whether to add the /32 for `n.peer_link_addr` to `routes`.

    Discriminates three cases by `(peer_node, peer_nic, my_nic)`:

      1. First time we see this address: record + append /32, return.
      2. Same `(peer_node, peer_nic)` already seen on a different
         `my_nic` → segment merge, NOT a real collision. The same
         peer interface is reachable via two of our NICs because
         the operator (or a switch loop) bridged two L2s together.
         The legitimate peer keeps its IP; we skip the /32 for our
         second NIC and log clearly. Firing the countermeasure here
         would chase a valid peer off its address and cascade.
      3. Same address, DIFFERENT `(peer_node, peer_nic)`, different
         `my_nic` → real cross-segment LL birthday-paradox collision.
         Engage ARP-defense countermeasure to force renumber.
      4. Same address, same nic → duplicate entry within sweep, skip.

    The `(peer_node, peer_nic)` discriminator is in the probe payload
    (signed by cluster_key, so a malicious actor can't forge it), and
    it's already in our Neighbour state — we don't need an ARP-cache
    lookup to tell merges from real collisions.
    """
    addr = n.peer_link_addr
    if not addr:
        return False
    prev = seen.get(addr)
    if prev is None:
        seen[addr] = (n.my_nic, n.peer_node, n.peer_nic)
        routes.append(f"{addr}/32 dev {n.my_nic} scope link")
        return True
    prev_my_nic, prev_peer_node, prev_peer_nic = prev

    # Case 2: same peer interface, multiple of our nics — segment merge.
    if (n.peer_node, n.peer_nic) == (prev_peer_node, prev_peer_nic):
        sys.stderr.write(
            f"bedrock-net: same peer interface "
            f"({n.peer_node}.{n.peer_nic}) reachable via both our "
            f"{prev_my_nic} and {n.my_nic} — looks like an L2 bridge "
            f"merge, NOT a cross-segment collision. Skipping ARP "
            f"countermeasure (firing it would chase a legitimate peer "
            f"off its address).\n"
        )
        return False

    # Case 4: redundant within-sweep entry.
    if prev_my_nic == n.my_nic:
        return False

    # Case 3: real cross-segment collision (same address, different
    # peer interfaces, different segments). Per-(addr, nic) cooldown
    # prevents multiple consecutive sweeps from re-firing while the
    # loser is still mid-renumber. Multiple discoverers in parallel
    # each fire once because each maintains its own cooldown — by
    # design, three frames-per-discoverer × N discoverers is fine
    # since RFC 3927 defense is idempotent.
    now = time.time()
    cooldown_key = (addr, n.my_nic)
    last_fired = d.last_arp_renumber.get(cooldown_key, 0.0)
    if now - last_fired < 30.0:
        return False
    sys.stderr.write(
        f"bedrock-net: LL COLLISION DETECTED — {addr} reachable via "
        f"both {prev_my_nic} (peer {prev_peer_node}.{prev_peer_nic}) "
        f"and {n.my_nic} (peer {n.peer_node}.{n.peer_nic}); engaging "
        f"ARP-defense countermeasure on {n.my_nic} to force renumber.\n"
    )
    d.last_arp_renumber[cooldown_key] = now
    try:
        arp_force_renumber(addr, n.my_nic)
        sys.stderr.write(
            f"bedrock-net: countermeasure complete on {n.my_nic}; "
            f"expecting peer to renumber within ~5 s "
            f"(cooldown 30 s before re-fire).\n"
        )
    except OSError as e:
        sys.stderr.write(
            f"bedrock-net: ARP countermeasure failed on {n.my_nic}: "
            f"{e}; cluster will keep working via other paths but "
            f"this peer-link stays unrouted until renumber.\n"
        )
    return False


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


# ── ICMP latency measurement (protocol 2 of three) ──────────────────
#
# Per the docs/06-mesh-network.md "three protocols, one job each"
# architecture: discovery rides multicast (above), latency rides
# ICMP echo with kernel timestamps (here), advertisement rides
# unicast (below). Each is independently observable + fails closed.
#
# Why unprivileged ICMP and not a custom UDP echo: kernel hot-path,
# 40 years of testing, externally validatable via tcpdump/mtr, no
# userspace echoer needed on the peer (peer's kernel responds).
#
# Why our own ICMP socket and not the `ping` shell utility:
# per-NIC source binding + sub-ms timing matter; subprocess startup
# alone adds milliseconds of jitter, swamping the signal we're
# trying to measure on sub-ms LANs.

ICMP_INTERVAL_S = 2.0
ICMP_TIMEOUT_S  = 0.5
RTT_OUTLIER_ABS_US = 100_000   # 100 ms absolute on a sub-ms LAN = noise

# TCP RFC 6298 EWMA constants
RTT_ALPHA = 0.125    # smoothed RTT mixing weight
RTT_BETA  = 0.25     # variance mixing weight


def icmp_checksum(data: bytes) -> int:
    """Standard internet checksum over a bytestring; pads odd length."""
    if len(data) & 1:
        data = data + b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += (data[i] << 8) | data[i + 1]
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return (~s) & 0xFFFF


def build_icmp_echo(identifier: int, seq: int, payload: bytes = b"bedrock") -> bytes:
    """ICMP echo request packet (type 8, code 0). Kernel will fill in
    the IP header for us via SOCK_DGRAM/IPPROTO_ICMP."""
    icmp_type, code = 8, 0
    header_without_csum = struct.pack("!BBHHH", icmp_type, code, 0,
                                       identifier & 0xFFFF, seq & 0xFFFF)
    pkt_for_csum = header_without_csum + payload
    csum = icmp_checksum(pkt_for_csum)
    header = struct.pack("!BBHHH", icmp_type, code, csum,
                          identifier & 0xFFFF, seq & 0xFFFF)
    return header + payload


def parse_icmp_reply_seq(buf: bytes) -> int | None:
    """Pull the (identifier, seq) out of an ICMP echo reply that the
    kernel handed us. The Linux unprivileged ICMP socket strips the
    IP header for us — the first byte is the ICMP type. Returns the
    sequence number if this is a valid echo reply, or None.

    Even after Linux's NAT remap of the identifier on
    SOCK_DGRAM/IPPROTO_ICMP, the sequence number is preserved
    verbatim, so we use it as the pending-request key."""
    if len(buf) < 8:
        return None
    icmp_type = buf[0]
    if icmp_type != 0:    # 0 == echo reply
        return None
    _ident, seq = struct.unpack("!HH", buf[4:8])
    return seq


@dataclass
class IcmpPinger:
    """One per (peer_node, peer_nic, my_nic): owns a non-blocking ICMP
    socket bound to my_link_addr, with a per-instance sequence counter
    and a tiny outstanding-probe map keyed by sequence number.

    Sockets are pooled per `my_nic` (one per local NIC), not per peer,
    because the kernel routes based on the destination /32 we
    installed via emit_routes. Many peers can share the same source
    socket. The pending map is per-(seq, peer) so reply
    dis-ambiguation works without collision."""
    sock: socket.socket
    seq:  int = 1
    pending: dict = field(default_factory=dict)
        # seq -> (peer_link_addr, send_ts_monotonic_ns)


def _ensure_icmp_socket(d: Daemon, my_nic: str) -> socket.socket | None:
    """One unprivileged ICMP socket per local NIC. Bound to that NIC's
    link-local address so the kernel uses it as the source. Returns
    None if the kernel doesn't allow unprivileged ICMP (no entry in
    /proc/sys/net/ipv4/ping_group_range covering our gid)."""
    p = d.icmp_pingers.get(my_nic)
    if p is not None:
        return p.sock
    src_addr = d.nic_addrs.get(my_nic, "")
    if not src_addr:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                           socket.IPPROTO_ICMP)
        s.bind((src_addr, 0))
        s.setblocking(False)
    except OSError as e:
        sys.stderr.write(
            f"bedrock-net: icmp socket on {my_nic} ({src_addr}) failed: "
            f"{e}. Ensure /proc/sys/net/ipv4/ping_group_range covers "
            f"the daemon's gid; latency measurement disabled on this "
            f"NIC until next try.\n"
        )
        return None
    d.icmp_pingers[my_nic] = IcmpPinger(sock=s)
    return s


def icmp_send_round(d: Daemon, now_mono_ns: int) -> None:
    """Send one ICMP echo request to every logged-up neighbour through
    its specific NIC. Non-blocking; receive happens in the drain.
    Called every ICMP_INTERVAL_S from the main loop."""
    for key, n in list(d.neighbours.items()):
        if not n.logged_up or not n.peer_link_addr:
            continue
        sock = _ensure_icmp_socket(d, n.my_nic)
        if sock is None:
            continue
        pinger = d.icmp_pingers[n.my_nic]
        seq = pinger.seq
        pinger.seq = (pinger.seq + 1) & 0xFFFF
        pkt = build_icmp_echo(identifier=os.getpid() & 0xFFFF, seq=seq)
        try:
            sock.sendto(pkt, (n.peer_link_addr, 0))
        except OSError:
            continue
        # Stash send time + dest so we can compute RTT on reply.
        pinger.pending[seq] = (n.peer_link_addr, now_mono_ns,
                                key)  # key for the neighbour update


def icmp_drain_replies(d: Daemon, now_mono_ns: int) -> None:
    """Drain every ICMP socket's receive queue, match replies to
    pending sends by sequence, update Neighbour's smoothed RTT."""
    for my_nic, pinger in list(d.icmp_pingers.items()):
        while True:
            try:
                data, addr = pinger.sock.recvfrom(2048)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                break
            seq = parse_icmp_reply_seq(data)
            if seq is None:
                continue
            pend = pinger.pending.pop(seq, None)
            if pend is None:
                continue   # late reply, already timed out
            peer_link_addr_expected, send_ts, neigh_key = pend
            if addr[0] != peer_link_addr_expected:
                continue   # reply from someone else with same seq, ignore
            sample_us = (now_mono_ns - send_ts) / 1000.0
            _update_neighbour_rtt(d, neigh_key, sample_us)

        # Expire pending sends past timeout, so the map doesn't grow.
        cutoff_ns = now_mono_ns - int(ICMP_TIMEOUT_S * 1_000_000_000)
        for seq in [s for s, p in pinger.pending.items()
                     if p[1] < cutoff_ns]:
            pinger.pending.pop(seq, None)


def _update_neighbour_rtt(d: Daemon, neigh_key: tuple,
                           sample_us: float) -> None:
    """TCP RFC 6298 EWMA on the per-neighbour RTT, with outlier
    rejection BEFORE smoothing so a transient 230 ms hiccup on a
    100 µs link doesn't poison the smoothed value or kick off route
    reshuffling. See docs/06-mesh-network.md §protocol 2."""
    n = d.neighbours.get(neigh_key)
    if n is None:
        return
    srtt = float(n.rtt_us)
    rttvar = float(n.rtt_var_us)

    if srtt <= 0:
        # First sample — accept as-is, seed rttvar with sample/2.
        n.rtt_us = int(sample_us)
        n.rtt_var_us = int(sample_us / 2)
        n.rtt_outlier_streak = 0
        return

    is_outlier = False
    if sample_us > srtt + 4 * rttvar:
        is_outlier = True
    elif srtt > 100 and sample_us > 10 * srtt:
        is_outlier = True
    elif srtt < 5_000 and sample_us > RTT_OUTLIER_ABS_US:
        is_outlier = True

    if is_outlier and n.rtt_outlier_streak < 3:
        # Single transient hiccup; reject the sample but track for
        # genuine degradation that produces 3 consecutive outliers
        # in a row.
        n.rtt_outlier_streak += 1
        return

    # Either not an outlier, or 3+ consecutive outliers (real degrade).
    n.rtt_var_us = int((1 - RTT_BETA) * rttvar
                       + RTT_BETA * abs(sample_us - srtt))
    n.rtt_us     = int((1 - RTT_ALPHA) * srtt + RTT_ALPHA * sample_us)
    n.rtt_outlier_streak = 0


# ── Local metric (per-receiver, format-decoupled) ────────────────────

def local_metric(bw_mbps: int, latency_us: int,
                 loss_rate: float = 0.0, age_s: float = 1e9) -> int:
    """EIGRP-style composite metric, weights tuned for modern speeds.

    bandwidth term: 1_000_000 / Mbps      → 12 at 80G, 100 at 10G,
                                             400 at 2.5G, 1000 at 1G
    latency term:   us / 100              → 1 per 100 µs of RTT
    flap penalty:   +50 if up_since < 60 s (additive, predictable —
                                             not a multiplier)
    loss penalty:   +500 × min(1, loss×20) → graded, not binary

    See docs/06-mesh-network.md §protocol 3.
    """
    bw_cost  = 1_000_000 / max(int(bw_mbps), 1)
    lat_cost = max(int(latency_us), 0) / 100
    flap     = 50 if age_s < 60 else 0
    loss     = 500 * min(1.0, max(0.0, loss_rate) * 20)
    return int(bw_cost + lat_cost + flap + loss)


def i_am_mgmt_master(d: Daemon) -> bool:
    """True if this node currently holds the mgmt-master role per
    state.json (single-writer source of truth). Followers' bedrock-net
    keeps its in-memory neighbour table for local routing decisions
    but does not write to the log — master is the only writer per
    the cluster's single-writer invariant."""
    try:
        state = json.loads(STATE_JSON.read_text())
        return "mgmt" in (state.get("role") or "")
    except Exception:
        return False


def emit_link_event(kind: str, d: Daemon, n: Neighbour, reason: str = "") -> bool:
    """Append a LINK_UP / LINK_DOWN / LINK_QUALITY entry to the log via
    rust_ipc. Only the mgmt master writes; followers return True
    immediately so the hysteresis state machine still records "logged"
    locally and doesn't keep retrying. The cluster-wide path table is
    populated by the master observing its own paths and emitting
    accordingly; followers' own paths reach cluster.json via the
    master's reciprocal observation. Returns True on success or
    follower-skip, False on master IPC failure (caller retries next
    sweep)."""
    if not i_am_mgmt_master(d):
        return True  # follower: don't append, but mark as logged
    try:
        from . import log_entries as le, rust_ipc
    except ImportError:
        # When run as a script (not module), the same import works
        # because /usr/local/lib/bedrock is on sys.path via PYTHONPATH
        # in the systemd unit.
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import log_entries as le, rust_ipc  # type: ignore

    ts = time.time()
    my_link_addr = d.nic_addrs.get(n.my_nic, "")
    if kind == "up":
        payload = le.link_up(
            node_a=d.my_node, nic_a=n.my_nic,
            node_b=n.peer_node, nic_b=n.peer_nic,
            link_addr_a=my_link_addr, link_addr_b=n.peer_link_addr,
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
            link_addr_a=my_link_addr, link_addr_b=n.peer_link_addr,
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
    # can diff. We only own routes whose dest is in this cluster's
    # /24 (derived from cluster_uuid via cluster_addr) plus our
    # 169.254.x.y /32 host routes. Other routes are operator's,
    # never touched.
    current = current_cluster_routes(d.cluster_uuid)
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
    args to `ip route replace`). Two route classes:

      1. **Per-peer-link host routes.** For every observed direct
         path, install `<peer_link_addr>/32 dev <my_nic> scope link`.
         This is what makes DRBD's `path { host A address 169.254.X.Y }`
         resolve to the correct physical exit interface — without
         these /32s, the kernel's auto-installed `169.254.0.0/16
         dev <some_nic>` routes are ambiguous when multiple NICs
         have link-local addresses, and the kernel could send a
         peer-bound packet out the wrong wire (where ARP fails).
         The /32 is more specific than the /16 and wins by
         longest-prefix-match regardless of metric.
      2. **Loopback /32 per peer with monotonic metrics + panic /24
         catch-all.** Drives general cluster traffic to the right
         physical NIC for protocols that talk to peer.loopback_ip
         (libvirt migration, NFS, SSH, garage, the bedrock dashboard).
    """
    routes: list[str] = []

    # Group neighbours by peer_node, sorted by speed desc, rtt asc.
    by_peer: dict[str, list[Neighbour]] = {}
    for n in d.neighbours.values():
        if not n.logged_up:
            continue
        if not n.peer_loopback:
            continue
        by_peer.setdefault(n.peer_node, []).append(n)

    # 1. Per-peer-link host routes (scope link, no via — ARP target).
    #    Bind each peer link address to the local NIC that observed
    #    its probe so DRBD's path blocks resolve unambiguously.
    #    Cross-segment LL collisions get caught here and resolved by
    #    ARP-defense countermeasure (see arp_force_renumber).
    seen_link_addrs: dict[str, str] = {}
    for n in d.neighbours.values():
        if not n.logged_up or not n.peer_link_addr:
            continue
        if not n.peer_link_addr.startswith("169.254.") and \
           _peer_in_local_subnet(d, n.peer_link_addr):
            # LAN bridge case — kernel's connected /24 already handles
            # it; our /32 would just duplicate.
            continue
        _detect_and_handle_ll_collision(routes, seen_link_addrs, n, d)

    # 2. Per-peer loopback /32s, sorted by the EIGRP-style local metric.
    #    See docs/06-mesh-network.md §protocol 3 for the formula.
    #    Stable tiebreak on my_nic ensures every node computes the
    #    same order given the same observables.
    now_s = time.time()
    def _path_cost(n: Neighbour) -> tuple[int, int, str]:
        # Read measured bandwidth from /sys/class/net; falls back to 0
        # which the metric treats as worst-case.
        bw = bucket_speed(nic_speed_mbps(n.my_nic))
        if not bw:
            bw = max(int(n.speed_mbps), 1)
        age_s = now_s - (n.first_seen or now_s)
        cost = local_metric(bw_mbps=bw,
                             latency_us=int(n.rtt_us),
                             loss_rate=0.0,           # TODO: track loss
                             age_s=age_s)
        # Tiebreak on RTT then nic name for determinism.
        return (cost, int(n.rtt_us), n.my_nic)

    for peer, lst in by_peer.items():
        lst.sort(key=_path_cost)
        for i, n in enumerate(lst):
            if not n.peer_link_addr:
                continue
            metric = METRIC_DIRECT_BASE + i
            spec = (f"{n.peer_loopback}/32 via {n.peer_link_addr} "
                    f"dev {n.my_nic} metric {metric}")
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
            from . import cluster_addr as _ca
            net = _ca.cluster_loopback_net(d.cluster_uuid)
            routes.append(
                f"{net} via {freshest.peer_link_addr} "
                f"dev {freshest.my_nic} metric {METRIC_PANIC}"
            )
    return routes


def current_cluster_routes(cluster_uuid: str) -> list[str]:
    """Read existing kernel routes that bedrock-net manages, scoped
    to this cluster's address block. We identify our routes by
    destination match:
      * <cluster_prefix>.0/24 panic catch-all
      * /32 inside <cluster_prefix>.0/24 (per-peer loopback)
      * /32 inside 169.254.0.0/16 with `scope link` (per-peer
        link-local — the kernel's auto-installed /16 connected
        routes don't have a /32 prefix, so we don't trip on them)
    The cluster_prefix is derived deterministically from cluster_uuid
    (cluster_addr.cluster_loopback_prefix), so we don't touch any
    address outside our own /24.
    """
    from . import cluster_addr as _ca
    prefix = _ca.cluster_loopback_prefix(cluster_uuid) + "."
    out = subprocess.run(
        ["ip", "-4", "route", "show"],
        capture_output=True, text=True,
    ).stdout
    keep = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            if " src " in line:
                continue
            keep.append(line)
            continue
        if line.startswith("169.254.") and " scope link" in line:
            head = line.split()[0]
            if "/" not in head:
                keep.append(line)
            continue
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
