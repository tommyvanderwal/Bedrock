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


def test_parse_echo_addr_forms():
    assert _parse_echo_addr("192.168.9.50") == ("192.168.9.50", 12321)
    assert _parse_echo_addr("192.168.9.50:12321") == ("192.168.9.50", 12321)
    assert _parse_echo_addr("host:9999") == ("host", 9999)
    assert _parse_echo_addr("[fe80::1]:5000") == ("fe80::1", 5000)
    assert _parse_echo_addr("fe80::1") == ("fe80::1", 12321)   # bare IPv6
    # rejects (return None — skipped, never probed with a garbage target)
    for bad in ("", "  ", "bad:x", "h:99999", "h:0", "h:-1", "[::1", "[::1]:x"):
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
    test_parse_echo_addr_forms()
    test_unicast_probe_targets_exactly_the_configured_endpoints()
    test_unicast_probe_skips_bad_endpoints_without_raising()
    test_unicast_probe_noop_without_socket()
    print("✓ witness unicast/parse tests passed")
