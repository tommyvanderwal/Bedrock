"""Native S3 witness backend — the slot protocol over an S3 bucket/prefix.

An S3 witness is a bucket+prefix on an S3-compatible store that EVERY node can
reach (AWS S3, MinIO, SeaweedFS, Ceph RGW, Backblaze B2's S3 API …). It plays
the SAME role as a fileshare witness, except the central slot store is S3 object
storage hit DIRECTLY over HTTP — no mount, no kernel client:

  * each node PUTs its OWN slot to ``<prefix>slot-<node_id>.bin`` (a single PUT
    is the durability barrier — S3 returns 200 only once the object is durable
    and immediately readable; there is no client-side write-back to lose);
  * each node LISTs the prefix and GETs every ``slot-*.bin`` to learn the others.

The slot bytes are the EXACT AEAD-sealed blob the Echo / fileshare backends
store (``witness._encode_slot`` / ``_decode_slot``), so ``_slots_valid`` /
``_slots_confirmed`` and the whole quorum math apply UNCHANGED. An S3 witness is
"valid + confirmed" under identical rules: the prefix holds a fresh slot for
every active member AND this node's own slot reads back with our current marker.

WHY NATIVE (no boto3 / rclone): the witness needs four verbs — PUT, GET, DELETE,
LIST — of one tiny (<1 KiB) object. That is ~one screen of SigV4 over stdlib
``urllib`` (the house HTTP style: peer_auth, discovery, cert_manager, tier_storage
all use urllib). boto3 is framework creep; a bundled rclone Go binary is a second
runtime. So this module signs requests itself.

ADDRESSING: PATH-STYLE (``<endpoint>/<bucket>/<key>``) by default — it is what
MinIO/SeaweedFS/Ceph want and AWS still accepts. SigV4, region-aware, with an
opt-out for TLS verification (self-signed in-house S3 is common; see the kopia
E2E lesson). NFSv2-style size limits do not apply — slots are tiny.

CONSISTENCY: a witness needs read-after-own-write (R1) + read-sees-peer's-
committed-write (R2). AWS S3 has been strongly read-after-write consistent for
all operations since Dec-2020 (incl. LIST); a SINGLE-filer SeaweedFS / single
MinIO is strongly consistent too. The own-readback health check (run every
minute by netd) is exactly what catches a store that violates this and flags it.

This module is TRANSPORT-ONLY: no netd/election coupling. Wiring it into the
off-hot-path slot-IO worker (S3 latency must never stall the 1 Hz election tick)
is ``run_io_cycle`` + the ``ws.configured_s3_witnesses`` field, mirroring the
fileshare backend.
"""
from __future__ import annotations

import hashlib
import hmac
import socket
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import quote, urlparse

try:                       # imported both as a package and bare on sys.path
    from . import witness   # type: ignore  # slot codec + validity predicates
except ImportError:         # pragma: no cover
    import witness          # type: ignore


_SLOT_PREFIX = "slot-"
_SLOT_SUFFIX = ".bin"
_SERVICE = "s3"
_ALGORITHM = "AWS4-HMAC-SHA256"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
DEFAULT_TIMEOUT_S = 15     # one slot op; the worker is off the hot path anyway


# ─────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────
@dataclass
class S3Config:
    """Everything needed to address + sign one S3 witness, derived from a
    storage_endpoints row (secret already unsealed by the caller)."""
    endpoint: str                 # scheme+host[:port], e.g. https://s3.eu-central-1.amazonaws.com
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"     # SeaweedFS/MinIO ignore it but SigV4 needs one
    prefix: str = ""              # object-key prefix; '' or 'a/b/' (trailing slash normalised)
    verify_tls: bool = True       # False = accept self-signed (in-house S3)

    def __post_init__(self) -> None:
        self.endpoint = self.endpoint.rstrip("/")
        if self.prefix and not self.prefix.endswith("/"):
            self.prefix += "/"

    @classmethod
    def from_endpoint(cls, ep: dict, secret_key: str) -> "S3Config":
        """Build from a storage_endpoints dict + the (already-unsealed) secret.
        ``ep`` carries s3_endpoint/s3_bucket/s3_region/s3_prefix/
        s3_disable_tls_verification and s3_access_key."""
        endpoint = (ep.get("s3_endpoint") or "").strip()
        if endpoint and "://" not in endpoint:
            # bare host[:port] → honour the disable_tls flag for the scheme
            scheme = "http" if ep.get("s3_disable_tls") else "https"
            endpoint = f"{scheme}://{endpoint}"
        return cls(
            endpoint=endpoint,
            bucket=(ep.get("s3_bucket") or "").strip(),
            access_key=(ep.get("s3_access_key") or "").strip(),
            secret_key=secret_key,
            region=(ep.get("s3_region") or "us-east-1").strip() or "us-east-1",
            prefix=(ep.get("s3_prefix") or "").strip(),
            verify_tls=not bool(ep.get("s3_disable_tls_verification")),
        )


# ─────────────────────────────────────────────────────────────────
#  SigV4 (AWS Signature Version 4) — validated against AWS's published
#  S3 "GET Object" example vector in test_witness_s3.py.
# ─────────────────────────────────────────────────────────────────
def _uri_encode(s: str, *, encode_slash: bool) -> str:
    """RFC-3986 encode for SigV4. Unreserved = A-Za-z0-9-_.~ stay literal;
    '/' stays literal in the path (encode_slash=False) but is encoded in query
    values (encode_slash=True). Mirrors AWS's UriEncode()."""
    safe = "-_.~" + ("" if encode_slash else "/")
    return quote(s, safe=safe)


def _sign_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k_date = _h(("AWS4" + secret_key).encode(), datestamp)
    k_region = _h(k_date, region)
    k_service = _h(k_region, service)
    return _h(k_service, "aws4_request")


def _authorization(method: str, canonical_uri: str, query: Dict[str, str],
                   headers: Dict[str, str], payload_hash: str, *,
                   region: str, access_key: str, secret_key: str,
                   amz_date: str, service: str = _SERVICE) -> str:
    """Compute the SigV4 Authorization header value. ``headers`` are the headers
    to SIGN (host + x-amz-* at minimum); their lowercased names form
    SignedHeaders. ``query`` is the (already-decoded) query map. ``service`` is
    's3' in production; it is a parameter only so the SigV4 algorithm can be
    validated against the official aws-sig-v4-test-suite (service='service')."""
    datestamp = amz_date[:8]
    # Canonical query string: URI-encode keys+values, sort by encoded key.
    canon_query = "&".join(
        f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
        for k, v in sorted(query.items()))
    # Canonical headers: lowercase name, trimmed value, sorted by name, each "n:v\n".
    low = {k.lower(): str(v).strip() for k, v in headers.items()}
    canon_headers = "".join(f"{k}:{low[k]}\n" for k in sorted(low))
    signed_headers = ";".join(sorted(low))
    canonical_request = "\n".join([
        method, canonical_uri, canon_query, canon_headers, signed_headers,
        payload_hash])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        _ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest()])
    signature = hmac.new(
        _sign_key(secret_key, datestamp, region, service),
        string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"{_ALGORITHM} Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")


# ─────────────────────────────────────────────────────────────────
#  S3 client (PUT / GET / DELETE / LIST) — exactly what the witness needs
# ─────────────────────────────────────────────────────────────────
class S3Error(Exception):
    """An S3 request failed (non-2xx, network, or parse). Fail-loud — the caller
    treats a witness it can't reach/write as not-certifying (the safe direction)."""


class S3Client:
    def __init__(self, cfg: S3Config, *, timeout: float = DEFAULT_TIMEOUT_S):
        if not cfg.endpoint or not cfg.bucket:
            raise S3Error("s3 witness needs endpoint + bucket")
        self.cfg = cfg
        self.timeout = timeout
        self._host = urlparse(cfg.endpoint).netloc
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        if cfg.endpoint.startswith("https") and not cfg.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    # path-style: /<bucket>/<key>
    def _canonical_uri(self, key: str) -> str:
        # key may contain '/'; keep them literal, encode each segment's chars.
        enc_key = _uri_encode(key, encode_slash=False)
        return f"/{self.cfg.bucket}/{enc_key}" if key else f"/{self.cfg.bucket}"

    def _request(self, method: str, key: str = "", *, query: Optional[dict] = None,
                 body: bytes = b"") -> bytes:
        query = query or {}
        payload_hash = hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256
        amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        headers = {
            "host": self._host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        canonical_uri = self._canonical_uri(key)
        headers["Authorization"] = _authorization(
            method, canonical_uri, query, headers, payload_hash,
            region=self.cfg.region, access_key=self.cfg.access_key,
            secret_key=self.cfg.secret_key, amz_date=amz_date)
        url = self.cfg.endpoint + canonical_uri
        if query:
            url += "?" + "&".join(
                f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
                for k, v in sorted(query.items()))
        req = urllib.request.Request(url, data=body or None, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ssl_ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise S3Error(f"{method} {key or '(bucket)'} -> HTTP {e.code}: "
                          f"{e.read()[:200]!r}") from e
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise S3Error(f"{method} {key or '(bucket)'} -> {type(e).__name__}: {e}") from e

    def put_object(self, key: str, data: bytes) -> None:
        self._request("PUT", key, body=data)

    def get_object(self, key: str) -> Optional[bytes]:
        """Bytes, or None if the object does not exist (404)."""
        try:
            return self._request("GET", key)
        except S3Error as e:
            if " HTTP 404" in str(e):
                return None
            raise

    def delete_object(self, key: str) -> None:
        try:
            self._request("DELETE", key)
        except S3Error as e:
            if " HTTP 404" in str(e):
                return            # already gone — idempotent
            raise

    def list_keys(self, prefix: str = "") -> list:
        """All object keys under ``prefix`` (ListObjectsV2, paginated)."""
        keys: list = []
        token = ""
        while True:
            query = {"list-type": "2"}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            body = self._request("GET", "", query=query)
            root = ET.fromstring(body)
            ns = root.tag[: root.tag.find("}") + 1] if "}" in root.tag else ""
            for c in root.findall(f"{ns}Contents"):
                k = c.findtext(f"{ns}Key")
                if k is not None:
                    keys.append(k)
            truncated = (root.findtext(f"{ns}IsTruncated") or "false").lower() == "true"
            token = root.findtext(f"{ns}NextContinuationToken") or ""
            if not truncated or not token:
                break
        return keys


# ─────────────────────────────────────────────────────────────────
#  Slot protocol over S3 (mirrors witness_file)
# ─────────────────────────────────────────────────────────────────
def slot_key(prefix: str, node_id: int) -> str:
    return f"{prefix}{_SLOT_PREFIX}{int(node_id)}{_SLOT_SUFFIX}"


def _is_slot_key(key: str, prefix: str) -> bool:
    if prefix and not key.startswith(prefix):
        return False
    base = key[len(prefix):]
    return base.startswith(_SLOT_PREFIX) and base.endswith(_SLOT_SUFFIX)


def write_own_slot(ws: "witness.WitnessState", client: S3Client,
                   *, now_ms: Optional[int] = None) -> None:
    """PUT THIS node's slot. A single PUT is the durability barrier (S3 returns
    200 only when the object is durable + immediately readable — no torn read,
    no client write-back). Raises (S3Error) on failure — the caller treats an
    un-writable witness as not-certifying (split-brain-safe direction)."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    blob = witness._encode_slot(ws, now_ms)
    client.put_object(slot_key(client.cfg.prefix, ws.my_node_id), blob)


def read_slots(ws: "witness.WitnessState", client: S3Client) -> Dict[int, "witness.Slot"]:
    """LIST the prefix, GET + decode every ``slot-*.bin`` into {node_id: Slot}.
    Mirrors witness_file.read_slots' acceptance rules: AEAD-invalid blobs and
    non-member slots are dropped. Never raises — a list/get failure yields no
    slot for that key (so the witness simply isn't valid this cycle)."""
    slots: Dict[int, "witness.Slot"] = {}
    try:
        keys = client.list_keys(client.cfg.prefix)
    except S3Error:
        return slots
    for key in keys:
        if not _is_slot_key(key, client.cfg.prefix):
            continue
        try:
            blob = client.get_object(key)
        except S3Error:
            continue
        if blob is None:
            continue
        s = witness._decode_slot(ws.cluster_key, blob)
        if s is None:
            continue
        if ws.member_ids is not None and s.node_id not in ws.member_ids:
            continue
        slots[s.node_id] = s
    return slots


def is_valid_confirmed(ws: "witness.WitnessState", client: S3Client,
                       *, now_local_ms: Optional[int] = None) -> bool:
    """True iff this S3 witness is valid AND confirmed right now, under the EXACT
    Echo/fileshare predicates. (Caller writes its own slot first.)"""
    slots = read_slots(ws, client)
    return (witness._slots_valid(ws, slots)
            and witness._slots_confirmed(ws, slots, now_local_ms))


def own_readback_ok(ws: "witness.WitnessState", client: S3Client,
                    *, now_ms: Optional[int] = None) -> bool:
    """The 1-minute health probe (R1): write our own slot, then read it back
    IMMEDIATELY and confirm it carries our current marker + is fresh. If our own
    write isn't visible to our own read, the store is lying/unreliable and the
    caller must flag the witness corrupt — this is the per-node instant check,
    distinct from is_valid_confirmed which also needs every PEER's slot."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    write_own_slot(ws, client, now_ms=now_ms)
    own_key = slot_key(client.cfg.prefix, ws.my_node_id)
    try:
        blob = client.get_object(own_key)
    except S3Error:
        return False
    if blob is None:
        return False
    s = witness._decode_slot(ws.cluster_key, blob)
    if s is None or s.node_id != int(ws.my_node_id):
        return False
    if ws.own_marker and s.marker != ws.own_marker:
        return False
    return not s.is_stale(now_ms)


def probe_writable(cfg: S3Config, *, timeout: float = DEFAULT_TIMEOUT_S) -> str:
    """Add-time UX guard: PUT then GET then DELETE a probe object. Returns "" if
    the bucket+prefix is reachable, writable, AND read-after-write coherent on
    THIS node, else a short human reason. Proves a REAL round-trip (not just a
    PUT 200) so a write-only or eventually-consistent endpoint is caught before
    it is trusted for quorum."""
    try:
        client = S3Client(cfg, timeout=timeout)
    except S3Error as e:
        return str(e)
    probe = f"{cfg.prefix}.bedrock-wprobe"
    token = str(int(time.time() * 1000)).encode() + b"-bedrock-probe"
    try:
        client.put_object(probe, token)
    except S3Error as e:
        return f"write failed ({e})"
    try:
        got = client.get_object(probe)
    except S3Error as e:
        return f"read-back failed ({e})"
    finally:
        try:
            client.delete_object(probe)
        except S3Error:
            pass
    if got != token:
        return "read-after-write mismatch (endpoint is not strongly consistent)"
    return ""


def run_io_cycle(ws: "witness.WitnessState", *,
                 now_ms: Optional[int] = None,
                 now_mono: Optional[float] = None,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 log=None) -> None:
    """One off-hot-path IO pass over the configured S3 witnesses, mirroring
    witness_file.run_io_cycle EXACTLY (per-witness isolation; a transient S3
    error carries the PRIOR verdict forward so a blip ages out over the
    freshness window instead of flipping to False now). Writes verdicts to
    ``ws.s3_witnesses`` via an atomic whole-dict swap (never mutates in place),
    so the 1 Hz election tick — which only READS the dict in
    count_valid_confirmed — never sees a half-built map and never blocks on S3.

    ``ws.configured_s3_witnesses`` is a list of (witness_id, S3Config). Only
    S3Error is caught (the expected failure); anything else propagates to the
    worker loop to be logged loudly (fail-loud)."""
    if now_mono is None:
        now_mono = time.monotonic()
    prior = ws.s3_witnesses
    new_verdicts: Dict[str, "witness.FileWitnessVerdict"] = {}
    for wid, cfg in list(ws.configured_s3_witnesses):
        try:
            client = S3Client(cfg, timeout=timeout)
            write_own_slot(ws, client, now_ms=now_ms)
            ok = is_valid_confirmed(ws, client, now_local_ms=now_ms)
        except S3Error as e:
            if log is not None:
                log(f"s3 witness {wid} ({cfg.endpoint}/{cfg.bucket}) IO error: {e}")
            if wid in prior:
                new_verdicts[wid] = prior[wid]      # carry prior — age out, don't flip
            continue
        new_verdicts[wid] = witness.FileWitnessVerdict(
            witness_id=wid, valid_confirmed=ok, evaluated_monotonic=now_mono)
    ws.s3_witnesses = new_verdicts                  # atomic swap
