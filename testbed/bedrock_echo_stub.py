#!/usr/bin/env python3
"""Stub BedRock Echo for testbed use.

Listens on the LAN broadcast UDP port for cluster nodes' probe + hb
packets, verifies the HMAC against the cluster key, and replies with
its observed STATUS_LIST. This is the minimum implementation that
lets lib/witness.py + lib/election.py exercise the 2-node-HA failover
path without an ESP32 device on the bench.

Wire format: same one defined in installer/lib/witness.py
(MAGIC+msgpack, HMAC-SHA256-truncated-16 over the canonical body).

Usage:
    python3 bedrock_echo_stub.py --cluster-key-file /path/to/key.bin
    python3 bedrock_echo_stub.py --cluster-key-hex <64-hex>

Echo binds 0.0.0.0:9501 on every interface it can reach (we want it
visible across the testbed's mgmt LAN AND the sims' bedrock-mesh
links so isolation tests can put it on one side of the partition).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac as _hmac
import secrets
import socket
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "installer" / "lib"))
from witness import MAGIC, WITNESS_PORT  # type: ignore
import msgpack


def _verify(data: bytes, key: bytes) -> dict | None:
    if not data.startswith(MAGIC):
        return None
    try:
        body = msgpack.unpackb(data[len(MAGIC):], raw=False)
    except Exception:
        return None
    if not isinstance(body, dict) or "hmac" not in body:
        return None
    sig = body.pop("hmac")
    canon = {k: body[k] for k in sorted(body)}
    payload = msgpack.packb(canon, use_bin_type=True)
    expect = _hmac.new(key, payload, hashlib.sha256).digest()[:16]
    if not _hmac.compare_digest(sig, expect):
        return None
    return body


def _pack(body: dict, key: bytes) -> bytes:
    canon = {k: body[k] for k in sorted(body) if k != "hmac"}
    payload = msgpack.packb(canon, use_bin_type=True)
    sig = _hmac.new(key, payload, hashlib.sha256).digest()[:16]
    body["hmac"] = sig
    return MAGIC + msgpack.packb(body, use_bin_type=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cluster-key-file", help="Read the 32-byte HMAC key from a file")
    ap.add_argument("--cluster-key-hex", help="Hex-encoded HMAC key")
    ap.add_argument("--echo-id", default="testbed-echo-1",
                    help="Identifier broadcast back to nodes")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.cluster_key_file:
        key = open(args.cluster_key_file, "rb").read().strip()
    elif args.cluster_key_hex:
        key = bytes.fromhex(args.cluster_key_hex)
    else:
        print("ERROR: --cluster-key-file or --cluster-key-hex required",
              file=sys.stderr)
        return 2
    if len(key) != 32:
        print(f"ERROR: cluster key must be 32 bytes (got {len(key)})",
              file=sys.stderr)
        return 2

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", WITNESS_PORT))
    print(f"bedrock-echo-stub: listening on 0.0.0.0:{WITNESS_PORT} "
          f"echo_id={args.echo_id} cluster_uuid_filter=any",
          file=sys.stderr, flush=True)

    # STATUS_LIST: node_name → last_seen_ms (epoch). Decays naturally
    # because clients keep heartbeating; entries older than 60s are
    # implicitly stale.
    status: dict[str, int] = {}

    while True:
        try:
            data, src = s.recvfrom(1500)
        except KeyboardInterrupt:
            return 0
        body = _verify(data, key)
        if not body:
            if args.verbose:
                print(f"  ignore {src}: bad hmac / not for us",
                      file=sys.stderr)
            continue
        kind = body.get("t")
        node = str(body.get("n") or "")
        cu = str(body.get("cu") or "")
        if not node:
            continue
        now_ms = int(time.time() * 1000)
        status[node] = now_ms
        # Build the relative STATUS_LIST (now − last_seen, ms).
        peers = {n: now_ms - ts for n, ts in status.items()}
        reply = {
            "v": 1,
            "t": "pong" if kind == "probe" else "hb_ack",
            "echo_id": args.echo_id,
            "cu": cu,
            "ts": now_ms,
            "peers": peers,
            "nonce": body.get("nonce") or secrets.token_bytes(8),
        }
        try:
            s.sendto(_pack(reply, key), src)
        except OSError as e:
            print(f"  send reply to {src}: {e!r}", file=sys.stderr)
        if args.verbose:
            print(f"  {kind} {node} ← {src}; status now {list(peers)}",
                  file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
