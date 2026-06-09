#!/usr/bin/env python3
"""Stub BedRock Echo for testbed use — passive AEAD-encrypted K/V slot store.

Per docs/cluster-quorum-spec.md: the witness has NO logic. It stores
each node's most recent encrypted slot blob and returns all slots on
every reply. The stub never decrypts slot blobs (they're AEAD-encrypted
opaque bytes from its point of view); it does AEAD-decrypt the envelope
to extract `(n, slot_blob)` and AEAD-encrypts the reply envelope.

Wire format: see lib/witness.py docstring. Single primitive:
ChaCha20-Poly1305 (12-byte nonces, 16-byte tags). 32-byte cluster_key
loaded from --cluster-key-file or --cluster-key-hex.

Usage:
    python3 bedrock_echo_stub.py --cluster-key-hex <64-hex>
    python3 bedrock_echo_stub.py --cluster-key-file /etc/bedrock/cluster.key

UDP/12321 on 0.0.0.0. State is in-memory (testbed only); production
echo MUST persist slots across restart.
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import sys
import threading
import time

# Same constants the cluster nodes use.
MAGIC = b"BREC"
WITNESS_PORT = 12321  # canonical bedrock-echo UDP port (matches firmware repo)
NONCE_LEN = 12

# mDNS service advertisement (so a cluster can `discover_echo_witnesses()` and
# the dashboard 'Scan LAN' finds this Echo by id+pubkey). Mirrors
# lib/discovery.py's ECHO_MDNS_NAME; the real ESP32 firmware
# advertises the same service.
ECHO_MDNS_NAME = b"bedrock-echo.local"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353


def _aead(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    return ChaCha20Poly1305(key)


def _seal(key: bytes, plaintext: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + _aead(key).encrypt(nonce, plaintext, None)


def _open(key: bytes, blob: bytes):
    if len(blob) < NONCE_LEN + 16:
        return None
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    try:
        return _aead(key).decrypt(nonce, ct, None)
    except Exception:
        return None


# ── mDNS advertisement (bedrock-echo.local) ──────────────────────────────

def _encode_qname(name: bytes) -> bytes:
    out = bytearray()
    for label in name.split(b"."):
        if label:
            out.append(len(label))
            out += label
    out.append(0)
    return bytes(out)


def _reply_ip_for(dst_ip: str) -> str:
    """The local IPv4 the OS would use to reach ``dst_ip`` (no packet sent) —
    picks the right interface even on a multi-homed host, so the A record we
    advertise is one the querier can actually reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dst_ip, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _build_echo_mdns_response(reply_ip: str, echo_id: str, pubkey: str) -> bytes:
    """mDNS response advertising this Echo: A (reply_ip) + TXT
    (echo_id=…;pubkey=…) for bedrock-echo.local. Matches what
    discovery._parse_mdns_response expects."""
    name = _encode_qname(ECHO_MDNS_NAME)
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 0)   # response, 2 answers
    a_rr = name + struct.pack("!HHIH", 1, 0x8001, 120, 4) + socket.inet_aton(reply_ip)
    txt_rdata = bytearray()
    for k, v in (("echo_id", echo_id), ("pubkey", pubkey)):
        seg = f"{k}={v}".encode()
        txt_rdata.append(len(seg))
        txt_rdata += seg
    txt_rr = name + struct.pack("!HHIH", 16, 0x8001, 120, len(txt_rdata)) + bytes(txt_rdata)
    return header + a_rr + txt_rr


def _mdns_responder(echo_id: str, pubkey: str, verbose: bool = False) -> None:
    """Daemon-thread body: answer mDNS queries for bedrock-echo.local with our
    A + TXT, unicast back to the querier (matching mdns_responder.py and what
    discover_echo_witnesses listens for on its ephemeral socket). Fail-soft: if
    the multicast socket can't bind (e.g. avahi already holds 5353), log and
    disable mDNS WITHOUT killing the witness UDP path."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", MDNS_PORT))
        mreq = socket.inet_aton(MDNS_GROUP) + socket.inet_aton("0.0.0.0")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    except OSError as e:
        print(f"bedrock-echo-stub: mDNS disabled ({e!r}) — witness UDP still up",
              file=sys.stderr, flush=True)
        return
    wanted = _encode_qname(ECHO_MDNS_NAME)
    print(f"bedrock-echo-stub: advertising {ECHO_MDNS_NAME.decode()} via mDNS "
          f"(echo_id={echo_id})", file=sys.stderr, flush=True)
    while True:
        try:
            data, src = s.recvfrom(2048)
        except OSError:
            continue
        if len(data) < 12:
            continue
        flags = struct.unpack_from("!H", data, 2)[0]
        if flags & 0x8000:           # a response, not a query — ignore
            continue
        if wanted not in data:       # not asking for our service
            continue
        resp = _build_echo_mdns_response(_reply_ip_for(src[0]), echo_id, pubkey)
        try:
            s.sendto(resp, src)      # unicast back to the querier
        except OSError:
            pass
        if verbose:
            print(f"  mDNS answer → {src[0]}", file=sys.stderr)


def main() -> int:
    import msgpack

    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-key-file", help="32-byte cluster.key path")
    ap.add_argument("--cluster-key-hex", help="64-hex-char cluster_key")
    ap.add_argument("--echo-id", default="testbed-echo-1")
    ap.add_argument("--pubkey", default="",
                    help="X25519 pubkey hex to advertise in the mDNS TXT record")
    ap.add_argument("--no-mdns", action="store_true",
                    help="disable the bedrock-echo.local mDNS advertisement")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.cluster_key_file:
        key = open(args.cluster_key_file, "rb").read()
        if len(key) == 33 and key[-1:] == b"\n":
            key = key[:32]
    elif args.cluster_key_hex:
        key = bytes.fromhex(args.cluster_key_hex)
    else:
        print("ERROR: --cluster-key-file or --cluster-key-hex required",
              file=sys.stderr)
        return 2
    if len(key) != 32:
        print(f"ERROR: cluster_key must be 32 bytes (got {len(key)})",
              file=sys.stderr)
        return 2

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", WITNESS_PORT))
    print(f"bedrock-echo-stub: listening on 0.0.0.0:{WITNESS_PORT} "
          f"echo_id={args.echo_id}", file=sys.stderr, flush=True)

    # Advertise the Echo over mDNS so clusters can discover it (id+pubkey).
    if not args.no_mdns:
        threading.Thread(
            target=_mdns_responder,
            args=(args.echo_id, args.pubkey, args.verbose),
            name="echo-mdns", daemon=True,
        ).start()

    # Per cluster_uuid → {node_id: opaque slot blob}. Each "blob" is
    # nonce(12)+ciphertext+tag bytes that we never decrypt. Cluster
    # nodes decrypt locally with their cluster_key.
    slots_by_cluster: dict[str, dict[int, bytes]] = {}

    while True:
        try:
            data, src = s.recvfrom(2048)
        except KeyboardInterrupt:
            return 0
        if not data.startswith(MAGIC):
            continue
        pt = _open(key, data[len(MAGIC):])
        if pt is None:
            if args.verbose:
                print(f"  drop {src}: bad envelope AEAD", file=sys.stderr)
            continue
        try:
            env = msgpack.unpackb(pt, raw=False, strict_map_key=False)
        except Exception:
            continue
        if not isinstance(env, dict):
            continue
        t = env.get("t")
        cu = env.get("cu")
        n  = env.get("n")
        if t not in ("hb", "probe") or not cu:
            continue
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if not (1 <= n <= 250):
            continue

        # Store slot blob if this is a heartbeat with one.
        if t == "hb" and "slot" in env:
            blob = env["slot"]
            if isinstance(blob, (bytes, bytearray)):
                slots_by_cluster.setdefault(cu, {})[n] = bytes(blob)
                if args.verbose:
                    print(f"  hb node={n} slot stored ({len(blob)}B)",
                          file=sys.stderr)
        elif args.verbose:
            print(f"  {t} node={n}", file=sys.stderr)

        # Build reply: ALL slots for this cluster_uuid.
        slots = slots_by_cluster.get(cu, {})
        ack = msgpack.packb({
            "v":       1,
            "t":       "ack",
            "echo_id": args.echo_id,
            "cu":      cu,
            "slots":   {int(k): v for k, v in slots.items()},
        }, use_bin_type=True)
        wire = MAGIC + _seal(key, ack)
        try:
            s.sendto(wire, src)
        except OSError as e:
            print(f"  send to {src}: {e!r}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
