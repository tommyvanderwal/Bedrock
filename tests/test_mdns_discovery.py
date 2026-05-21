"""Tests for mDNS discovery + TXT round-trip.

The wire-level functions in installer/lib/mdns_responder.py and
installer/lib/discovery.py have to agree on the binary format —
this test pins that interop.
"""
from __future__ import annotations

import pathlib
import socket
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]
                       / "installer"))

from lib import discovery as _d
from lib import mdns_responder as _r


# ───────────────────────────────────────────────────────────────────
# TXT rdata encoding/parsing round-trip
# ───────────────────────────────────────────────────────────────────


def test_txt_rdata_roundtrips_identity():
    identity = {
        "cluster_uuid": "abc-def-123-456",
        "cluster_name": "test-cluster",
        "node_name": "bedrock-deadbeef",
    }
    rdata = _r._txt_rdata(identity)
    parsed = _d._parse_txt_rdata(rdata)
    assert parsed == identity


def test_txt_rdata_skips_missing_fields():
    rdata = _r._txt_rdata({"cluster_uuid": "x"})
    parsed = _d._parse_txt_rdata(rdata)
    assert parsed == {"cluster_uuid": "x"}


def test_txt_rdata_empty_identity_is_single_zero_string():
    """RFC 6763 §6.1: empty TXT is one zero-length string, NOT empty
    rdata. Distinguishes 'no records' from 'name doesn't exist'."""
    rdata = _r._txt_rdata({})
    assert rdata == b"\x00"


# ───────────────────────────────────────────────────────────────────
# A + TXT response round-trip
# ───────────────────────────────────────────────────────────────────


def _make_response(qtype: int, ip: str, identity: dict) -> bytes:
    ip_bytes = socket.inet_aton(ip) if ip else b""
    return _r.build_response(qtype, ip_bytes, identity)


def test_a_response_parses_back_to_ip():
    identity = {"cluster_uuid": "u1", "cluster_name": "c1"}
    resp = _make_response(_r.TYPE_A, "192.168.2.37", identity)
    parsed = _d._parse_mdns_response(resp)
    assert parsed["ips"] == ["192.168.2.37"]
    # A-only response: TXT was not requested
    assert parsed["txt"] == {}


def test_txt_response_parses_back_to_identity():
    identity = {"cluster_uuid": "uuu", "cluster_name": "prod",
                "node_name": "bedrock-7dd990"}
    resp = _make_response(_r.TYPE_TXT, "10.0.0.1", identity)
    parsed = _d._parse_mdns_response(resp)
    # TXT-only response: no A records
    assert parsed["ips"] == []
    assert parsed["txt"] == identity


def test_any_response_carries_both():
    identity = {"cluster_uuid": "U", "cluster_name": "N"}
    resp = _make_response(_r.TYPE_ANY, "172.16.0.5", identity)
    parsed = _d._parse_mdns_response(resp)
    assert parsed["ips"] == ["172.16.0.5"]
    assert parsed["txt"] == identity


def test_response_carries_single_a_per_query():
    """Per the IP_PKTINFO design: responder replies with ONE A
    record — the IP on the interface the query arrived on. Joiner
    naturally sees a different IP per network path it queries on.
    No more multi-A flood + sort + TCP-probe."""
    identity = {"cluster_uuid": "uX", "cluster_name": "n"}
    resp = _r.build_response(_r.TYPE_ANY, socket.inet_aton("192.168.2.37"),
                             identity)
    parsed = _d._parse_mdns_response(resp)
    assert parsed["ips"] == ["192.168.2.37"]
    assert parsed["txt"] == identity


# ───────────────────────────────────────────────────────────────────
# Query parsing (what the responder sees)
# ───────────────────────────────────────────────────────────────────


def test_responder_recognises_query_for_bedrock_local_A():
    query = _d._build_mdns_query(qtype=_r.TYPE_A)
    qtype, qclass = _r.parse_question(query)
    assert qtype == _r.TYPE_A
    assert qclass == 1


def test_responder_recognises_query_for_bedrock_local_TXT():
    query = _d._build_mdns_query(qtype=_r.TYPE_TXT)
    qtype, qclass = _r.parse_question(query)
    assert qtype == _r.TYPE_TXT


def test_responder_recognises_query_for_ANY():
    query = _d._build_mdns_query(qtype=255)
    qtype, qclass = _r.parse_question(query)
    assert qtype == 255


def test_responder_ignores_query_for_other_name():
    # Build a query for `not-bedrock.local`
    header = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    qname = b"\x0bnot-bedrock\x05local\x00"
    qrest = struct.pack("!HH", _r.TYPE_A, 1)
    query = header + qname + qrest
    qtype, qclass = _r.parse_question(query)
    assert qtype is None and qclass is None


# ───────────────────────────────────────────────────────────────────
# ClusterCandidate label
# ───────────────────────────────────────────────────────────────────


def test_cluster_candidate_label_includes_all_known_fields():
    c = _d.ClusterCandidate(
        ip="192.168.2.37",
        cluster_uuid="abc12345-deadbeef",
        cluster_name="prod",
        node_name="bedrock-7dd990",
    )
    label = c.label()
    assert "192.168.2.37" in label
    assert "prod" in label
    assert "abc12345" in label  # truncated uuid prefix
    assert "bedrock-7dd990" in label


def test_cluster_candidate_label_handles_empty_fields():
    c = _d.ClusterCandidate(ip="10.0.0.1")
    label = c.label()
    assert "10.0.0.1" in label
    # No crash on empty cluster_name / uuid




# ───────────────────────────────────────────────────────────────────
# IP_PKTINFO-based per-query address selection
# ───────────────────────────────────────────────────────────────────


def test_interface_map_excludes_loopback_and_cluster_32():
    """_interface_ipv4_map filters 127.x and 100.X.Y.X/32 cluster
    identity addresses. We can't fully mock without subprocess
    shimming; just smoke-test the public output."""
    m = _r._interface_ipv4_map()
    for ip in m.values():
        assert not ip.startswith("127."), f"loopback leaked: {ip}"


def test_response_no_address_a_only_yields_empty():
    """A-only query with no usable IP: nothing to send."""
    resp = _r.build_response(_r.TYPE_A, b"", {"cluster_uuid": "u"})
    assert resp == b""


def test_response_no_address_any_still_emits_txt():
    """ANY query with no usable IP: TXT-only response so the
    operator at least sees the cluster exists."""
    identity = {"cluster_uuid": "u", "cluster_name": "n"}
    resp = _r.build_response(_r.TYPE_ANY, b"", identity)
    parsed = _d._parse_mdns_response(resp)
    assert parsed["ips"] == []
    assert parsed["txt"] == identity


def test_first_reachable_returns_none_when_all_unreachable():
    cs = [
        _d.ClusterCandidate(ip="192.0.2.1"),   # TEST-NET-1, RFC 5737
        _d.ClusterCandidate(ip="198.51.100.2"),  # TEST-NET-2
    ]
    assert _d.first_reachable(cs, timeout=0.2) is None

