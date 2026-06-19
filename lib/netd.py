"""Bedrock mesh-network daemon (`bedrock-net.service`).

See `netd.md` (next to this file) for the implementation reference
(function inventory, state shapes, kernel state touched, invariants).
See `docs/06-mesh-network.md` for the high-level design rationale
and operational verification.

Single Python daemon that runs on every node and owns the layer
between L2-cable-up and "DRBD/libvirt/SeaweedFS can talk to a peer's
loopback IP." The short version:

  * Every node has ONE cluster identity = a /32 loopback IP recorded
    in rqlite's `nodes` table (set at init/join, mirrored in
    state.json). Per-NIC IPs ARE logged (as link_addr_a/b in
    LINK_UP/LINK_QUALITY) so DRBD's multi-path config can list them in
    path blocks.
  * On every up non-blocklisted interface, this daemon emits a signed
    UDP multicast probe every 1 s. Recipients verify the cluster_key
    HMAC, learn "node X's loopback Y is reachable on this link via
    address Z." The interface blocklist filters lo / virbr / docker /
    br-* / veth / tap / tun / wg / kube / cali / cni prefixes and
    any interface that's enslaved to a bridge (bridge ports get no
    /32 because the bridge itself is the routable endpoint).
  * In-memory gossip is realtime; only durable transitions get
    recorded to rqlite (the `paths` table, master-side): LINK_UP after
    5 s continuous, LINK_DOWN after DOWN_HYSTERESIS_S (10 s) silent,
    LINK_QUALITY is rate-limited first
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
  * Routing: each tick rebuilds the desired routes from in-memory
    state: direct paths (per-peer-link host routes + per-peer
    loopback /32s in metric order) PLUS transit /32s computed from
    protocol-3 routing advertisements (path-vector, BGP-shaped, with
    via_chain loop detection). Backup routes installed at monotonic
    metrics so the kernel fails over for free on link-down. The cluster
    VIP (.254 arbiter IP) is one such advertised route, originated by
    whoever currently hosts it — so routing never reads "who is master".
  * Panic catch-all `<cluster /24> via <lowest-octet lower-than-self
    neighbour> metric 999` covers the gap between a peer's withdrawal /
    a fresh joiner and the next adv-table recompute. Master-INDEPENDENT
    (no rqlite read). Forwarding only ever toward a strictly-lower octet
    makes it loop-free by the well-ordering of octets; the global-lowest
    node installs none and sinks unknown traffic (the base case). Cluster
    /24 is derived from cluster_uuid (RFC 6598 100.64.0.0/10).

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
`lib/tier_storage.py::regen_drbd_configs_from_snapshot`,
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
from typing import Any, Optional

import msgpack

try:
    from . import l2disc
    from . import mesh_tcp
except ImportError:
    # When this file is run directly (not as a package member) by the
    # bedrock-net systemd unit, fall back to the explicit sys.path
    # entry that PYTHONPATH puts in place.
    sys.path.insert(0, "/usr/local/lib/bedrock")
    from lib import l2disc  # type: ignore
    from lib import mesh_tcp  # type: ignore


# ── Constants ────────────────────────────────────────────────────────

CLUSTER_KEY_FILE = Path("/etc/bedrock/cluster.key")
CLUSTER_JSON     = Path("/etc/bedrock/cluster.json")
STATE_JSON       = Path("/etc/bedrock/state.json")

PROBE_GROUP = "239.7.7.7"        # private-block multicast
PROBE_PORT  = 7732               # 'BR' = 0x4252 → 7732 prime nearby
PROBE_TTL   = 1                  # link-local only; never crosses routers
# Mesh path-liveness cadences. Probes + route re-emit are mesh path/route
# maintenance (neighbour RTT, stale-adv expiry, route-table re-validation).
# Neighbour-down is gated by DOWN_HYSTERESIS_S=10s. Cluster election/HB
# cadence lives in lib/cluster_daemon.py (independent thread).
PROBE_INTERVAL = 1.5             # seconds between probes per interface
ROUTE_EMIT_INTERVAL_S = 1.5      # periodic route re-emit (changes are event-
                                 # driven via adv-drain; this is the backstop)
TICK_INTERVAL  = 0.25            # main loop tick


UP_HYSTERESIS_S   = 5.0          # link must be up this long before LINK_UP
# Down hysteresis: enough to absorb a few missed probes without flapping
# LINK_DOWN on brief blips. Drives mesh LINK_DOWN / routing only.
DOWN_HYSTERESIS_S = 10.0         # silent this long before LINK_DOWN
QUALITY_REFRESH_S = 60.0         # LINK_QUALITY rate limit when stable


# Rate-limit (monotonic) for the "Echo answering with an unconfigured echo_id"
# warning — a likely echo_id != witness_id misconfiguration.
_LAST_UNCONFIGURED_WARN = 0.0

# Loopback identity range — derived per-cluster from cluster_uuid via
# cluster_addr.cluster_loopback_prefix(). Lives in RFC 6598 Shared
# Address Space (100.64.0.0/10) so it can't collide with operator
# LANs. The actual values come from Daemon state at runtime.

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

# The cluster VIP (.254 arbiter IP) is advertised as an ordinary
# path-vector route originated by whoever currently hosts it (the
# mgmt master). It rides the SAME advertisement packet as every other
# path — no extra hello. `dest` is a sentinel that can't collide with a
# hostname; the address is resolved locally from cluster_uuid on install
# (cluster_addr.cluster_vip), so receivers never read "who is master".
VIP_DEST_SENTINEL   = "@vip"
# Origin bandwidth/latency = the identity elements of the path-composition
# algebra (bw=min(...), lat=sum(...)). Advertising ~infinite bandwidth /
# zero latency at the source makes the composed cost to .254 collapse to
# the true cost of reaching the host — exactly right, since the VIP IS at
# the host. ("connected at 4 TB/s, 0.0 µs".)
VIP_ADV_BW_MBPS     = 1_000_000_000


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


def encode_advertisement(*, cluster_uuid: str, advertiser: str, seq: int,
                          ts: float, paths: list, key: bytes) -> bytes:
    """Sign-then-pack a routing advertisement (protocol 3). Same wrap
    layout as the discovery probe so receivers reuse the verification
    flow: msgpack({v, body, sig}), body is itself msgpack-packed so
    the HMAC input is bit-identical across implementations."""
    body = msgpack.packb({
        "cluster_uuid": cluster_uuid,
        "advertiser":   advertiser,
        "seq":          int(seq) & 0xFFFFFFFF,
        "ts":           float(ts),
        "paths":        paths,
    }, use_bin_type=True)
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return msgpack.packb({"v": ADV_VERSION, "body": body, "sig": sig},
                          use_bin_type=True)


def decode_advertisement(buf: bytes, *, key: bytes) -> Optional[dict]:
    """Verify an advertisement and return the body dict, or None on
    any signature / schema failure. Silent on failure for the same
    reason decode_probe is — random UDP arrives on port 7733 too."""
    try:
        wrap = msgpack.unpackb(buf, raw=False)
        if not isinstance(wrap, dict):
            return None
        if wrap.get("v") != ADV_VERSION:
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
        for k in ("cluster_uuid", "advertiser", "seq", "ts", "paths"):
            if k not in body:
                return None
        if not isinstance(body["paths"], list):
            return None
        return body
    except Exception:
        return None



# ── Helpers ──────────────────────────────────────────────────────────

def is_bridge_slave(nic: str) -> bool:
    """A bridge port has /sys/class/net/<nic>/master pointing at the
    bridge interface. We never address such NICs ourselves — assigning
    them an IP fights the bridge and breaks the LAN side. The bridge
    itself (e.g. br0) is what we treat as the routable endpoint.

    os.path.* not pathlib: this runs per-NIC on every 4Hz tick; Path
    object construction + canonicalization showed up as ~22% of the netd
    thread's CPU in the 2026-06-01 py-spy. os.path.islink/exists are bare
    stat/lstat syscalls with no object churn."""
    return os.path.islink(f"/sys/class/net/{nic}/master") or \
           os.path.exists(f"/sys/class/net/{nic}/brport")


def list_interfaces() -> list[str]:
    """All non-blocklisted up interfaces that are usable as path
    endpoints (i.e. not bridge slaves and not in the prefix blocklist)."""
    out: list[str] = []
    for nic in sorted(os.listdir("/sys/class/net")):
        if any(nic.startswith(p) for p in INTERFACE_BLOCKLIST_PREFIXES):
            continue
        if is_bridge_slave(nic):
            continue
        # try/except (like get_mac): a NIC that vanishes between listdir and
        # this read raised FileNotFoundError → the broad tick handler aborted
        # the WHOLE tick (election + sweep). Treating a vanished NIC as
        # not-up and continuing is the correct behavior, not a crash.
        try:
            with open(f"/sys/class/net/{nic}/operstate") as f:
                oper = f.read().strip()
        except OSError:
            continue
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
    so the fold-deterministic invariant holds; we round on emit.

    Two special cases the raw /sys/class/net/<nic>/speed gets wrong:

      * **Bridges** — Linux's bridge driver hardcodes the bridge's
        reported speed to 10000 regardless of slave speed. The actual
        physical-link rate is the min across non-virtual slaves
        (vnet*/veth* are libvirt's per-VM taps, meaningless here).

      * **Thunderbolt** — the thunderbolt-net driver doesn't populate
        /sys/class/net/<nic>/speed at all. Kernel reads return -1 or
        blank. Real-world Linux `thunderbolt-net` ceilings sit in the
        ~10-25 Gbps range regardless of wire spec — the driver is
        single-RX-queue / single-CPU-softirq bound:

          AMD Phoenix/Hawk Point (Ryzen 7040/8040 USB4)    ~11-12 Gbps
          AMD Strix Halo (Ryzen AI Max, USB4 v2 / TB5)     ~10 Gbps
          Intel Maple Ridge (TB4), untuned                  8-17 Gbps
          Intel Maple Ridge (TB4), IRQ+qdisc tuned         25-26 Gbps
          Intel Barlow Ridge (TB5)                         no public Linux numbers yet

        Wire rate (TB3=40G, TB4=40G, TB5=80G) is irrelevant to TCP;
        the spec-mandated PCIe-tunnel virtual link advertises Gen1
        2.5 GT/s on both Intel and AMD (this is correct, not a bug
        — fixed in kernel 6.7). The throughput gap is in the AMD
        USB4 controller's DMA engine + thunderbolt-net interaction,
        not link negotiation.

        15 Gbps is the honest midpoint: above any common ethernet,
        below the optimistic TB-marketing numbers, within reach of
        every documented Linux platform. The mesh-link preference
        still picks Thunderbolt over a 2.5G LAN bridge.
    """
    # os.path.* + open() not pathlib throughout: this is called once per
    # neighbour across several per-tick loops (probe/adv/quality/route emit),
    # so it ran many times/sec — Path construction + the .resolve() realpath
    # walk for the driver symlink dominated the netd thread's pathlib cost
    # (~27%, py-spy 2026-06-01). Same syscalls, no object churn, and
    # os.readlink replaces resolve()'s full canonicalization.
    base = f"/sys/class/net/{nic}"

    # Bridge: walk slaves, return min physical speed.
    if os.path.isdir(f"{base}/bridge"):
        brif = f"{base}/brif"
        if os.path.isdir(brif):
            speeds: list[int] = []
            for name in os.listdir(brif):
                if name.startswith(("vnet", "veth", "tap", "macvtap")):
                    continue
                try:
                    with open(f"/sys/class/net/{name}/speed") as f:
                        sp = int(f.read().strip())
                    if sp > 0:
                        speeds.append(sp)
                except (OSError, ValueError):
                    continue
            if speeds:
                return min(speeds)
        # No physical slaves yet — fall through to kernel value.

    # Thunderbolt-net: kernel doesn't expose speed; report 15 Gbps —
    # an honest Linux real-world midpoint (see docstring for the
    # platform-by-platform breakdown). os.readlink (the immediate link
    # target's basename = driver name) instead of Path.resolve() — same
    # answer, no realpath canonicalization walk.
    try:
        drv_link = f"{base}/device/driver"
        if os.path.islink(drv_link):
            if os.path.basename(os.readlink(drv_link)) == "thunderbolt-net":
                return 15000
    except OSError:
        pass

    # Default path: read the kernel-reported speed.
    try:
        with open(f"{base}/speed") as f:
            speed = int(f.read().strip())
        return max(0, speed)  # -1 means "unknown" on virtio sometimes
    except (OSError, ValueError):
        return 0


def bucket_speed(mbps: int) -> int:
    """Round to a coarse bucket so jitter doesn't perturb the fold.
    Buckets cover the realistic NIC tiers we see in the field, with
    a 15 Gbps step specifically for the Linux `thunderbolt-net`
    ceiling (12-17 Gbps unidir across documented platforms). Without
    that bucket, a TB link would round up to 25000 and oversell the
    actual achievable throughput.
    Unknown (0) stays 0."""
    if mbps <= 0:
        return 0
    for b in (1000, 2500, 10000, 15000, 25000, 40000, 100000):
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
    # Consecutive-outliers gate before we let the filter relent and
    # accept a new value (see _update_neighbour_rtt). Resets to 0 on
    # the first non-outlier or on the 4th consecutive outlier.
    rtt_outlier_streak: int = 0
    # Blip telemetry — every rejected sample (even ones we threw out
    # of the EWMA) is still a signal that something on this path
    # wasn't perfect. Cluster of blips on the same peer-link is the
    # earliest warning an operator gets that a cable, a switch port,
    # or a kernel buffer is starting to misbehave. Surfaced on the
    # daemon's status line + rate-limited journal warning.
    rtt_blip_total:  int   = 0   # cumulative since daemon start
    rtt_last_blip_us: int  = 0   # most-recent rejected sample (µs)
    rtt_last_blip_at: float = 0.0  # wallclock timestamp
    rtt_last_blip_log_at: float = 0.0  # last journal emit (rate-limit)
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
    # Set of peer node-names ever observed during this run. Survives
    # `sweep_hysteresis` dropping silent neighbours from `neighbours`.
    # Used by vm_failover quorum checks (see lib/cluster_daemon.py for election).
    ever_seen_peers: set = field(default_factory=set)
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
    # Routing-advertisement state (protocol 3 — TCP per adjacency).
    # session_table — last full route table received per live TCP session
    #   keyed by (peer_node, peer_nic, my_nic).
    # mesh_sessions / mesh_listeners / mesh_connecting — see mesh_tcp.py.
    # best_transit_paths — merged from all session_table entries.
    rib_lock: threading.Lock = field(default_factory=threading.Lock)
    route_adv_seq: int = 0
    session_table: dict = field(default_factory=dict)
    mesh_sessions: dict = field(default_factory=dict)
    mesh_connecting: set = field(default_factory=set)
    mesh_listeners: dict = field(default_factory=dict)
    best_transit_paths: dict = field(default_factory=dict)
    # Last vote-config epoch THIS node advertised as applied (node_applied_epoch_set).
    # Cached so the 4 Hz tick only writes rqlite when the epoch actually advances —
    # an unconditional write every tick would re-create the L57 storm. -1 = unknown
    # (forces a first advertise on startup so a fresh node publishes its epoch).
    applied_epoch_cache: int = -1
    # L2 switch/router neighbour discovery (LLDP, CDP, MNDP). Per-NIC
    # raw sockets are opened on NIC bringup and closed on teardown.
    # switch_neighbors is keyed by (my_nic, protocol) → dict, where
    # the dict carries {chassis_id, system_name, port_id, ...,
    # first_seen, last_seen, last_logged_at}. mndp_sock is single,
    # shared across all NICs (broadcast UDP socket with IP_PKTINFO).
    lldp_socks: dict = field(default_factory=dict)
    cdp_socks: dict  = field(default_factory=dict)
    mndp_sock: Optional[socket.socket] = None
    switch_neighbors: dict = field(default_factory=dict)
    # Back-reference to the unified BedrockState (set in run_daemon when running
    # inside bedrock-d). Reserved for future cross-layer use. 
    shared_state: Optional[Any] = None
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
    # falls back to "default outgoing interface" — which cross-
    # attributes probes between mesh planes.
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
    # state.json can be a 0-byte/corrupt truncation after a power-loss in
    # save()'s rename window — a bare json.loads() here would crash the
    # whole netd thread (observed sim-4 2026-05-29). Load defensively and
    # self-heal this node's identity from the surviving cluster state before
    # giving up, so a lost state.json doesn't brick consensus.
    try:
        from . import state as _state
    except ImportError:                      # running outside the package
        import state as _state               # type: ignore
    state = _state.load_or_recover()
    cluster_uuid = state.get("cluster_uuid") or ""
    my_node = state.get("node_name") or os.uname().nodename
    if not cluster_uuid:
        raise RuntimeError("state.json has no cluster_uuid — not in a cluster "
                           "(cluster.json self-heal found none either)")
    my_loopback = state.get("loopback_ip") or ""
    return cluster_key, cluster_uuid, my_node, my_loopback


def ensure_routing_sysctls() -> None:
    """Apply routing-layer sysctls that the bedrock-net design relies on.

    Idempotent (writing the desired value to /proc/sys/... is a no-op
    when already set). Skipped silently if /proc is read-only (e.g.
    inside an unprivileged container during tests) — the daemon would
    fail elsewhere first in that case.

    Sysctls applied:

    * `net.ipv4.fib_multipath_hash_policy=1` — kernel hashes flows
      across ECMP nexthops using the L4 5-tuple (src/dst IP +
      src/dst port + protocol). Without this, the kernel uses L3
      hashing only, which would pin every flow with the same
      (src_IP, dst_IP) to the same nexthop. For Bedrock's pattern
      of "few clients, many flows" (e.g. DRBD's connection-per-
      path machinery hitting peer.loopback), L4 hashing actually
      distributes traffic across cables.

    * `net.ipv4.conf.all.arp_ignore=1` — only reply to ARP for an
      IP if that IP is configured on the RECEIVING interface. The
      default (0) replies from any NIC, which is fatal for mesh
      nodes with multiple NICs all carrying `169.254.0.0/16`
      link-local addresses. Without this, peer A's ARP for "who has
      169.254.X.Y" on enp3s0 gets replied from peer B's enp3s0,
      enp4s0, enp5s0 — each reply carries a DIFFERENT MAC, and the
      asker's neighbour cache fills with wrong-MAC entries pointing
      to the wrong bridge. Result: traffic sent to peer-loopback
      exits the wrong physical NIC and is dropped at the wrong
      bridge. See lesson_mesh_loopback_asymmetric_routes.md.

    * `net.ipv4.conf.all.arp_announce=2` — when sending an ARP
      request, use the BEST local source address matching the
      target (preferring an address on the outgoing interface).
      Pairs with arp_ignore=1 to keep ARP exchanges symmetric per
      physical wire. ``default`` is set too so newly created NICs
      pick up the same policy automatically.
    """
    knobs = {
        "/proc/sys/net/ipv4/fib_multipath_hash_policy": "1",
        "/proc/sys/net/ipv4/conf/all/arp_ignore":      "1",
        "/proc/sys/net/ipv4/conf/default/arp_ignore":  "1",
        "/proc/sys/net/ipv4/conf/all/arp_announce":    "2",
        "/proc/sys/net/ipv4/conf/default/arp_announce":"2",
        # rp_filter=2 ("loose mode"): accept incoming packets if their
        # source IP has ANY route on this host, regardless of which NIC
        # the kernel would have chosen as the outgoing path. The strict
        # default (1) drops the asymmetric mesh probes that come in on
        # enp<N> when sim-1's route to that src would have exited a
        # different enp<M>. ARP itself is unaffected (it's raw-socket
        # below the routing layer); only L3 traffic (ICMP, UDP, TCP)
        # depends on this knob. See lesson_mesh_loopback_asymmetric_routes.
        "/proc/sys/net/ipv4/conf/all/rp_filter":       "2",
        "/proc/sys/net/ipv4/conf/default/rp_filter":   "2",
    }
    for path, value in knobs.items():
        try:
            current = Path(path).read_text().strip()
            if current != value:
                Path(path).write_text(value)
        except OSError as e:
            sys.stderr.write(
                f"bedrock-net: could not set {path}={value}: {e}\n"
            )


def ensure_loopback_ip(loopback_ip: str) -> None:
    """Idempotent. Add the cluster identity IP as a /32 on `lo`, AND
    drop any stale 100.X.Y.Z/32 from a prior cluster_uuid. Survives
    reboots when state.json is read on daemon start.

    The 100.64.0.0/10 carrier-grade NAT space is reserved per RFC 6598
    for cluster loopbacks (`cluster_addr.cluster_loopback_net`). Any
    /32 in that range on `lo` that isn't our current identity is a
    residual from a wiped/reinstalled cluster and would confuse mesh
    routing — drop it.
    """
    if not loopback_ip:
        return
    cidr = f"{loopback_ip}/32"
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "lo"],
        capture_output=True, text=True,
    ).stdout
    have_self = False
    for line in out.splitlines():
        # Each line is like: "1: lo    inet 100.86.181.1/32 scope global lo \\..."
        parts = line.split()
        for i, tok in enumerate(parts):
            if tok == "inet" and i + 1 < len(parts):
                addr_cidr = parts[i + 1]
                if addr_cidr == cidr:
                    have_self = True
                elif addr_cidr.startswith("100.") and addr_cidr.endswith("/32"):
                    # Stale loopback from a prior cluster_uuid.
                    subprocess.run(
                        ["ip", "addr", "del", addr_cidr, "dev", "lo"],
                        capture_output=True, text=True,
                    )
    if not have_self:
        subprocess.run(["ip", "addr", "add", cidr, "dev", "lo"],
                       capture_output=True, text=True)






def _run_silent_capture(cmd: list[str]) -> tuple[int, str, str]:
    """Helper: capture stdout/stderr of a subprocess without raising."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout or "", r.stderr or ""





def run_daemon(shared_state=None):
    """Main netd loop. If `shared_state` is a state_shared.BedrockState
    instance, the constructed Daemon is also attached as `shared_state.netd`
    so other in-process readers (FastAPI handlers) can see live netd state
    without going through /run/bedrock/*.json files.

    Also honors `shared_state.stop_event` for clean shutdown when running
    inside the unified `bedrock-d` process. Without a shared_state arg the
    function crashes on missing state and relies on systemd
    Restart=on-failure, as the standalone `bedrock-net` systemd unit does."""
    # bedrock-d starts BEFORE `bedrock init`/`bedrock join` has run — so
    # cluster.key + state.json don't exist yet. The standalone
    # bedrock-net.service relies on systemd Restart=on-failure to retry
    # after init wrote cluster.key; inside bedrock-d we wait in-process
    # instead — raising here would kill the netd thread and leave it dead,
    # blocking the very init flow that would have unblocked us.
    while True:
        try:
            cluster_key, cluster_uuid, my_node, my_loopback = load_state()
            break
        except RuntimeError as e:
            if shared_state is None:
                # Standalone path — crash and let systemd restart us.
                raise
            sys.stderr.write(
                f"bedrock-net: waiting for cluster bootstrap: {e}\n"
            )
            if shared_state.stop_event.wait(2.0):
                return  # shutdown requested while waiting
    if not my_loopback:
        # Fall back to local rqlite (works without quorum) for our own
        # loopback assignment if state.json hasn't been refreshed yet.
        try:
            try:
                from . import rqlite_client as _rc_mod
            except ImportError:
                import sys as _sys2
                _sys2.path.insert(0, "/usr/local/lib/bedrock")
                from lib import rqlite_client as _rc_mod  # type: ignore
            with _rc_mod.RqliteClient() as _rc:
                row = _rc.query_one(
                    "SELECT loopback_ip FROM nodes WHERE node_name = ?",
                    params=[my_node], level="none",
                )
            my_loopback = (row or {}).get("loopback_ip", "")
        except Exception:
            pass
    if not my_loopback:
        sys.stderr.write("bedrock-net: no loopback_ip yet (cluster.json/state.json incomplete); will retry every tick\n")

    if my_loopback:
        ensure_loopback_ip(my_loopback)

    ensure_routing_sysctls()

    d = Daemon(
        cluster_key=cluster_key,
        cluster_uuid=cluster_uuid,
        my_node=my_node,
        my_loopback=my_loopback,
    )

    # Publish the Daemon onto the shared state object so the unified
    # bedrock-d process can answer dashboard queries without re-reading
    # /run/bedrock/*.json files. In standalone mode (shared_state=None)
    # state lives only in this stack frame.
    if shared_state is not None:
        with shared_state.netd_lock:
            shared_state.netd = d
            shared_state.self_node_name = my_node
            shared_state.self_loopback_ip = my_loopback
            shared_state.cluster_uuid = cluster_uuid
        d.shared_state = shared_state

    d.recv_sock = open_recv_socket()
    d.recv_sock.settimeout(0.05)
    try:
        d.mndp_sock = l2disc.open_mndp_socket()
    except OSError as e:
        sys.stderr.write(
            f"bedrock-net: mndp socket open failed: {e}; "
            f"MikroTik neighbour discovery disabled.\n"
        )
        d.mndp_sock = None

    print(f"bedrock-net: cluster_uuid={cluster_uuid} node={my_node} "
          f"loopback={my_loopback or '<not yet assigned>'}",
          file=sys.stderr, flush=True)

    last_probe = 0.0
    last_route_emit = 0.0
    last_icmp = 0.0
    last_status = 0.0
    last_switch_state = 0.0

    def _should_stop() -> bool:
        if d.stopped:
            return True
        if shared_state is not None and shared_state.stop_event.is_set():
            return True
        return False

    while not _should_stop():
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
            # Protocol 3: TCP routing per adjacency (mesh_tcp).
            mesh_tcp.reconcile_listeners(d)
            mesh_tcp.route_sessions_push(d, now)
            recompute_best_transit_paths(d, now)
            # L2 neighbour discovery — passive, drain every tick.
            l2disc_drain(d, now)
            if now - last_switch_state >= SWITCH_STATE_INTERVAL_S:
                write_switch_state_file(d)
                write_mesh_state_file(d)
                last_switch_state = now
            if now - last_route_emit >= ROUTE_EMIT_INTERVAL_S:
                emit_routes(d)
                last_route_emit = now
            if now - last_status >= 30.0:
                logged = sum(1 for n in d.neighbours.values() if n.logged_up)
                sessions = [
                    f"{k[0]}/{k[2]}({len((d.session_table.get(k) or {}).get('paths', []))}p)"
                    for k in d.mesh_sessions
                ]
                transit = list(d.best_transit_paths.keys())
                # Latency blips — non-fatal but observable.
                blips_total = sum(n.rtt_blip_total for n in d.neighbours.values())
                recent_blip = max(
                    (n for n in d.neighbours.values() if n.rtt_last_blip_at > 0),
                    key=lambda n: n.rtt_last_blip_at,
                    default=None,
                )
                if blips_total and recent_blip:
                    age_s = max(0, int(now - recent_blip.rtt_last_blip_at))
                    blip_str = (
                        f"blips_total={blips_total} "
                        f"last={recent_blip.rtt_last_blip_us}us@{age_s}s_ago"
                        f"({recent_blip.peer_node}/{recent_blip.my_nic})"
                    )
                else:
                    blip_str = "blips_total=0"
                # Switch / router neighbours by NIC.
                sw_parts: list[str] = []
                for (nic, proto), entry in sorted(d.switch_neighbors.items()):
                    name = entry.get("system_name") or entry.get("chassis_id", "")
                    port = entry.get("port_id", "")
                    short = f"{proto}:{nic}->{name}"
                    if port:
                        short += f"/{port}"
                    sw_parts.append(short)
                sw_str = (f"switches={len(sw_parts)}"
                          + (f"({','.join(sw_parts)})" if sw_parts else ""))
                print(
                    f"bedrock-net: status neighbours={len(d.neighbours)} "
                    f"(logged_up={logged}); mesh_tcp=[{','.join(sessions) or '-'}]; "
                    f"transit_dests=[{','.join(transit) or '-'}]; "
                    f"{blip_str}; {sw_str}",
                    file=sys.stderr, flush=True,
                )
                last_status = now
        except KeyboardInterrupt:
            break
        except Exception as e:
            sys.stderr.write(f"bedrock-net: tick error: {e!r}\n")
        time.sleep(TICK_INTERVAL)

    mesh_tcp.stop_all(d)


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
            try:
                from . import rqlite_client as _rc_mod
            except ImportError:
                import sys as _sys2
                _sys2.path.insert(0, "/usr/local/lib/bedrock")
                from lib import rqlite_client as _rc_mod  # type: ignore
            with _rc_mod.RqliteClient() as _rc:
                row = _rc.query_one(
                    "SELECT loopback_ip FROM nodes WHERE node_name = ?",
                    params=[d.my_node], level="none",
                )
            d.my_loopback = (row or {}).get("loopback_ip", "")
            if d.my_loopback:
                ensure_loopback_ip(d.my_loopback)
                print(f"bedrock-net: loopback now {d.my_loopback}",
                      file=sys.stderr, flush=True)
        except Exception as e:
            # Re-runs every tick, so a transient failure self-heals — but log
            # it (don't swallow silently) so a PERSISTENT failure is visible,
            # matching the sibling refresh path.
            print(f"bedrock-net: loopback refresh failed (retry next tick): {e}",
                  file=sys.stderr, flush=True)

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
                # L2 neighbour discovery — receive-only LLDP + CDP per
                # NIC. Failures are non-fatal: a NIC without permission
                # to AF_PACKET still does discovery + ICMP + routing.
                try:
                    d.lldp_socks[nic] = l2disc.open_lldp_socket(nic)
                except OSError as e:
                    sys.stderr.write(
                        f"bedrock-net: lldp socket on {nic} failed: "
                        f"{e}; switch identity won't be observed on "
                        f"this NIC.\n"
                    )
                try:
                    d.cdp_socks[nic] = l2disc.open_cdp_socket(nic)
                except OSError as e:
                    sys.stderr.write(
                        f"bedrock-net: cdp socket on {nic} failed: "
                        f"{e}; Cisco/Aruba/HP switch identity won't "
                        f"be observed on this NIC.\n"
                    )
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
            for sock_map in (d.lldp_socks, d.cdp_socks):
                s = sock_map.pop(nic, None)
                if s is not None:
                    try: s.close()
                    except Exception: pass
            # Drop any switch_neighbors entries for this NIC.
            for k in list(d.switch_neighbors.keys()):
                if k[0] == nic:
                    d.switch_neighbors.pop(k, None)
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
        # NOTE: ever_seen_peers is updated only when logged_up flips
        # to True (sweep_hysteresis), NOT on first probe receipt. See
        # the comment there for why — counting a one-way probe sender
        # towards quorum drops every fresh-boot daemon into NoQuorum.
    else:
        n.last_seen = now
        n.peer_link_addr = peer_link_addr
        n.peer_loopback = peer_loopback

    mesh_tcp.mesh_maybe_connect(
        d, peer_node, peer_nic, my_nic, peer_loopback, peer_link_addr,
    )


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
        # yet logged → emit LINK_UP. Always mark logged_up True for
        # local routing purposes — the rqlite write is for cluster-wide
        # observability and must NOT gate local route installation
        # (that would chicken-and-egg: routes need rqlite leader, leader
        # needs reachable peer, peer needs routes).
        age_since_first = now - (n.first_seen or 0.0)
        if not n.logged_up and age_since_first >= UP_HYSTERESIS_S:
            wrote = emit_link_event("up", d, n)
            n.logged_up = True   # local-routing flag; emit is best-effort
            if wrote:
                n.last_quality_log = now
            # Count this peer towards quorum from the moment it first
            # reached logged_up. Without this gate, ever_seen_peers
            # bumped on the very first one-way probe receipt, so
            # n_nodes jumped to 2 before logged_up was True →
            # my_votes=10/11 → NoQuorum-marked before the 5 s
            # UP_HYSTERESIS_S handshake could complete. Particularly
            # bites startup right after a bedrock-d restart.
            d.ever_seen_peers.add(n.peer_node)
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

# Protocol 3: TCP routing sync per direct adjacency (mesh_tcp.py).
# One session per link (peer_node, peer_nic, my_nic); hello triggers
# dial (higher loopback → lower link-local); keepalive-only liveness.
ADV_PORT       = 7733          # TCP on link-local (was UDP unicast)
ADV_VERSION    = 1


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


BLIP_LOG_RATE_LIMIT_S = 300.0   # one journal line per (peer, my_nic) per 5 min


def _update_neighbour_rtt(d: Daemon, neigh_key: tuple,
                           sample_us: float) -> None:
    """TCP RFC 6298 EWMA on the per-neighbour RTT, with outlier
    rejection BEFORE smoothing so a transient 230 ms hiccup on a
    100 µs link doesn't poison the smoothed value or kick off route
    reshuffling.

    Outliers ARE counted, even when they're thrown out of the EWMA
    — they're a real signal that something on this path isn't
    perfect. A cluster of blips on the same (peer, my_nic) is the
    earliest warning an operator gets that a cable, a switch port,
    or a kernel buffer is starting to misbehave. Per-neighbour
    totals appear on the daemon's 30 s status line; each blip also
    prints a structured journal line that each node's VLagent
    forwards to both redundant VictoriaLogs backends, rate-limited
    to one emit per (peer, my_nic) per 5 min so a flapping path
    can't flood the log server. Operators query

        _msg:BLIP peer:bedrock-X | stats by (my_nic) count()

    in LogsQL to see how often a specific link is misbehaving.
    See docs/06-mesh-network.md §protocol 2."""
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

    outlier_rule = None
    if sample_us > srtt + 4 * rttvar:
        outlier_rule = "variance"
    elif srtt > 100 and sample_us > 10 * srtt:
        outlier_rule = "multiplicative"
    elif srtt < 5_000 and sample_us > RTT_OUTLIER_ABS_US:
        outlier_rule = "absolute"

    if outlier_rule is not None and n.rtt_outlier_streak < 3:
        # Reject the sample, but record the blip. The streak gate
        # ensures three consecutive outliers (~6 s) ⇒ genuine
        # degradation, which the next iteration falls through to
        # the EWMA update path below.
        n.rtt_outlier_streak += 1
        n.rtt_blip_total += 1
        n.rtt_last_blip_us = int(sample_us)
        now = time.time()
        n.rtt_last_blip_at = now
        if now - n.rtt_last_blip_log_at >= BLIP_LOG_RATE_LIMIT_S:
            sys.stderr.write(
                f"bedrock-net: BLIP "
                f"peer={n.peer_node} my_nic={n.my_nic} "
                f"sample_us={int(sample_us)} srtt_us={int(srtt)} "
                f"rule={outlier_rule} streak={n.rtt_outlier_streak} "
                f"total={n.rtt_blip_total}\n"
            )
            sys.stderr.flush()
            n.rtt_last_blip_log_at = now
        return

    # Either not an outlier, or 3+ consecutive outliers (real degrade).
    n.rtt_var_us = int((1 - RTT_BETA) * rttvar
                       + RTT_BETA * abs(sample_us - srtt))
    n.rtt_us     = int((1 - RTT_ALPHA) * srtt + RTT_ALPHA * sample_us)
    n.rtt_outlier_streak = 0


# ── Routing advertisement (protocol 3 of three) ──────────────────────
#
# Path-vector advertisement, BGP-shaped: every 2 s each node sends ONE
# signed UDP unicast per peer (regardless of how many physical NICs
# connect us) listing the destinations it currently believes it can
# reach. Each path carries its full via_chain (loop prevention),
# bottleneck bandwidth (min over the path), and cumulative latency
# (sum over the path). Receivers compose `bw = min(adv_bw, my_bw_to_adv)`
# and `lat = adv_lat + my_rtt_to_adv`, rank candidates per destination
# by local_metric, and install the lowest-metric next-hop as a /32.
#
# Invariants enforced here:
#   * one advertisement per peer per cycle, kernel picks the NIC
#   * advertiser MUST be a direct logged-up neighbour of the receiver
#     (transit-borne advertisements are dropped — would invite spoof)
#   * via_chain[0] MUST == advertiser
#   * receiver's own node name in via_chain ⇒ drop (loop)
#   * stale advertisements (> ADV_STALE_S) are withdrawn implicitly
#     by recompute_best_transit_paths skipping them


def _direct_neighbour_by_node(d: Daemon) -> dict:
    """For each peer_node we have at least one logged_up neighbour
    entry for, return the BEST (lowest local_metric) Neighbour record.
    Used both for sending advertisements (we send to peer_loopback,
    kernel picks NIC) and for composing transit metrics (rtt_us +
    bw_to_advertiser).
    """
    best: dict[str, Neighbour] = {}
    best_metric: dict[str, int] = {}
    for n in d.neighbours.values():
        if not n.logged_up or not n.peer_loopback:
            continue
        bw = bucket_speed(nic_speed_mbps(n.my_nic)) or max(int(n.speed_mbps), 1)
        lat = int(n.rtt_us)
        m = local_metric(bw_mbps=bw, latency_us=lat)
        prev = best_metric.get(n.peer_node)
        if prev is None or m < prev:
            best[n.peer_node] = n
            best_metric[n.peer_node] = m
    return best


def _cluster_node_loopbacks(my_node: str) -> dict:
    """nodes -> loopback_ip mapping (best-effort). Used to know who to address
    advertisements/heartbeats to. Mesh routing decisions themselves never depend
    on this — membership is membership-of-record, not routing-of-record. Returns
    {} on any error.

    Reads via the REVISION-CACHED load_cluster (level='none'), NOT a fresh
    `SELECT ... FROM nodes` — this is called a few times per second and the nodes
    table never changes at idle, so the direct SELECT was pure rqlite load (RCA:
    ~3 uncached q/s into rqlited). load_cluster does one cheap revision read and
    returns the SHARED cached snapshot that the 4 Hz election tick already
    populated this tick — so on a cache hit there's no nodes scan + no rebuild."""
    try:
        try:
            from . import cluster_state as _cs
        except ImportError:
            import sys as _sys2
            _sys2.path.insert(0, "/usr/local/lib/bedrock")
            from lib import cluster_state as _cs  # type: ignore
        nodes = _cs.load_cluster(level="none").get("nodes") or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for nm, info in nodes.items():
        if nm == my_node:
            continue
        lo = (info or {}).get("loopback_ip") or ""
        if lo:
            out[nm] = lo
    return out


def build_advertisement_paths(d: Daemon) -> list:
    """Construct the paths[] list for an outgoing advertisement.
    Includes:
      * one entry per direct logged-up peer (via_chain=[me, peer])
      * one entry per transit destination we've selected as best
        (via_chain=[me, ...adv.via_chain]), so neighbours can
        propagate our learned paths through to further peers
    A destination reachable both directly and through transit is
    advertised only as direct — direct beats transit by definition."""
    paths: list[dict] = []
    direct = _direct_neighbour_by_node(d)

    for peer, n in direct.items():
        bw  = bucket_speed(nic_speed_mbps(n.my_nic)) or max(int(n.speed_mbps), 1)
        lat = int(n.rtt_us)
        paths.append({
            "dest":                 peer,
            "via_chain":            [d.my_node, peer],
            "bottleneck_bw_mbps":   int(bw),
            "cumulative_latency_us": int(lat),
        })

    for dest, sel in d.best_transit_paths.items():
        if dest == d.my_node:
            continue
        if dest in direct:
            continue
        paths.append({
            "dest":                 dest,
            "via_chain":            [d.my_node] + list(sel["via_chain"]),
            "bottleneck_bw_mbps":   int(sel["bw"]),
            "cumulative_latency_us": int(sel["lat"]),
        })

    # The mgmt master originates the cluster VIP (.254) as a connected
    # /32, folded into this same packet. Gating on the local single-writer
    # role (i_am_mgmt_master) is self-knowledge, not a remote lookup, so it
    # doesn't reintroduce the master dependency on the receiver side. On
    # demote the entry simply stops being emitted → ages out cluster-wide
    # (ADV_STALE_S) → the /32 is withdrawn → the new host re-originates it.
    if i_am_mgmt_master(d):
        paths.append({
            "dest":                 VIP_DEST_SENTINEL,
            "via_chain":            [d.my_node],
            "bottleneck_bw_mbps":   VIP_ADV_BW_MBPS,
            "cumulative_latency_us": 0,
        })
    return paths



def recompute_best_transit_paths(d: Daemon, now_ts: float) -> None:
    """For every destination advertised on a live TCP adjacency, pick
    the lowest-local-metric path through that session's direct link.

    Path-vector semantics:
      * one session_table entry per (peer_node, peer_nic, my_nic)
      * each path's via_chain[0] must == peer_node (the advertiser)
      * each path's via_chain must not contain my_node
      * composed metric: bw=min(adv_bw, my_bw_to_adv);
                          lat=adv_lat + my_rtt_to_adv
    """
    best: dict[str, dict] = {}

    with d.rib_lock:
        entries = list(d.session_table.items())

    for session_key, adv in entries:
        peer_node, peer_nic, my_nic = session_key
        nb = d.neighbours.get(session_key)
        if nb is None:
            continue
        advertiser = peer_node
        bw_to_adv  = bucket_speed(nic_speed_mbps(nb.my_nic)) or max(int(nb.speed_mbps), 1)
        lat_to_adv = int(nb.rtt_us)

        for p in adv.get("paths", []):
            try:
                dest      = p["dest"]
                via_chain = list(p["via_chain"])
                p_bw      = int(p["bottleneck_bw_mbps"])
                p_lat     = int(p["cumulative_latency_us"])
            except (KeyError, TypeError, ValueError):
                continue
            if not via_chain or via_chain[0] != advertiser:
                continue
            if d.my_node in via_chain:
                continue
            if dest == d.my_node:
                continue

            bw  = min(p_bw, bw_to_adv) if p_bw > 0 else bw_to_adv
            lat = p_lat + lat_to_adv
            m   = local_metric(bw_mbps=bw, latency_us=lat)

            cur = best.get(dest)
            if (cur is None
                    or m < cur["metric"]
                    or (m == cur["metric"] and lat < cur["lat"])
                    or (m == cur["metric"] and lat == cur["lat"]
                        and advertiser < cur["advertiser"])):
                best[dest] = {
                    "metric":     m,
                    "advertiser": advertiser,
                    "neighbour":  nb,
                    "bw":         bw,
                    "lat":        lat,
                    "via_chain":  via_chain,
                }

    d.best_transit_paths = best


# ── L2 neighbour discovery (LLDP + CDP + MNDP) ───────────────────────
#
# Sidecar feature, not part of the routing protocols 1/2/3. Purely
# observational: each tick we drain whatever LLDP / CDP / MNDP frames
# the kernel queued for us, decode them (l2disc.decode_*), and update
# Daemon.switch_neighbors. First-seen and switch-swap events emit a
# structured 'NIC_SWITCH' journal line that the per-node VLagent
# forwards to the cluster's two redundant VictoriaLogs backends.
#
# The live per-node view is also written to
# /run/bedrock/switch_neighbors.json so the mgmt master can scrape
# it cheaply and the dashboard can render "node X enp3s0 → switch S
# port 5" without any cluster log involvement (single-writer rule
# stays clean: this is per-node local reality, not consensus state).

SWITCH_REFRESH_S = 24 * 3600        # re-emit NIC_SWITCH at least daily
SWITCH_DRAIN_MAX = 100              # frames per socket per tick
SWITCH_STATE_FILE = Path("/run/bedrock/switch_neighbors.json")
SWITCH_STATE_INTERVAL_S = 5.0

# Same per-node-local-state-file pattern, but for protocol-1
# (signed multicast discovery) observations — every directly-cabled
# peer this node sees on every NIC. The dashboard scrapes this from
# each node to build the cluster-side of the topology diagram.
MESH_STATE_FILE = Path("/run/bedrock/mesh_neighbors.json")


def l2disc_drain(d: Daemon, now_ts: float) -> bool:
    """Drain LLDP + CDP per-NIC sockets and the shared MNDP socket.
    Returns True if any (my_nic, protocol) entry was inserted or
    swapped to a new chassis_id."""
    changed = False
    for nic, sock in list(d.lldp_socks.items()):
        for _ in range(SWITCH_DRAIN_MAX):
            try:
                buf = sock.recv(2048)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                break
            info = l2disc.decode_lldp(buf)
            if info and _record_switch(d, nic, info, now_ts):
                changed = True
    for nic, sock in list(d.cdp_socks.items()):
        for _ in range(SWITCH_DRAIN_MAX):
            try:
                buf = sock.recv(2048)
            except (BlockingIOError, socket.timeout):
                break
            except OSError:
                break
            info = l2disc.decode_cdp(buf)
            if info and _record_switch(d, nic, info, now_ts):
                changed = True
    if d.mndp_sock is not None:
        for _ in range(SWITCH_DRAIN_MAX):
            data, src_addr, ifindex = recv_with_ifindex(d.mndp_sock)
            if data is None:
                break
            nic = ifname_for_index(ifindex) if ifindex else ""
            if not nic:
                continue
            info = l2disc.decode_mndp(data)
            if not info:
                continue
            if src_addr and "mgmt_ip" not in info:
                info["mgmt_ip"] = src_addr
            if _record_switch(d, nic, info, now_ts):
                changed = True
    return changed


def _record_switch(d: Daemon, nic: str, info: dict, now_ts: float) -> bool:
    """Insert/update the entry for (nic, protocol). Emit a journal
    line if this is a first-observation, a chassis_id swap (cable
    moved / switch replaced), or if 24 h has elapsed since the last
    emit for this (nic, protocol)."""
    protocol = info.get("protocol", "")
    chassis_id = info.get("chassis_id", "")
    if not chassis_id or not protocol:
        return False
    key = (nic, protocol)
    prev = d.switch_neighbors.get(key)
    is_new  = prev is None
    is_swap = (prev is not None
               and prev.get("chassis_id") != chassis_id)
    is_refresh = (prev is not None and not is_swap
                  and now_ts - prev.get("last_logged_at", 0)
                       >= SWITCH_REFRESH_S)

    entry = dict(info)
    if prev and not is_swap:
        entry["first_seen"] = prev.get("first_seen", now_ts)
    else:
        entry["first_seen"] = now_ts
    entry["last_seen"] = now_ts
    if is_new or is_swap or is_refresh:
        entry["last_logged_at"] = now_ts
        _emit_nic_switch_log(
            nic, entry,
            reason=("new" if is_new
                    else "swap" if is_swap
                    else "refresh"),
        )
    else:
        entry["last_logged_at"] = prev.get("last_logged_at", 0)
    d.switch_neighbors[key] = entry
    return is_new or is_swap


def _emit_nic_switch_log(nic: str, entry: dict, *, reason: str) -> None:
    """Structured key=value journal line. VLagent forwards it to
    both VictoriaLogs backends; LogsQL operators query e.g.

        _msg:NIC_SWITCH chassis:"00:1f:2e:aa:bb:cc"
            | stats by (my_nic, port) count()
    """
    fields = [
        f"my_nic={nic}",
        f"protocol={entry.get('protocol', '')}",
        f"chassis={entry.get('chassis_id', '')}",
    ]
    for src, label in (("system_name", "system"),
                        ("port_id",     "port"),
                        ("port_descr",  "port_descr"),
                        ("mgmt_ip",     "mgmt"),
                        ("platform",    "platform"),
                        ("ttl_s",       "ttl_s")):
        v = entry.get(src)
        if v not in (None, ""):
            # Wrap values containing spaces in quotes for clean LogsQL.
            sval = str(v)
            if " " in sval:
                sval = '"' + sval.replace('"', '\\"') + '"'
            fields.append(f"{label}={sval}")
    fields.append(f"reason={reason}")
    sys.stderr.write("bedrock-net: NIC_SWITCH " + " ".join(fields) + "\n")
    sys.stderr.flush()


def write_switch_state_file(d: Daemon) -> None:
    """Atomic write of the current per-NIC switch view to
    /run/bedrock/switch_neighbors.json. Per-node local file —
    NOT replicated, NOT folded into rqlite. The mgmt master
    scrapes one of these per node to build an in-memory rollup
    for the dashboard (or a /run/bedrock/physical_topology.json
    cache on the master, if convenient). rqlite stays
    consensus-only.

    Shape:
      { "enp2s0": { "lldp": {chassis_id, system_name, port_id, …},
                     "cdp":  {…} },
        "enp3s0": { "mndp": {…} },
        … }
    """
    grouped: dict = {}
    for (nic, protocol), entry in d.switch_neighbors.items():
        # Strip our internal bookkeeping field; everything else is
        # the protocol payload + first_seen/last_seen.
        view = {k: v for k, v in entry.items() if k != "last_logged_at"}
        grouped.setdefault(nic, {})[protocol] = view
    try:
        SWITCH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SWITCH_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(grouped, indent=2, sort_keys=True) + "\n")
        tmp.replace(SWITCH_STATE_FILE)
    except OSError as e:
        sys.stderr.write(
            f"bedrock-net: switch state file write failed: {e}\n"
        )


def write_mesh_state_file(d: Daemon) -> None:
    """Atomic write of the current per-NIC view of directly-cabled
    cluster peers (protocol-1 observations) to
    /run/bedrock/mesh_neighbors.json. Per-node local file — same
    lifecycle / non-replicated / not-folded-into-rqlite story
    as switch_neighbors.json. mgmt master scrapes this so the
    dashboard topology view shows node-to-node cables as well as
    node-to-switch.

    Shape:
      { 'me': 'bedrock-X',
        'nics': {
          'enp2s0': {
            'addr': '169.254.165.122',
            'speed_mbps': 10000,
            'neighbours': [
              {'peer_node': 'bedrock-Y', 'peer_nic': 'enp2s0',
               'peer_link_addr': '169.254.49.209',
               'peer_loopback': '100.104.109.2',
               'rtt_us': 145, 'rtt_var_us': 22,
               'first_seen': ..., 'last_seen': ...,
               'logged_up': true, 'blip_total': 0},
              ...
            ]
          }, ...
        }
      }
    """
    nics_view: dict = {}
    for nic, addr in d.nic_addrs.items():
        nics_view[nic] = {
            "addr": addr,
            "speed_mbps": bucket_speed(nic_speed_mbps(nic)),
            "neighbours": [],
        }
    for n in d.neighbours.values():
        if n.my_nic not in nics_view:
            nics_view[n.my_nic] = {
                "addr": d.nic_addrs.get(n.my_nic, ""),
                "speed_mbps": bucket_speed(nic_speed_mbps(n.my_nic)),
                "neighbours": [],
            }
        nics_view[n.my_nic]["neighbours"].append({
            "peer_node":      n.peer_node,
            "peer_nic":       n.peer_nic,
            "peer_link_addr": n.peer_link_addr,
            "peer_loopback":  n.peer_loopback,
            "rtt_us":         int(n.rtt_us),
            "rtt_var_us":     int(n.rtt_var_us),
            "blip_total":     int(n.rtt_blip_total),
            "first_seen":     float(n.first_seen),
            "last_seen":      float(n.last_seen),
            "logged_up":      bool(n.logged_up),
        })
    # Sort neighbour lists for stable output.
    for v in nics_view.values():
        v["neighbours"].sort(
            key=lambda r: (r["peer_node"], r["peer_nic"]))

    out = {"me": d.my_node, "nics": nics_view}
    try:
        MESH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MESH_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, indent=2, sort_keys=True))
        tmp.replace(MESH_STATE_FILE)
    except OSError as e:
        sys.stderr.write(
            f"bedrock-net: mesh state file write failed: {e}\n"
        )


# ── Local metric (per-receiver, format-decoupled) ────────────────────

def local_metric(bw_mbps: int, latency_us: int,
                 loss_rate: float = 0.0, age_s: float = 1e9) -> int:
    """EIGRP-style composite metric, weights tuned for modern speeds.

    bandwidth term: 1_000_000 / Mbps      → 12 at 80G, 100 at 10G,
                                             400 at 2.5G, 1000 at 1G
    latency term:   max(0, us-1000) / 100 → 0 below 1 ms (LAN noise
                                             floor), then 1 per 100 µs
                                             above. Sub-ms is noise on
                                             a healthy LAN; bandwidth
                                             should dominate at local
                                             scale.
    flap penalty:   +50 if up_since < 60 s (additive, predictable —
                                             not a multiplier)
    loss penalty:   +500 × min(1, loss×20) → graded, not binary

    See docs/06-mesh-network.md §protocol 3.
    """
    bw_cost  = 1_000_000 / max(int(bw_mbps), 1)
    lat_cost = max(0, int(latency_us) - 1000) / 100   # floor below 1 ms
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
    """Persist a LINK_UP / LINK_DOWN / LINK_QUALITY observation by
    writing/updating the corresponding row in rqlite's `paths` table.
    Only the mgmt master writes (single-writer discipline); followers
    return True immediately so the hysteresis state machine still records
    "logged" locally and doesn't keep retrying. The cluster-wide path
    table is populated by the master observing its own paths; followers'
    own paths reach the `paths` table via the master's reciprocal
    observation. Returns True on success or follower-skip, False on
    rqlite error (caller retries next sweep).
    """
    if not i_am_mgmt_master(d):
        return True  # follower: don't write, but mark as logged
    try:
        from . import bedrock_state as bs
    except ImportError:
        sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import bedrock_state as bs  # type: ignore

    ts = time.time()
    my_link_addr = d.nic_addrs.get(n.my_nic, "")
    try:
        if kind == "up":
            rev = bs.link_up(
                node_a=d.my_node, nic_a=n.my_nic,
                node_b=n.peer_node, nic_b=n.peer_nic,
                link_addr_a=my_link_addr, link_addr_b=n.peer_link_addr,
                speed_mbps=n.speed_mbps, rtt_us=n.rtt_us,
                observed_at=ts,
            )
        elif kind == "down":
            rev = bs.link_down(
                node_a=d.my_node, nic_a=n.my_nic,
                node_b=n.peer_node, nic_b=n.peer_nic,
                reason=reason or "hysteresis",
                observed_at=ts,
            )
        elif kind == "quality":
            rev = bs.link_quality(
                node_a=d.my_node, nic_a=n.my_nic,
                node_b=n.peer_node, nic_b=n.peer_nic,
                link_addr_a=my_link_addr, link_addr_b=n.peer_link_addr,
                speed_mbps=n.speed_mbps, rtt_us=n.rtt_us,
                observed_at=ts,
            )
        else:
            return False
        print(f"bedrock-net: {kind} {n.peer_node}.{n.peer_nic}↔{d.my_node}.{n.my_nic} rev={rev}",
              file=sys.stderr, flush=True)
        return True
    except Exception as e:
        # Transient rqlite error — caller should retry next sweep.
        sys.stderr.write(f"bedrock-net: rqlite write {kind} failed: {e!r}\n")
        return False


# ── Routing ──────────────────────────────────────────────────────────

def emit_routes(d: Daemon) -> None:
    """Compute the per-peer routes from the in-memory neighbour table
    and the protocol-3 transit paths. Update the kernel routing table
    only if the desired set differs from what we last installed.

    Strategy:
      * For every direct neighbour with logged_up=True, emit a host
        route to peer_loopback via peer_link_addr dev my_nic with the
        primary metric.
      * Add backup routes (other direct paths to the same peer) at
        increasing metrics so the kernel auto-fails-over on link-down.
      * For each non-direct destination in best_transit_paths (fed
        by protocol 3 advertisements from direct neighbours), install
        a single /32 via the chosen next-hop at METRIC_TRANSIT_BASE.
      * Add a panic route for the cluster /24 via the freshest
        neighbour at metric 999.
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

    to_del = current_set - desired_set
    to_add = desired_set - current_set
    if to_del or to_add:
        sys.stderr.write(
            f"bedrock-net: emit_routes: +{len(to_add)} -{len(to_del)} "
            f"(desired={len(desired_set)} current={len(current_set)})\n"
        )
    for cmd in to_del:
        rc = subprocess.run(["ip", "route", "del"] + cmd.split(),
                            capture_output=True, text=True).returncode
        if rc != 0:
            sys.stderr.write(f"bedrock-net: route del failed [{cmd}] rc={rc}\n")
    for cmd in to_add:
        # `replace` instead of `add` so a stale leftover doesn't make
        # the add fail.
        rc = subprocess.run(["ip", "route", "replace"] + cmd.split(),
                            capture_output=True, text=True)
        if rc.returncode != 0:
            sys.stderr.write(
                f"bedrock-net: route replace failed [{cmd}] "
                f"rc={rc.returncode} stderr={rc.stderr.strip()}\n"
            )

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
         (libvirt migration, SeaweedFS, SSH, the bedrock dashboard).
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
        # Group paths by tied cost — paths with identical
        # local_metric (after sub-ms latency floor + bucketed bandwidth)
        # get emitted as a single multipath route. Kernel hashes flows
        # across nexthops via fib_multipath_hash_policy=1 (set in
        # ensure_routing_sysctls).
        tier_groups: list[tuple[int, list]] = []
        for n in lst:
            if not n.peer_link_addr:
                continue
            cost = _path_cost(n)[0]
            if tier_groups and tier_groups[-1][0] == cost:
                tier_groups[-1][1].append(n)
            else:
                tier_groups.append((cost, [n]))
        for i, (_cost, tier_paths) in enumerate(tier_groups):
            metric = METRIC_DIRECT_BASE + i
            if len(tier_paths) == 1:
                n = tier_paths[0]
                spec = (f"{n.peer_loopback}/32 via {n.peer_link_addr} "
                        f"dev {n.my_nic} metric {metric}")
            else:
                # ECMP multipath: stable order on (peer_link_addr,
                # my_nic) so every fold writes the same spec.
                # `iproute2` requires `metric` BEFORE the `nexthop`
                # list — putting it after produces:
                #   Error: "nexthop" or end of line is expected
                #   instead of "metric"
                tier_paths_sorted = sorted(
                    tier_paths,
                    key=lambda p: (p.peer_link_addr, p.my_nic),
                )
                hops = " ".join(
                    f"nexthop via {p.peer_link_addr} dev {p.my_nic} weight 1"
                    for p in tier_paths_sorted
                )
                spec = (f"{tier_paths_sorted[0].peer_loopback}/32 "
                        f"metric {metric} {hops}")
            routes.append(spec)

    # 3. Transit /32s (protocol 3, path-vector). For each cluster peer
    #    that's NOT directly reachable but whose path we've learned
    #    from a direct neighbour's advertisement, install a single
    #    /32 via that neighbour at METRIC_TRANSIT_BASE. Direct beats
    #    transit by definition (METRIC_DIRECT_BASE < METRIC_TRANSIT_BASE,
    #    and longest-prefix-match is identical at /32 so kernel sorts
    #    by metric). The destination loopback IP comes from rqlite's
    #    nodes table — membership-of-record, never used for routing
    #    decisions themselves.
    dest_loopbacks = _cluster_node_loopbacks(d.my_node)
    transit_items = sorted(d.best_transit_paths.items(),
                            key=lambda kv: (kv[1]["metric"], kv[0]))
    for i, (dest, sel) in enumerate(transit_items):
        if dest in by_peer:
            continue   # direct already covers it
        if dest == VIP_DEST_SENTINEL:
            # The cluster VIP: address is a pure function of cluster_uuid,
            # resolved locally — never an rqlite/membership lookup. This is
            # what keeps the VIP route on the data plane only.
            from . import cluster_addr as _ca
            dest_lo = _ca.cluster_vip(d.cluster_uuid)
        else:
            dest_lo = dest_loopbacks.get(dest, "")
        if not dest_lo:
            continue   # rqlite hasn't caught up yet; retry next tick
        nb = sel["neighbour"]
        if not nb.peer_link_addr:
            continue
        spec = (f"{dest_lo}/32 via {nb.peer_link_addr} "
                f"dev {nb.my_nic} metric {METRIC_TRANSIT_BASE + i}")
        routes.append(spec)

    # Panic catch-all for the cluster /24: route the rest of the subnet
    # toward the lowest-loopback-octet node we can directly reach whose
    # octet is STRICTLY LOWER than our own. This is master-INDEPENDENT —
    # compute_routes reads nothing from rqlite. The VIP (.254) no longer
    # rides this route; it has its own advertised /32 (see the @vip
    # origination in build_advertisement_paths). The catch-all is now a
    # pure transient safety net covering the gap between a peer's
    # withdrawal / a fresh joiner and the next adv-table recompute.
    #
    # Loop-freedom: every node forwards only toward a strictly-lower
    # octet, so packets descend monotonically over the total order on
    # octets and terminate at the global-lowest node in <= N hops — no
    # loop is possible even mid-convergence or under partial connectivity.
    # The global-lowest node has no lower-octet neighbour, so it installs
    # NO catch-all and SINKS unknown traffic (drops it) rather than
    # bouncing it back up — the well-ordering base case. Worst case is a
    # black-hole on a genuine partition, which is the correct behaviour.
    # (The old "freshest neighbour" fallback had no such guarantee: two
    # nodes could each pick the other and loop until IP TTL expired.)
    if d.neighbours:
        from . import cluster_addr as _ca
        net = _ca.cluster_loopback_net(d.cluster_uuid)

        my_octet = _loopback_octet(d.my_loopback)
        best_nh: Optional[Neighbour] = None
        if my_octet > 0:
            for nb in _direct_neighbour_by_node(d).values():
                if not (nb.logged_up and nb.peer_link_addr and nb.peer_loopback):
                    continue
                oct_ = _loopback_octet(nb.peer_loopback)
                if oct_ <= 0 or oct_ >= my_octet:
                    continue   # only ever forward toward a lower octet
                if best_nh is None or oct_ < _loopback_octet(best_nh.peer_loopback):
                    best_nh = nb

        if best_nh is not None:
            routes.append(
                f"{net} via {best_nh.peer_link_addr} "
                f"dev {best_nh.my_nic} metric {METRIC_PANIC}"
            )
    return routes


def _loopback_octet(loopback_ip: str) -> int:
    """Last octet of a cluster loopback /32 (== node_index, 1..254), or 0
    if unparseable. The node's stable rank in the lowest-octet catch-all
    total order; the VIP at .254 never participates as a next-hop."""
    try:
        return int(loopback_ip.rsplit(".", 1)[-1])
    except (ValueError, AttributeError, IndexError):
        return 0


def current_cluster_routes(cluster_uuid: str) -> list[str]:
    """Read existing kernel routes that bedrock-net manages, scoped
    to this cluster's address block. We identify our routes by
    destination match:
      * <cluster_prefix>.0/24 panic catch-all
      * /32 inside <cluster_prefix>.0/24 (per-peer loopback) — may
        be single-path OR multipath (ECMP). Multipath routes
        appear in `ip route show` as a header line followed by
        indented `nexthop` continuation lines; we re-join them into
        the single-string form we emit so the diff round-trips
        cleanly.
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

    # First pass: join multipath continuation lines (lines starting
    # with whitespace are continuations of the previous header).
    joined: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        if line.startswith((" ", "\t")):
            if joined:
                joined[-1] = joined[-1] + " " + line.strip()
            continue
        joined.append(line.rstrip())

    keep: list[str] = []
    for line in joined:
        line = _normalize_route_line(line.strip())
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


def _normalize_route_line(line: str) -> str:
    """Convert `ip route show` output to the same form `compute_routes`
    emits, so set-based diffs work cleanly.

    Three normalisations:

    1. **Add /32 suffix** to bare-IP destinations. `ip route show`
       prints `100.X.Y.2 via ...` for host routes; we emit
       `100.X.Y.2/32 via ...`. Without this, every tick the diff
       sees current ≠ desired and churns the route table.
    2. **Drop kernel-added annotations** (`proto`, `pref`, `src`,
       `table`, `linkdown`, `onlink`). We never emit these, so they
       shouldn't appear in the desired set.
    3. **Move `metric N` to BEFORE the nexthop list** —
       iproute2's `ip route replace` requires that form ("metric"
       AFTER nexthop is a syntax error). compute_routes emits
       metric-first; we normalise the kernel's tail-form readback
       to match.
    """
    tokens = line.split()
    if not tokens:
        return ""

    # 1. /32 suffix on bare-IP destinations.
    dest = tokens[0]
    if "/" not in dest and dest.count(".") == 3 and dest[0].isdigit():
        tokens[0] = dest + "/32"

    # 2. Strip kernel-added annotations.
    out_tokens: list[str] = []
    skip_next = False
    drop_flags_with_value = {"proto", "pref", "src", "table"}
    drop_flags_no_value = {"linkdown", "onlink"}
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in drop_flags_with_value:
            skip_next = True
            continue
        if tok in drop_flags_no_value:
            continue
        out_tokens.append(tok)

    # 3. Multipath: move metric to BEFORE the first nexthop (the form
    # iproute2 accepts in `ip route replace`).
    if "nexthop" in out_tokens and "metric" in out_tokens:
        metric_idx = out_tokens.index("metric")
        nh_idx = out_tokens.index("nexthop")
        if metric_idx + 1 < len(out_tokens) and metric_idx > nh_idx:
            metric_val = out_tokens[metric_idx + 1]
            del out_tokens[metric_idx:metric_idx + 2]
            # Re-find nexthop index after the deletion (may have shifted).
            nh_idx = out_tokens.index("nexthop")
            out_tokens[nh_idx:nh_idx] = ["metric", metric_val]
    return " ".join(out_tokens)


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
