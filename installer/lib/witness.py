"""BedRock witness — passive per-node K/V slot store.

See docs/cluster-quorum-spec.md for the load-bearing protocol. Short
version: each node owns one slot (key = node_id, single byte 1-250),
writes its own slot every 1 s, and reads every other node's slot to
decide arbiter takeover. The witness has NO logic — it stores last-
write per slot and returns all slots on every reply.

Wire format (envelope, on every UDP packet OR fileshare blob):
    b"BREC" || nonce(12) || ChaCha20Poly1305(cluster_key, nonce, plaintext)

Where plaintext = msgpack({
    v: 1, t: "hb"|"probe"|"ack",
    cu: cluster_uuid (16 bytes),
    n:  node_id (1 byte, 1-250),       # writer's id (hb/probe only)
    slot:  <encrypted slot blob>       # hb only (own slot to publish)
    slots: {nid: <encrypted slot blob>, ...}    # ack only
    echo_id: "...",                    # ack only
})

A slot blob is itself an AEAD packet: nonce(12) || ChaCha20Poly1305(
cluster_key, nonce, msgpack(slot_plaintext)) — opaque to the witness.

Slot plaintext:
    {
        n:         node_id,            # must match envelope's n
        ts_writer: u64 epoch_ms,       # writer's clock; reader uses own clock
        tag:       u8 bitflag,         # bit 0 = lms; bits 1-7 reserved (=0)
        marker:    bytes,              # most relevant generation marker
                                       # (drbd current-uuid for arbiter slot)
        kind:      u8,                 # 1 = drbd-arbiter-uuid (others reserved)
    }
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Wire constants — kept tiny so an ESP32 firmware can implement this
# in a few hundred lines.
WITNESS_PORT = 12321  # canonical bedrock-echo UDP port (github.com/.../bedrock-echo)
MAGIC = b"BREC"
NONCE_LEN = 12        # ChaCha20Poly1305 nonce
WITNESS_FRESHNESS_S = 12.0     # reply-freshness for "witness reachable" vote
DISCOVERY_REPROBE_S = 30.0     # re-broadcast probe cadence after staleness
SLOT_STALE_MS       = 10_000   # master-slot-stale threshold (EXECUTION-PLAN
                               # BAD-1: a survivor treats the master's slot
                               # as gone after >10 s, matching the 10-missed-
                               # heartbeat leader-loss detector in netd)
NODE_ID_MIN         = 1
NODE_ID_MAX         = 250

# Tag bitflags. Bit 0 = lms; other bits reserved.
TAG_LMS    = 0x01

# Marker kinds.
MARKER_KIND_DRBD_ARBITER_UUID = 1


@dataclass
class Slot:
    """A decoded slot we got back from the witness in a reply. Plaintext."""
    node_id:   int
    ts_writer_ms: int
    tag:       int
    marker:    bytes
    kind:      int = MARKER_KIND_DRBD_ARBITER_UUID
    # When the local reader observed this slot (monotonic).
    seen_at_monotonic: float = 0.0

    @property
    def lms(self) -> bool:
        return bool(self.tag & TAG_LMS)

    def is_stale(self, now_local_ms: Optional[int] = None,
                 threshold_ms: int = SLOT_STALE_MS) -> bool:
        """Stale = (reader's clock) - (writer's claimed ts) >= threshold."""
        if now_local_ms is None:
            now_local_ms = int(time.time() * 1000)
        return (now_local_ms - self.ts_writer_ms) >= threshold_ms


@dataclass
class EchoEndpoint:
    addr: tuple[str, int]
    echo_id: str
    last_reply_ms: int = 0
    # Per-witness decoded slot cache from THIS endpoint's most recent
    # reply (M10 multi-witness). Each configured witness is validated
    # INDIVIDUALLY (a slot for every active node + our own readback) so
    # that, with multiple witnesses configured, each valid+confirmed one
    # contributes +1 to the tally. The merged ws.slots (latest reply,
    # any witness) is kept for the single-witness takeover read path.
    slots: dict = field(default_factory=dict)
    last_reply_monotonic: float = 0.0


@dataclass
class WitnessState:
    cluster_uuid: str        # used as msgpack `cu` value; stored as 16-byte str
    cluster_key: bytes       # 32 bytes — AEAD key
    my_node_id: int          # 1-250
    my_node_name: str = ""   # informational, not on wire
    sock: Optional[socket.socket] = None
    discovered: dict[str, EchoEndpoint] = field(default_factory=dict)
    last_probe_at: float = 0.0
    last_alive_at: float = 0.0   # monotonic of most recent decryptable reply
    # Live local cache of slots from the most recent reply. Decoded.
    slots: dict[int, Slot] = field(default_factory=dict)

    # ── outgoing slot we (this node) want to publish each tick ──
    # The election tick updates these; heartbeat_all/broadcast_probe
    # serialise + encrypt + send. None = no slot to publish yet
    # (early boot, before drbd UUID is known).
    own_marker: bytes = b""
    own_kind: int = MARKER_KIND_DRBD_ARBITER_UUID
    own_tag: int = 0   # bit 0 = lms

    # Current active-node member set (node_ids), refreshed each netd
    # tick from rqlite's `nodes` table via cluster_state.load_cluster().
    # drain_replies drops any slot whose node_id is NOT in this set so
    # a decommissioned node's stale slot stops counting (the primary
    # stuck-LMS escape, cluster-quorum-spec INV-7 path b). A witness is
    # only `is_valid` when it holds a slot for *every* member here.
    # None = "membership not yet known" — no filtering, no validity.
    member_ids: Optional[set[int]] = None

    # Configured Echo endpoints (host, port) from the rqlite `witnesses`
    # table (backend=='echo'). netd refreshes this each tick and the probe
    # step unicasts to each — so an Echo added BY IP that is OFF the broadcast
    # domain (routed/off-subnet) is still probed + can vote. broadcast_probe
    # only reaches the local L2; this directed probe covers the rest. The
    # reply is keyed by the Echo's echo_id in drain_replies, so a configured
    # endpoint and a broadcast-found one dedupe to a single discovered entry.
    configured_echo_addrs: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────
#  Crypto helpers
# ─────────────────────────────────────────────────────────────────

def _aead():
    """Lazy import — keep witness.py import cheap when not in use."""
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    return ChaCha20Poly1305


def _msgpack():
    import msgpack
    return msgpack


def _aead_seal(key: bytes, plaintext: bytes) -> bytes:
    """Returns nonce(12) || ChaCha20Poly1305(key, nonce, plaintext) -> bytes.
    The Poly1305 tag is appended to the ciphertext by the library."""
    nonce = os.urandom(NONCE_LEN)
    c = _aead()(key).encrypt(nonce, plaintext, None)
    return nonce + c


def _aead_open(key: bytes, blob: bytes) -> Optional[bytes]:
    """Inverse of _aead_seal. Returns plaintext bytes or None on auth fail."""
    if len(blob) < NONCE_LEN + 16:
        return None
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return _aead()(key).decrypt(nonce, ct, None)
    except Exception:
        return None


def _encode_slot(ws: WitnessState, ts_writer_ms: int) -> bytes:
    """Encrypt own slot. Returns opaque slot blob (to witness)."""
    mp = _msgpack()
    payload = mp.packb({
        "n":         ws.my_node_id,
        "ts_writer": int(ts_writer_ms),
        "tag":       int(ws.own_tag),
        "marker":    ws.own_marker or b"",
        "kind":      int(ws.own_kind),
    }, use_bin_type=True)
    return _aead_seal(ws.cluster_key, payload)


def _decode_slot(key: bytes, blob: bytes) -> Optional[Slot]:
    """Decrypt a slot blob; build a Slot dataclass."""
    if not isinstance(blob, (bytes, bytearray)):
        return None
    pt = _aead_open(key, bytes(blob))
    if pt is None:
        return None
    try:
        body = _msgpack().unpackb(pt, raw=False, strict_map_key=False)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    try:
        n  = int(body.get("n", 0))
        ts = int(body.get("ts_writer", 0))
        tg = int(body.get("tag", 0))
        mk = body.get("marker", b"") or b""
        kd = int(body.get("kind", 0))
    except (TypeError, ValueError):
        return None
    if not (NODE_ID_MIN <= n <= NODE_ID_MAX):
        return None
    return Slot(node_id=n, ts_writer_ms=ts, tag=tg, marker=bytes(mk), kind=kd,
                seen_at_monotonic=time.monotonic())


def _encode_envelope(ws: WitnessState, *, t: str,
                     include_own_slot: bool) -> bytes:
    """Build the on-wire envelope: magic || nonce || AEAD(envelope_plain)."""
    mp = _msgpack()
    env = {
        "v": 1,
        "t": t,
        "cu": ws.cluster_uuid,
        "n":  ws.my_node_id,
    }
    if include_own_slot and ws.own_marker:
        env["slot"] = _encode_slot(ws, int(time.time() * 1000))
    plain = mp.packb(env, use_bin_type=True)
    return MAGIC + _aead_seal(ws.cluster_key, plain)


def _decode_envelope(key: bytes, data: bytes) -> Optional[dict]:
    if not data.startswith(MAGIC):
        return None
    pt = _aead_open(key, data[len(MAGIC):])
    if pt is None:
        return None
    try:
        body = _msgpack().unpackb(pt, raw=False, strict_map_key=False)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body


# ─────────────────────────────────────────────────────────────────
#  Public API — called by netd's election tick
# ─────────────────────────────────────────────────────────────────

def open_socket() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", WITNESS_PORT))
    s.setblocking(False)
    return s


def broadcast_probe(ws: WitnessState, broadcast_addrs: Iterable[str]) -> None:
    """Send a probe (no slot payload) to every broadcast address. Used at
    discovery time and when all cached endpoints have gone stale."""
    if ws.sock is None:
        return
    pkt = _encode_envelope(ws, t="probe", include_own_slot=False)
    for addr in broadcast_addrs:
        try:
            ws.sock.sendto(pkt, (addr, WITNESS_PORT))
        except OSError:
            pass
    ws.last_probe_at = time.monotonic()


def unicast_probe(ws: WitnessState, endpoints: Iterable) -> None:
    """Send a probe to each explicit (host, port) — for CONFIGURED Echo
    witnesses which may be OFF the broadcast domain (added by IP / routed).
    Mirrors broadcast_probe but targets exact addresses + ports (broadcast_probe
    hardcodes WITNESS_PORT and only reaches the local L2). Replies are keyed by
    the Echo's echo_id in drain_replies, so a configured endpoint dedupes with
    any broadcast-found one. Best-effort; never raises."""
    if ws.sock is None:
        return
    pkt = _encode_envelope(ws, t="probe", include_own_slot=False)
    for ep in endpoints:
        try:
            host, port = ep
            ws.sock.sendto(pkt, (host, int(port)))
        except (OSError, ValueError, TypeError):
            pass


def heartbeat_all(ws: WitnessState) -> None:
    """Unicast a heartbeat (with own slot payload) to every discovered
    Echo. Called once per election tick."""
    if ws.sock is None or not ws.discovered:
        return
    pkt = _encode_envelope(ws, t="hb", include_own_slot=True)
    for ep in ws.discovered.values():
        try:
            ws.sock.sendto(pkt, ep.addr)
        except OSError:
            pass


def drain_replies(ws: WitnessState, max_packets: int = 32) -> None:
    """Non-blocking recvfrom loop. Updates discovered[], last_alive_at,
    and the local slots cache from each accepted reply."""
    if ws.sock is None:
        return
    for _ in range(max_packets):
        try:
            data, src = ws.sock.recvfrom(2048)
        except (BlockingIOError, OSError):
            return
        body = _decode_envelope(ws.cluster_key, data)
        if not body:
            continue
        if body.get("cu") != ws.cluster_uuid:
            continue
        if body.get("t") != "ack":
            continue
        echo_id = str(body.get("echo_id") or src[0])
        now_ms = int(time.time() * 1000)
        ep = ws.discovered.get(echo_id)
        if ep is None:
            ep = EchoEndpoint(addr=src, echo_id=echo_id, last_reply_ms=now_ms)
            ws.discovered[echo_id] = ep
        else:
            ep.addr = src
            ep.last_reply_ms = now_ms
        ws.last_alive_at = time.monotonic()
        # Decode every slot blob in the reply, dropping any slot whose
        # node_id is not a current cluster member (rqlite `nodes`
        # table, plumbed onto ws.member_ids each netd tick). This is
        # the primary stuck-LMS escape from cluster-quorum-spec INV-7
        # path (b): `bedrock node leave` removes the node from rqlite,
        # so its stale lms=1 slot stops counting and a takeover can
        # proceed. member_ids=None means membership isn't known yet
        # (early boot) — don't filter, but the witness also can't be
        # `is_valid` without it.
        slots_blob = body.get("slots") or {}
        if isinstance(slots_blob, dict):
            new_cache: dict[int, Slot] = {}
            for nid_raw, blob in slots_blob.items():
                try:
                    nid = int(nid_raw)
                except (TypeError, ValueError):
                    continue
                if ws.member_ids is not None and nid not in ws.member_ids:
                    continue
                slot = _decode_slot(ws.cluster_key, blob)
                if slot is not None and slot.node_id == nid:
                    new_cache[nid] = slot
            # Per-witness cache for individual validity (M10), plus the
            # merged latest-reply cache the single-witness takeover path
            # reads. Atomic-ish swap; election tick reads from ws.slots.
            ep.slots = new_cache
            ep.last_reply_monotonic = time.monotonic()
            ws.slots = new_cache


def is_alive(ws: WitnessState) -> bool:
    """Witness is reachable iff a reply landed within the freshness window."""
    if ws.last_alive_at == 0.0:
        return False
    return (time.monotonic() - ws.last_alive_at) <= WITNESS_FRESHNESS_S


def _slots_valid(ws: WitnessState, slots: dict) -> bool:
    """Validity check over a specific slot cache (one witness's, or the
    merged one): holds a slot for EVERY active member. Requires the
    membership set to be known + non-empty."""
    if not ws.member_ids:
        return False
    return all(nid in slots for nid in ws.member_ids)


def _slots_confirmed(ws: WitnessState, slots: dict,
                     now_local_ms: Optional[int] = None) -> bool:
    """Confirmation check over a specific slot cache: our own slot is
    present, carries our current marker, and is fresh (readback proof)."""
    own = slots.get(int(ws.my_node_id))
    if own is None:
        return False
    if ws.own_marker and own.marker != ws.own_marker:
        return False
    return not own.is_stale(now_local_ms)


def is_valid(ws: WitnessState) -> bool:
    """A witness is VALID (eligible to add to the vote tally) only if it
    currently holds a slot for EVERY active node in the cluster
    (cluster-quorum-spec witness-validity rule). Entries may be
    stale/hours-old — fine — but a *missing* member's slot ⇒ the witness
    can't testify to that node ⇒ invalid ⇒ contributes 0 votes.

    Validity requires the membership set to be known *and* non-empty;
    until netd plumbs ws.member_ids from rqlite we cannot certify the
    witness, so it counts 0 (biases toward "do not fail over").

    This is the merged-cache (single-witness) view; count_valid_confirmed
    is the per-witness multi-witness tally."""
    return _slots_valid(ws, ws.slots)


def is_confirmed(ws: WitnessState,
                 now_local_ms: Optional[int] = None) -> bool:
    """A witness is CONFIRMED for the candidate's own takeover use when
    the candidate wrote its own slot here and read it back this cycle —
    i.e. our slot is present with our current marker and a fresh
    ts_writer. This is the readback proof the takeover step-5 relies on,
    surfaced as a predicate so the election can fold it into the tally
    (only valid+confirmed witnesses add +1)."""
    return _slots_confirmed(ws, ws.slots, now_local_ms)


def count_valid_confirmed(ws: WitnessState, n_configured: int,
                          now_local_ms: Optional[int] = None) -> int:
    """M10 multi-witness tally: how many CONFIGURED witnesses are
    INDIVIDUALLY valid+confirmed right now, capped at n_configured.

    Each discovered Echo endpoint keeps its own slot cache (ep.slots),
    validated independently: it must hold a slot for every active member
    AND reflect our own readback. A witness whose last reply has gone
    stale (beyond the freshness window) is not counted. The cap ensures
    we never tally more valid witnesses than the cluster has configured
    (a rogue/extra Echo answering can't inflate the vote).

    For the single-witness testbed this yields 0 or 1, matching the old
    `1 if (is_valid and is_confirmed) else 0`. With multiple configured
    witnesses, each valid+confirmed one now contributes correctly to the
    tally (and all configured ones still count in the denominator, via
    n_configured passed to election.compute separately)."""
    if n_configured <= 0 or not ws.member_ids:
        return 0
    now_mono = time.monotonic()
    count = 0
    for ep in ws.discovered.values():
        if (now_mono - ep.last_reply_monotonic) > WITNESS_FRESHNESS_S:
            continue
        if (_slots_valid(ws, ep.slots)
                and _slots_confirmed(ws, ep.slots, now_local_ms)):
            count += 1
    return min(count, n_configured)


def needs_reprobe(ws: WitnessState) -> bool:
    if not ws.discovered:
        return time.monotonic() - ws.last_probe_at >= 1.0
    if is_alive(ws):
        return False
    return time.monotonic() - ws.last_probe_at >= DISCOVERY_REPROBE_S


def set_own_slot(ws: WitnessState, *, marker: bytes,
                 tag: int = 0,
                 kind: int = MARKER_KIND_DRBD_ARBITER_UUID) -> None:
    """Update what THIS node will publish on its next heartbeat.

    Caller responsibility: every 1 s, set `marker` to the current
    DRBD current-UUID of the `cluster` singleton resource and `tag` to
    `TAG_LMS` if operating last-man-standing, else 0. heartbeat_all
    picks these up on the next tick."""
    ws.own_marker = bytes(marker)
    ws.own_tag = int(tag) & 0xFF
    ws.own_kind = int(kind) & 0xFF


def read_slot(ws: WitnessState, node_id: int) -> Optional[Slot]:
    """Return the most recent decoded Slot for `node_id`, or None if we
    haven't seen one. Cache is replaced wholesale on every drain_replies."""
    return ws.slots.get(int(node_id))


def own_slot(ws: WitnessState) -> Optional[Slot]:
    """Convenience: read our own slot from the witness's view of us.
    Used by takeover step 5 (readback check)."""
    return ws.slots.get(int(ws.my_node_id))


# ─────────────────────────────────────────────────────────────────
#  Cluster key on disk
# ─────────────────────────────────────────────────────────────────

def load_cluster_key(path: Path = Path("/etc/bedrock/cluster.key")) -> bytes:
    """Read the shared AEAD key from disk. install.sh + mgmt_install
    write this; agent_install copies it from the master at join.
    Returns b"" if unreadable — caller decides whether that's fatal.

    DO NOT strip(): cluster.key is 32 raw random bytes. ~5% of randomly
    generated keys start or end with a byte that bytes.strip() treats
    as whitespace (0x09/0x0A/0x0B/0x0C/0x0D/0x20)."""
    try:
        data = path.read_bytes()
    except OSError:
        return b""
    if len(data) == 33 and data[-1:] == b"\n":
        return data[:32]
    return data
