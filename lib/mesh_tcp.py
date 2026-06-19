"""TCP per-adjacency mesh routing (protocol 3).

One TCP session per direct link (peer_node, peer_nic, my_nic). UDP hello
(probe) triggers dial when we are the higher loopback; the lower side listens
on each mesh NIC at ADV_PORT. Keepalive-only liveness; full route table
on connect and whenever the central export changes.
"""
from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .netd import Daemon

# TCP keepalive: ~2s idle → probes → dead ≈ 5–6s (AlmaLinux / RHEL 6.x).
_TCP_KEEPIDLE = 2
_TCP_KEEPINTVL = 1
_TCP_KEEPCNT = 3
_TCP_USER_TIMEOUT_MS = 6000
_MAX_FRAME = 4_000_000


@dataclass
class MeshSession:
    key: tuple
    sock: socket.socket
    role: str
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    last_sent_sig: str = ""
    send_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class MeshListener:
    my_nic: str
    sock: socket.socket
    thread: threading.Thread
    stop_event: threading.Event = field(default_factory=threading.Event)


def _import_netd():
    try:
        from . import netd
    except ImportError:
        import sys as _sys
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import netd  # type: ignore
    return netd


def _loopback_rank(ip: str) -> tuple:
    try:
        parts = [int(x) for x in ip.split(".")]
        if len(parts) == 4:
            return tuple(parts)
    except (ValueError, TypeError):
        pass
    return (0, 0, 0, 0)


def we_dial(our_loopback: str, peer_loopback: str) -> bool:
    """Higher cluster loopback initiates TCP to lower."""
    return _loopback_rank(our_loopback) > _loopback_rank(peer_loopback)


def configure_tcp_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (
        (socket.TCP_KEEPIDLE, _TCP_KEEPIDLE),
        (socket.TCP_KEEPINTVL, _TCP_KEEPINTVL),
        (socket.TCP_KEEPCNT, _TCP_KEEPCNT),
    ):
        sock.setsockopt(socket.IPPROTO_TCP, opt, val)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT,
                    _TCP_USER_TIMEOUT_MS)


def _paths_signature(paths: list) -> str:
    import json
    return json.dumps(paths, sort_keys=True, separators=(",", ":"))


def _send_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def _find_session_key(d: Daemon, my_nic: str, peer_link_addr: str) -> Optional[tuple]:
    for key, n in d.neighbours.items():
        if key[2] == my_nic and n.peer_link_addr == peer_link_addr:
            return key
    return None


def process_route_message(d: Daemon, session_key: tuple,
                          body: dict, now_ts: float) -> bool:
    peer_node, _peer_nic, _my_nic = session_key
    advertiser = body.get("advertiser") or ""
    if advertiser != peer_node:
        return False
    paths = body.get("paths")
    if not isinstance(paths, list):
        return False
    prev = (d.session_table.get(session_key) or {}).get("paths")
    with d.rib_lock:
        d.session_table[session_key] = {
            "paths": paths,
            "ts_local": now_ts,
            "advertiser": advertiser,
        }
    return prev != paths


def _session_ended(d: Daemon, key: tuple) -> None:
    netd = _import_netd()
    with d.rib_lock:
        if key not in d.mesh_sessions and key not in d.session_table:
            return
        sess = d.mesh_sessions.pop(key, None)
        d.session_table.pop(key, None)
        d.mesh_connecting.discard(key)
    if sess is not None:
        sess.stop_event.set()
        try:
            sess.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sess.sock.close()
        except OSError:
            pass
    netd.recompute_best_transit_paths(d, time.time())


def _session_reader(d: Daemon, sess: MeshSession) -> None:
    netd = _import_netd()
    sock = sess.sock
    buf = b""
    try:
        while not sess.stop_event.is_set():
            try:
                chunk = sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= 4:
                flen = struct.unpack("!I", buf[:4])[0]
                if flen > _MAX_FRAME:
                    return
                if len(buf) < 4 + flen:
                    break
                frame = buf[4:4 + flen]
                buf = buf[4 + flen:]
                body = netd.decode_advertisement(frame, key=d.cluster_key)
                if not body:
                    continue
                if body.get("cluster_uuid") != d.cluster_uuid:
                    continue
                if body.get("advertiser") == d.my_node:
                    continue
                if process_route_message(d, sess.key, body, time.time()):
                    netd.recompute_best_transit_paths(d, time.time())
    finally:
        _session_ended(d, sess.key)


def _attach_session(d: Daemon, key: tuple, sock: socket.socket,
                    role: str) -> None:
    d.mesh_connecting.discard(key)
    if key in d.mesh_sessions:
        try:
            sock.close()
        except OSError:
            pass
        return
    configure_tcp_keepalive(sock)
    sess = MeshSession(key=key, sock=sock, role=role)
    d.mesh_sessions[key] = sess
    t = threading.Thread(
        target=_session_reader,
        args=(d, sess),
        name=f"bedrock-mesh-{key[0]}-{key[2]}",
        daemon=True,
    )
    sess.thread = t
    t.start()


def _dial_worker(d: Daemon, key: tuple, peer_link: str,
                 my_nic: str, local_addr: str) -> None:
    netd = _import_netd()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        configure_tcp_keepalive(sock)
        sock.bind((local_addr, 0))
        sock.settimeout(8.0)
        sock.connect((peer_link, netd.ADV_PORT))
        sock.settimeout(None)
        _attach_session(d, key, sock, "client")
    except Exception:
        d.mesh_connecting.discard(key)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _listener_worker(d: Daemon, my_nic: str, listen_sock: socket.socket,
                     stop_ev: threading.Event) -> None:
    while not stop_ev.is_set():
        try:
            listen_sock.settimeout(1.0)
            conn, addr = listen_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        key = _find_session_key(d, my_nic, addr[0])
        if key is None:
            try:
                conn.close()
            except OSError:
                pass
            continue
        if key in d.mesh_sessions or key in d.mesh_connecting:
            try:
                conn.close()
            except OSError:
                pass
            continue
        _attach_session(d, key, conn, "server")


def _open_listener(local_addr: str) -> socket.socket:
    netd = _import_netd()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    configure_tcp_keepalive(s)
    s.bind((local_addr, netd.ADV_PORT))
    s.listen(8)
    return s


def reconcile_listeners(d: Daemon) -> None:
    """Ensure one accept socket per mesh NIC with an IPv4 address."""
    want = set(d.nic_addrs.keys())
    for my_nic in list(d.mesh_listeners.keys()):
        addr = d.nic_addrs.get(my_nic, "")
        if my_nic not in want or not addr:
            lst = d.mesh_listeners.pop(my_nic)
            lst.stop_event.set()
            try:
                lst.sock.close()
            except OSError:
                pass
    for my_nic, local_addr in d.nic_addrs.items():
        if not local_addr or my_nic in d.mesh_listeners:
            continue
        try:
            sock = _open_listener(local_addr)
        except OSError as e:
            sys.stderr.write(
                f"bedrock-net: mesh TCP listen on {my_nic} ({local_addr}) "
                f"failed: {e!r}\n"
            )
            continue
        stop_ev = threading.Event()
        t = threading.Thread(
            target=_listener_worker,
            args=(d, my_nic, sock, stop_ev),
            name=f"bedrock-mesh-listen-{my_nic}",
            daemon=True,
        )
        d.mesh_listeners[my_nic] = MeshListener(
            my_nic=my_nic, sock=sock, thread=t, stop_event=stop_ev,
        )
        t.start()


def mesh_maybe_connect(d: Daemon, peer_node: str, peer_nic: str,
                       my_nic: str, peer_loopback: str,
                       peer_link_addr: str) -> None:
    """Hello seen: higher loopback dials peer on this adjacency if no session."""
    if not d.my_loopback or not peer_loopback or not peer_link_addr:
        return
    if peer_node == d.my_node:
        return
    key = (peer_node, peer_nic, my_nic)
    if key in d.mesh_sessions or key in d.mesh_connecting:
        return
    if not we_dial(d.my_loopback, peer_loopback):
        return
    local = d.nic_addrs.get(my_nic, "")
    if not local:
        return
    d.mesh_connecting.add(key)
    threading.Thread(
        target=_dial_worker,
        args=(d, key, peer_link_addr, my_nic, local),
        name=f"bedrock-mesh-dial-{peer_node}-{my_nic}",
        daemon=True,
    ).start()


def _build_export(d: Daemon) -> tuple[list, str]:
    netd = _import_netd()
    paths = netd.build_advertisement_paths(d)
    return paths, _paths_signature(paths)


def _push_session(d: Daemon, sess: MeshSession, now_ts: float) -> None:
    netd = _import_netd()
    paths, sig = _build_export(d)
    if sess.last_sent_sig == sig:
        return
    d.route_adv_seq = (d.route_adv_seq + 1) & 0xFFFFFFFF
    buf = netd.encode_advertisement(
        cluster_uuid=d.cluster_uuid,
        advertiser=d.my_node,
        seq=d.route_adv_seq,
        ts=now_ts,
        paths=paths,
        key=d.cluster_key,
    )
    try:
        with sess.send_lock:
            _send_frame(sess.sock, buf)
        sess.last_sent_sig = sig
    except OSError:
        _session_ended(d, sess.key)


def route_sessions_push(d: Daemon, now_ts: float) -> None:
    """Push full route table to every live adjacency when export changed."""
    for sess in list(d.mesh_sessions.values()):
        _push_session(d, sess, now_ts)


def stop_all(d: Daemon) -> None:
    for lst in list(d.mesh_listeners.values()):
        lst.stop_event.set()
        try:
            lst.sock.close()
        except OSError:
            pass
    d.mesh_listeners.clear()
    for key in list(d.mesh_sessions.keys()):
        _session_ended(d, key)
