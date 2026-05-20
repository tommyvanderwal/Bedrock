#!/usr/bin/env python3
"""Stub BedRock Echo for testbed use — passive AEAD-encrypted K/V slot store.

Per docs/cluster-quorum-spec.md: the witness has NO logic. It stores
each node's most recent encrypted slot blob and returns all slots on
every reply. The stub never decrypts slot blobs (they're AEAD-encrypted
opaque bytes from its point of view); it does AEAD-decrypt the envelope
to extract `(n, slot_blob)` and AEAD-encrypts the reply envelope.

Wire format: see installer/lib/witness.py docstring. Single primitive:
ChaCha20-Poly1305 (12-byte nonces, 16-byte tags). 32-byte cluster_key
loaded from --cluster-key-file or --cluster-key-hex.

Usage:
    python3 bedrock_echo_stub.py --cluster-key-hex <64-hex>
    python3 bedrock_echo_stub.py --cluster-key-file /etc/bedrock/cluster.key

UDP/9501 on 0.0.0.0. State is in-memory (testbed only); production
echo MUST persist slots across restart.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

# Same constants the cluster nodes use.
MAGIC = b"BREC"
WITNESS_PORT = 9501
NONCE_LEN = 12


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


def main() -> int:
    import msgpack

    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-key-file", help="32-byte cluster.key path")
    ap.add_argument("--cluster-key-hex", help="64-hex-char cluster_key")
    ap.add_argument("--echo-id", default="testbed-echo-1")
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
