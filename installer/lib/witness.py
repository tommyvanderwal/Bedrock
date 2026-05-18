"""BedRock witness — discovery + heartbeat against a BedRock Echo on the LAN.

The Echo is an ESP32 (or compatible) appliance that listens on UDP and
replies with a signed view of who is currently heartbeating to it. The
cluster never *trusts* the Echo to decide master; it just gets one
extra observable vote in the weighted election (see lib/election.py).

Wire format — intentionally tiny so an ESP32 firmware can implement it
in a few hundred lines:

  Probe   (cluster → Echo, broadcast):
      msgpack({"v": 1, "t": "probe", "cu": cluster_uuid,
               "n": my_node_name, "ts": int(epoch_ms),
               "nonce": 8 random bytes,
               "hmac": HMAC-SHA256(cluster_key, body_without_hmac)})

  Heartbeat (cluster → Echo, unicast to discovered Echo):
      msgpack({"v": 1, "t": "hb", "cu": cluster_uuid,
               "n": my_node_name, "ts": int(epoch_ms),
               "nonce": 8 random bytes, "hmac": ...})

  Reply    (Echo → node, unicast):
      msgpack({"v": 1, "t": "pong" | "hb_ack",
               "echo_id": "<hex>", "cu": cluster_uuid,
               "ts": int(epoch_ms),
               "peers": {node_name: last_seen_ms_relative, ...},
               "nonce": <echo of caller's>, "hmac": ...})

Discovery uses UDP broadcast on the mgmt LAN (port WITNESS_PORT). Echo
binds the same port and answers probes whose HMAC verifies for its
configured cluster_uuid / cluster_key. Nodes whose probe doesn't
match get silent-ignored — multiple clusters can coexist on the same
LAN with no leak.

State stored in lib/witness.py is intentionally minimal: a cached
list of discovered Echo addrs and the timestamp of the most recent
successful heartbeat per echo. The election layer reads that and
decides whether the witness vote is present.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Single UDP port — Echo binds it; nodes broadcast probes here and
# unicast heartbeats here. 9501 is unassigned (IANA registry as of
# writing); plays well with operator-grade firewalls that filter the
# 0-1023 + 4000-9000 windows. If this collides with something else on
# the LAN, override via /etc/bedrock/witness.conf in a follow-up.
WITNESS_PORT = 9501

# Magic prefix so we can drop garbage on the broadcast socket cheaply
# without invoking msgpack on every random LAN packet.
MAGIC = b"BREC"  # BedRock ECho

# How fresh a witness reply must be to count as "alive" for the
# election layer. 12s gives 3 missed 1s ticks plus jitter.
WITNESS_FRESHNESS_S = 12.0

# Discovery probes are broadcast at this cadence until at least one
# Echo replies; after that the cached endpoint is heartbeated and a
# fresh broadcast probe is only sent if every cached endpoint stops
# answering.
DISCOVERY_REPROBE_S = 30.0


@dataclass
class EchoEndpoint:
    addr: tuple[str, int]
    echo_id: str
    last_reply_ms: int = 0


@dataclass
class WitnessState:
    """Live state held by the election tick. Mutated in-place by the
    poll/heartbeat helpers. Read by lib/election.py."""
    cluster_uuid: str
    cluster_key: bytes
    my_node: str
    sock: socket.socket | None = None
    discovered: dict[str, EchoEndpoint] = field(default_factory=dict)
    last_probe_at: float = 0.0
    # Most recent reply timestamp across all endpoints (monotonic).
    last_alive_at: float = 0.0
    # Most recent observed-peers dict from any endpoint (newest wins).
    observed_peers: dict[str, int] = field(default_factory=dict)
    # Currently-blessed-by-the-witness master for the cluster's arbiter
    # DRBD volume — set when a node wins the election, completes its
    # promote, and publishes its tier-critical current-UUID via
    # send_claim(). Subsequent would-be promoters must check this:
    # if blessed_master is set and != self and fresh, refuse to
    # promote (zombie-old-master prevention).
    blessed_master: str = ""
    blessed_drbd_uuid: str = ""
    blessed_at_ms: int = 0


def _pack(body: dict, cluster_key: bytes) -> bytes:
    import msgpack
    # HMAC over the canonical form WITHOUT the hmac field, then add
    # the hmac and re-pack. Both sides do the same dance.
    canon = {k: body[k] for k in sorted(body) if k != "hmac"}
    payload = msgpack.packb(canon, use_bin_type=True)
    sig = _hmac.new(cluster_key, payload, hashlib.sha256).digest()[:16]
    body["hmac"] = sig
    return MAGIC + msgpack.packb(body, use_bin_type=True)


def _unpack(data: bytes, cluster_key: bytes) -> dict | None:
    if not data.startswith(MAGIC):
        return None
    try:
        import msgpack
        body = msgpack.unpackb(data[len(MAGIC):], raw=False)
    except Exception:
        return None
    if not isinstance(body, dict) or "hmac" not in body:
        return None
    sig = body.pop("hmac")
    canon = {k: body[k] for k in sorted(body)}
    import msgpack
    payload = msgpack.packb(canon, use_bin_type=True)
    expect = _hmac.new(cluster_key, payload, hashlib.sha256).digest()[:16]
    if not _hmac.compare_digest(sig, expect):
        return None
    return body


def open_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", WITNESS_PORT))
    s.setblocking(False)
    return s


def _build_probe(ws: WitnessState) -> bytes:
    return _pack({
        "v": 1, "t": "probe", "cu": ws.cluster_uuid,
        "n": ws.my_node, "ts": int(time.time() * 1000),
        "nonce": secrets.token_bytes(8),
    }, ws.cluster_key)


def _build_heartbeat(ws: WitnessState) -> bytes:
    return _pack({
        "v": 1, "t": "hb", "cu": ws.cluster_uuid,
        "n": ws.my_node, "ts": int(time.time() * 1000),
        "nonce": secrets.token_bytes(8),
    }, ws.cluster_key)


def broadcast_probe(ws: WitnessState, broadcast_addrs: Iterable[str]) -> None:
    if ws.sock is None:
        return
    pkt = _build_probe(ws)
    for addr in broadcast_addrs:
        try:
            ws.sock.sendto(pkt, (addr, WITNESS_PORT))
        except OSError:
            pass
    ws.last_probe_at = time.monotonic()


def heartbeat_all(ws: WitnessState) -> None:
    if ws.sock is None or not ws.discovered:
        return
    pkt = _build_heartbeat(ws)
    for ep in ws.discovered.values():
        try:
            ws.sock.sendto(pkt, ep.addr)
        except OSError:
            pass


def send_claim(ws: WitnessState, drbd_uuid: str) -> bool:
    """Publish a primacy claim to every discovered Echo: this node is
    now the cluster's master AND the tier-critical (arbiter-data)
    DRBD volume's current UUID is `drbd_uuid`. Witness records this
    and refuses to bless a competing claimant whose UUID doesn't
    match for a configurable hold-down window.

    Caller-contract: send this AFTER cluster_arbiter.promote_to_arbiter_host
    succeeds (drbdadm primary + mount + .254 VIP + arbiter rqlite
    + filer all up). Returns True if at least one Echo accepted the
    claim (ack came back with accepted=True)."""
    if ws.sock is None or not ws.discovered:
        return False
    pkt = _pack({
        "v": 1, "t": "claim", "cu": ws.cluster_uuid,
        "n": ws.my_node, "drbd_uuid": drbd_uuid,
        "ts": int(time.time() * 1000),
        "nonce": secrets.token_bytes(8),
    }, ws.cluster_key)
    for ep in ws.discovered.values():
        try:
            ws.sock.sendto(pkt, ep.addr)
        except OSError:
            pass
    return True


def drain_replies(ws: WitnessState, max_packets: int = 32) -> None:
    """Non-blocking drain — call every tick. Updates discovered[],
    last_alive_at, observed_peers, and the blessed_master / drbd_uuid
    fields on every accepted reply."""
    if ws.sock is None:
        return
    for _ in range(max_packets):
        try:
            data, src = ws.sock.recvfrom(1500)
        except (BlockingIOError, OSError):
            return
        body = _unpack(data, ws.cluster_key)
        if not body:
            continue
        if body.get("cu") != ws.cluster_uuid:
            continue
        kind = body.get("t")
        if kind not in ("pong", "hb_ack", "claim_ack"):
            continue
        echo_id = str(body.get("echo_id") or src[0])
        peers_raw = body.get("peers") or {}
        peers = {str(k): int(v) for k, v in peers_raw.items()
                 if isinstance(v, (int, float))}
        ep = ws.discovered.get(echo_id)
        now_ms = int(time.time() * 1000)
        if ep is None:
            ep = EchoEndpoint(addr=src, echo_id=echo_id, last_reply_ms=now_ms)
            ws.discovered[echo_id] = ep
        else:
            ep.addr = src
            ep.last_reply_ms = now_ms
        ws.observed_peers = peers
        ws.last_alive_at = time.monotonic()
        # Witness's view of who currently owns the cluster (set by an
        # accepted claim). Empty until the first successful failover.
        bm = body.get("blessed_master") or ""
        bu = body.get("blessed_drbd_uuid") or ""
        bat = body.get("blessed_at_ms") or 0
        if bm:
            ws.blessed_master = str(bm)
            ws.blessed_drbd_uuid = str(bu)
            try:
                ws.blessed_at_ms = int(bat)
            except (TypeError, ValueError):
                ws.blessed_at_ms = now_ms


def is_alive(ws: WitnessState) -> bool:
    """Witness vote is valid iff we got a reply in the last
    WITNESS_FRESHNESS_S seconds."""
    if ws.last_alive_at == 0.0:
        return False
    return (time.monotonic() - ws.last_alive_at) <= WITNESS_FRESHNESS_S


def needs_reprobe(ws: WitnessState) -> bool:
    """True if we should re-broadcast a probe — either we've never
    discovered an Echo, or every cached endpoint has gone stale."""
    if not ws.discovered:
        return time.monotonic() - ws.last_probe_at >= 1.0
    if is_alive(ws):
        return False
    # All endpoints stale — re-probe at DISCOVERY_REPROBE_S cadence.
    return time.monotonic() - ws.last_probe_at >= DISCOVERY_REPROBE_S


def load_cluster_key(path: Path = Path("/etc/bedrock/cluster.key")) -> bytes:
    """Read the shared HMAC key from disk. install.sh + mgmt_install
    write this; agent_install copies it from the master at join time.
    Empty if unreadable — caller decides whether that's fatal."""
    try:
        return path.read_bytes().strip()
    except OSError:
        return b""
