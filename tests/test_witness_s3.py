"""witness_s3 — the native S3 witness backend.

The load-bearing test is SigV4 correctness: we reproduce AWS's OWN published
"GET Object" example signature byte-for-byte, so a real S3/MinIO/SeaweedFS will
accept our requests. The rest exercises the slot protocol over an in-memory fake
(no network) and the count_valid_confirmed fold that lets an S3 witness vote.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "installer"))

from lib import witness                 # noqa: E402
from lib import witness_s3 as w3        # noqa: E402


# ─────────────────────────────────────────────────────────────────
#  SigV4 — the OFFICIAL aws-sig-v4-test-suite "get-vanilla" vector
#  (AKIDEXAMPLE / wJalrXUtnFEMI…, us-east-1, service 'service',
#  20150830T123600Z). This is THE unambiguous SigV4 conformance vector;
#  reproducing its signature byte-for-byte proves our signing is wire-correct.
#  (The S3 doc-page "GET Object" example uses a redacted/different secret, so
#  it can't be reproduced from the well-known AKIAIOSFODNN7EXAMPLE pair — we
#  validate the algorithm against the canonical suite instead.)
# ─────────────────────────────────────────────────────────────────
def test_sigv4_matches_aws_test_suite_get_vanilla():
    auth = w3._authorization(
        "GET", "/", {},
        {"host": "example.amazonaws.com", "x-amz-date": "20150830T123600Z"},
        w3._EMPTY_SHA256, region="us-east-1", access_key="AKIDEXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        amz_date="20150830T123600Z", service="service")
    assert "SignedHeaders=host;x-amz-date" in auth
    assert ("Signature="
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
            in auth)
    assert auth.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
        "aws4_request")


def test_sigv4_s3_service_scope_is_wellformed():
    """The production path uses service='s3' (default); confirm the scope +
    signed-headers shape it emits is correct (crypto proven by get-vanilla)."""
    auth = w3._authorization(
        "PUT", "/bk/w/slot-1.bin", {},
        {"host": "minio.lan:9000", "x-amz-content-sha256": w3._EMPTY_SHA256,
         "x-amz-date": "20240101T000000Z"},
        w3._EMPTY_SHA256, region="eu-central-1", access_key="AK",
        secret_key="SK", amz_date="20240101T000000Z")
    assert "/20240101/eu-central-1/s3/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth


def test_empty_sha256_constant_is_correct():
    import hashlib
    assert w3._EMPTY_SHA256 == hashlib.sha256(b"").hexdigest()


def test_uri_encode_keeps_slash_in_path_encodes_in_query():
    assert w3._uri_encode("a/b c", encode_slash=False) == "a/b%20c"
    assert w3._uri_encode("a/b c", encode_slash=True) == "a%2Fb%20c"
    # unreserved set stays literal
    assert w3._uri_encode("Az9-_.~", encode_slash=False) == "Az9-_.~"


# ─────────────────────────────────────────────────────────────────
#  Config parsing
# ─────────────────────────────────────────────────────────────────
def test_config_from_endpoint_infers_scheme_and_normalises_prefix():
    cfg = w3.S3Config.from_endpoint(
        {"s3_endpoint": "minio.lan:9000", "s3_bucket": "bk",
         "s3_prefix": "wit/cluster", "s3_access_key": "AK",
         "s3_disable_tls": 1}, "SK")
    assert cfg.endpoint == "http://minio.lan:9000"   # disable_tls → http
    assert cfg.prefix == "wit/cluster/"              # trailing slash added
    assert cfg.secret_key == "SK"
    assert cfg.verify_tls is True


def test_config_from_endpoint_https_default_and_verify_optout():
    cfg = w3.S3Config.from_endpoint(
        {"s3_endpoint": "s3.example.com", "s3_bucket": "bk",
         "s3_disable_tls_verification": 1}, "SK")
    assert cfg.endpoint == "https://s3.example.com"  # default https
    assert cfg.verify_tls is False
    assert cfg.region == "us-east-1"                 # default region


def test_path_style_canonical_uri():
    cfg = w3.S3Config(endpoint="https://s3.example.com", bucket="bk",
                      access_key="AK", secret_key="SK", prefix="p/")
    c = w3.S3Client(cfg)
    assert c._canonical_uri("p/slot-1.bin") == "/bk/p/slot-1.bin"
    assert c._canonical_uri("") == "/bk"


# ─────────────────────────────────────────────────────────────────
#  Slot keys
# ─────────────────────────────────────────────────────────────────
def test_slot_key_and_recognition():
    assert w3.slot_key("p/", 7) == "p/slot-7.bin"
    assert w3.slot_key("", 7) == "slot-7.bin"
    assert w3._is_slot_key("p/slot-7.bin", "p/")
    assert not w3._is_slot_key("p/other.bin", "p/")
    assert not w3._is_slot_key("q/slot-7.bin", "p/")   # wrong prefix


# ─────────────────────────────────────────────────────────────────
#  Slot protocol over an in-memory fake S3 (no network)
# ─────────────────────────────────────────────────────────────────
class _FakeCfg:
    prefix = "w/"


class _FakeS3:
    """Implements just the S3Client surface the slot protocol calls."""
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.cfg = _FakeCfg()

    def put_object(self, key, data):
        self.store[key] = data

    def get_object(self, key):
        return self.store.get(key)

    def delete_object(self, key):
        self.store.pop(key, None)

    def list_keys(self, prefix=""):
        return [k for k in self.store if k.startswith(prefix)]


def _ws(node_id: int, key: bytes) -> witness.WitnessState:
    ws = witness.WitnessState(
        cluster_uuid="c" * 16, cluster_key=key, my_node_id=node_id)
    ws.member_ids = {1, 2}
    ws.own_marker = bytes([node_id]) * 16   # a stable per-node marker
    return ws


def test_write_read_roundtrip_and_valid_confirmed():
    key = b"k" * 32
    fake = _FakeS3()
    now = int(time.time() * 1000)
    n1, n2 = _ws(1, key), _ws(2, key)
    # both nodes write their own slot to the SAME store
    w3.write_own_slot(n1, fake, now_ms=now)
    w3.write_own_slot(n2, fake, now_ms=now)
    assert set(fake.store) == {"w/slot-1.bin", "w/slot-2.bin"}
    # node 1 reads → sees both members' slots
    slots = w3.read_slots(n1, fake)
    assert set(slots) == {1, 2}
    # valid (slot for every member) + confirmed (own readback matches, fresh)
    assert w3.is_valid_confirmed(n1, fake, now_local_ms=now) is True


def test_not_valid_when_a_member_slot_missing():
    key = b"k" * 32
    fake = _FakeS3()
    now = int(time.time() * 1000)
    n1 = _ws(1, key)
    w3.write_own_slot(n1, fake, now_ms=now)        # only node 1's slot exists
    # member_ids={1,2} but node 2's slot absent → not valid
    assert w3.is_valid_confirmed(n1, fake, now_local_ms=now) is False


def test_foreign_cluster_key_blob_is_dropped():
    fake = _FakeS3()
    now = int(time.time() * 1000)
    n1 = _ws(1, b"k" * 32)
    # a slot written under a DIFFERENT cluster key must not decode/count
    other = _ws(2, b"x" * 32)
    w3.write_own_slot(other, fake, now_ms=now)
    assert w3.read_slots(n1, fake) == {}            # AEAD-invalid → dropped


def test_own_readback_ok_detects_a_lying_store():
    key = b"k" * 32
    now = int(time.time() * 1000)
    n1 = _ws(1, key)

    class _Amnesiac(_FakeS3):
        def put_object(self, key, data):
            pass                                    # silently drops the write

    good = _FakeS3()
    assert w3.own_readback_ok(n1, good, now_ms=now) is True
    assert w3.own_readback_ok(n1, _Amnesiac(), now_ms=now) is False


# ─────────────────────────────────────────────────────────────────
#  The tally fold — an S3 witness verdict must be able to vote
# ─────────────────────────────────────────────────────────────────
def _fresh_verdict(wid, ok=True):
    return witness.FileWitnessVerdict(
        witness_id=wid, valid_confirmed=ok, evaluated_monotonic=time.monotonic())


def test_s3_verdict_counts_in_tally():
    ws = witness.WitnessState(cluster_uuid="c" * 16, cluster_key=b"k" * 32,
                              my_node_id=1)
    ws.member_ids = {1, 2}
    ws.configured_witness_ids = {"s3a"}
    ws.s3_witnesses = {"s3a": _fresh_verdict("s3a")}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_s3_and_file_same_id_counts_once():
    ws = witness.WitnessState(cluster_uuid="c" * 16, cluster_key=b"k" * 32,
                              my_node_id=1)
    ws.member_ids = {1, 2}
    ws.configured_witness_ids = {"dup"}
    ws.file_witnesses = {"dup": _fresh_verdict("dup")}
    ws.s3_witnesses = {"dup": _fresh_verdict("dup")}
    # one distinct witness → tally 1, never inflated to 2
    assert witness.count_valid_confirmed(ws, n_configured=1) == 1


def test_stale_s3_verdict_does_not_count():
    ws = witness.WitnessState(cluster_uuid="c" * 16, cluster_key=b"k" * 32,
                              my_node_id=1)
    ws.member_ids = {1, 2}
    ws.configured_witness_ids = {"s3a"}
    stale = witness.FileWitnessVerdict(
        witness_id="s3a", valid_confirmed=True,
        evaluated_monotonic=time.monotonic() - witness.WITNESS_FRESHNESS_S - 5)
    ws.s3_witnesses = {"s3a": stale}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0


def test_unconfigured_s3_witness_does_not_count():
    ws = witness.WitnessState(cluster_uuid="c" * 16, cluster_key=b"k" * 32,
                              my_node_id=1)
    ws.member_ids = {1, 2}
    ws.configured_witness_ids = {"other"}           # 's3a' is NOT configured
    ws.s3_witnesses = {"s3a": _fresh_verdict("s3a")}
    assert witness.count_valid_confirmed(ws, n_configured=1) == 0
