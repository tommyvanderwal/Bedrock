"""Python client for the bedrock-rust IPC socket.

Wire format mirrors `rust/bedrock-rust/src/ipc.rs`: each frame is a
4-byte big-endian length followed by a MessagePack-encoded body.

Usage:

    from lib.rust_ipc import Daemon

    with Daemon() as d:
        info = d.status()                    # {'latest_index': N, 'latest_hash': bytes}
        idx, h = d.append(kind=Kind.OPAQUE, payload=b"hello")
        for e in d.read(from_index=1):
            print(e['index'], e['kind'], e['payload'])

The socket lives at `/run/bedrock-rust.sock` by default.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import msgpack


DEFAULT_SOCK = "/run/bedrock-rust.sock"


class Kind:
    """Mirror of `rust/bedrock-rust/src/payload.rs::Kind`."""
    BOOTSTRAP = 0x01
    OPAQUE = 0x02


class IpcError(RuntimeError):
    """Raised on any IPC-level error: bad framing, daemon Error response,
    socket failure."""


@dataclass
class Daemon:
    """Open a connection to the bedrock-rust daemon over its Unix socket.

    Use as a context manager. Each method does one request → one response.
    """
    sock_path: str = DEFAULT_SOCK
    _sock: Optional[socket.socket] = None

    def __enter__(self) -> "Daemon":
        if not Path(self.sock_path).exists():
            raise IpcError(
                f"bedrock-rust IPC socket not found at {self.sock_path} — "
                f"is the daemon running?"
            )
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.sock_path)
        self._sock = s
        return self

    def __exit__(self, *_):
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # ── public API ──

    def status(self) -> dict:
        return self._call({"op": "status"}, expect="status")

    def append(self, payload: bytes, kind: int = Kind.OPAQUE) -> tuple[int, bytes]:
        r = self._call(
            {"op": "append", "kind": kind, "payload": payload}, expect="appended",
        )
        return r["index"], bytes(r["hash"])

    def read(self, from_index: int = 1, to: Optional[int] = None) -> Iterable[dict]:
        r = self._call(
            {"op": "read", "from": from_index, "to": to}, expect="entries",
        )
        for e in r["entries"]:
            yield {
                "index": e["index"],
                "epoch": e["epoch"],
                "prev_hash": bytes(e["prev_hash"]),
                "kind": e["kind"],
                "payload": bytes(e["payload"]),
                "hash": bytes(e["hash"]),
            }

    def verify(self) -> int:
        r = self._call({"op": "verify"}, expect="verified")
        return r["entries_checked"]

    def peer_status(self) -> list[dict]:
        """Snapshot of every peer link the daemon knows about. Each
        entry: {address, direction, identified_role, latest_index,
        last_acked_index, last_frame_ms_ago}. Used by `_wait_replicated`
        to confirm a freshly-appended entry has reached every peer."""
        r = self._call({"op": "peer_status"}, expect="peer_status")
        return list(r.get("links", []))

    def subscribe(self) -> Iterable[dict]:
        """Long-lived stream of committed entries.

        Sends `Subscribe`, expects an Ok confirmation, then yields each
        `Committed` entry as the daemon pushes it. Use a fresh `Daemon`
        connection for each subscription — the connection stays open for
        the lifetime of the iterator. Closes when the underlying socket
        closes (daemon restart, IPC error, etc.).
        """
        body = msgpack.packb({"op": "subscribe"}, use_bin_type=True)
        self._sock.sendall(struct.pack(">I", len(body)) + body)
        # First frame: confirmation Ok or Error
        first = self._read_response()
        kind = first.get("kind")
        if kind == "error":
            raise IpcError(first.get("message", "<no message>"))
        if kind != "ok":
            raise IpcError(f"subscribe: expected ok, got {kind!r}: {first!r}")
        while True:
            resp = self._read_response()
            kind = resp.get("kind")
            if kind == "committed":
                e = resp["entry"]
                yield {
                    "index": e["index"],
                    "epoch": e["epoch"],
                    "prev_hash": bytes(e["prev_hash"]),
                    "kind": e["kind"],
                    "payload": bytes(e["payload"]),
                    "hash": bytes(e["hash"]),
                }
            elif kind == "subscribe_overrun":
                # Caller's mailbox overflowed. Tell them so they can
                # reconnect-and-catch-up via Read.
                raise IpcError("subscribe: queue overrun; reconnect + catch up")
            elif kind == "error":
                raise IpcError(resp.get("message", "<no message>"))
            else:
                raise IpcError(f"subscribe: unexpected kind={kind!r}")

    # ── frame I/O ──

    def _read_response(self) -> dict:
        len_buf = self._recv_exact(4)
        n = struct.unpack(">I", len_buf)[0]
        body = self._recv_exact(n)
        return msgpack.unpackb(body, raw=False)

    def _call(self, req: dict, expect: str) -> dict:
        body = msgpack.packb(req, use_bin_type=True)
        self._sock.sendall(struct.pack(">I", len(body)) + body)
        resp = self._read_response()
        kind = resp.get("kind")
        if kind == "error":
            raise IpcError(resp.get("message", "<no message>"))
        if kind != expect:
            raise IpcError(f"unexpected response kind={kind!r}: {resp!r}")
        return resp

    def _recv_exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = self._sock.recv(n - len(out))
            if not chunk:
                raise IpcError("daemon closed the connection mid-frame")
            out += chunk
        return out
