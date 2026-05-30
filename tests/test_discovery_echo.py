"""mDNS discovery of BedRock Echo witnesses (discovery.discover_echo_witnesses
+ the codec generalization). Parsing is tested against synthetic mDNS response
bytes — no live multicast needed; the stub-advertising + live path is e2e."""
from __future__ import annotations

import socket
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import discovery  # noqa: E402


def _mk_response(name: bytes, ip: str, txt_pairs: dict) -> bytes:
    """Build a minimal mDNS RESPONSE with one A record + one TXT record for
    ``name`` (a dotted DNS name like b'bedrock-echo.local')."""
    wire_name = discovery._encode_qname(name)
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 0)   # response, 2 answers
    a_rr = wire_name + struct.pack("!HHIH", 1, 1, 120, 4) + socket.inet_aton(ip)
    txt_rdata = bytearray()
    for k, v in txt_pairs.items():
        s = f"{k}={v}".encode()
        txt_rdata.append(len(s))
        txt_rdata += s
    txt_rr = wire_name + struct.pack("!HHIH", 16, 1, 120, len(txt_rdata)) + bytes(txt_rdata)
    return header + a_rr + txt_rr


# ── codec generalization (must not change cluster discovery) ─────────────

def test_default_query_is_bedrock_local_unchanged():
    # Regression: the joiner's cluster-discovery query is byte-identical.
    assert b"\x07bedrock\x05local\x00" in discovery._build_mdns_query()


def test_echo_query_targets_the_echo_name():
    q = discovery._build_mdns_query(qtype=255, qname=discovery.ECHO_MDNS_NAME)
    assert b"\x0cbedrock-echo\x05local\x00" in q


def test_encode_qname_roundtrips_labels():
    assert discovery._encode_qname(b"bedrock-echo.local") == b"\x0cbedrock-echo\x05local\x00"
    assert discovery._encode_qname(b"bedrock.local") == b"\x07bedrock\x05local\x00"


# ── parse + discover_echo_witnesses ──────────────────────────────────────

def test_parse_extracts_echo_ip_id_pubkey():
    resp = _mk_response(discovery.ECHO_MDNS_NAME, "10.0.0.9",
                        {"echo_id": "echo-rack-1", "pubkey": "ab" * 32})
    parsed = discovery._parse_mdns_response(resp, expect_name=discovery.ECHO_MDNS_NAME)
    assert parsed["ips"] == ["10.0.0.9"]
    assert parsed["txt"]["echo_id"] == "echo-rack-1"
    assert parsed["txt"]["pubkey"] == "ab" * 32


def test_parse_filters_by_expected_name():
    # An Echo response must NOT match when we asked for bedrock.local, and a
    # cluster response must NOT match when we asked for bedrock-echo.local.
    echo = _mk_response(discovery.ECHO_MDNS_NAME, "10.0.0.9", {"echo_id": "e1"})
    assert discovery._parse_mdns_response(echo, expect_name=discovery.MDNS_NAME)["ips"] == []
    cluster = _mk_response(discovery.MDNS_NAME, "10.0.0.5", {"cluster_uuid": "u"})
    assert discovery._parse_mdns_response(cluster, expect_name=discovery.ECHO_MDNS_NAME)["ips"] == []


def test_echo_candidate_label():
    c = discovery.EchoCandidate(ip="10.0.0.9", echo_id="e1", pubkey="ab" * 32)
    assert c.label() == "e1 @ 10.0.0.9"
    assert discovery.EchoCandidate(ip="10.0.0.9").label() == "? @ 10.0.0.9"
