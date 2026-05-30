"""Echo-by-IP: netd unicast-probes CONFIGURED Echo witness addresses so an
Echo added by IP (routed / off the broadcast domain) still gets probed + can
vote. Locks in the address parsing + that unicast_probe targets exactly the
configured (host, port) pairs (broadcast_probe only reaches the local L2).
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "installer"))

from lib import witness as w          # noqa: E402
from lib.netd import _parse_echo_addr  # noqa: E402


def test_parse_echo_addr_accepts_only_ipv4_unicast():
    # Valid IPv4 unicast literals (the only thing the directed probe may target)
    assert _parse_echo_addr("192.168.9.50") == ("192.168.9.50", 12321)
    assert _parse_echo_addr("192.168.9.50:12321") == ("192.168.9.50", 12321)
    assert _parse_echo_addr("10.0.0.9:9999") == ("10.0.0.9", 9999)
    # Rejected — return None so the election tick never sends to a bad target:
    rejects = [
        "",                  # empty
        "host:9999",         # HOSTNAME → would block the tick on DNS
        "echo.lan",          # hostname, no port
        "fe80::1",           # IPv6 → unreachable on AF_INET
        "[fe80::1]:5000",    # bracketed IPv6
        "224.0.0.1",         # multicast → would flood
        "255.255.255.255",   # broadcast
        "0.0.0.0",           # unspecified
        "127.0.0.1",         # loopback
        "169.254.1.1",       # link-local
        "192.168.9.50:99999",  # port out of range
        "192.168.9.50:0",    # port 0
        "192.168.9.50:x",    # bad port
        "a:b:c",             # garbage
    ]
    for bad in rejects:
        assert _parse_echo_addr(bad) is None, bad


def _ws():
    return w.WitnessState(cluster_uuid="0123456789abcdef",
                          cluster_key=b"\x01" * 32, my_node_id=1)


def test_unicast_probe_targets_exactly_the_configured_endpoints():
    ws = _ws()
    sent = []

    class FakeSock:
        def sendto(self, _pkt, addr):
            sent.append(addr)

    ws.sock = FakeSock()
    w.unicast_probe(ws, [("192.168.9.50", 12321), ("10.0.0.9", 9999)])
    assert sent == [("192.168.9.50", 12321), ("10.0.0.9", 9999)]


def test_unicast_probe_skips_bad_endpoints_without_raising():
    ws = _ws()
    sent = []

    class FakeSock:
        def sendto(self, _pkt, addr):
            sent.append(addr)

    ws.sock = FakeSock()
    # a malformed tuple must be skipped, the good ones still sent
    w.unicast_probe(ws, [("ok", 12321), ("missing-port",), None])
    assert sent == [("ok", 12321)]


def test_unicast_probe_noop_without_socket():
    ws = _ws()                # sock is None
    w.unicast_probe(ws, [("1.2.3.4", 12321)])   # must not raise


if __name__ == "__main__":
    test_parse_echo_addr_accepts_only_ipv4_unicast()
    test_unicast_probe_targets_exactly_the_configured_endpoints()
    test_unicast_probe_skips_bad_endpoints_without_raising()
    test_unicast_probe_noop_without_socket()
    print("✓ witness unicast/parse tests passed")
