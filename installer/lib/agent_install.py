"""Joiner-side install (`bedrock join`).

`install` runs the node_join saga. The join-handshake helpers in this
module (`_request_join`, `_poll_status`, `_install_peer_pubkeys`) are
reused by the saga's steps to register with the cluster's mgmt API and
exchange SSH/peer keys.
"""

import json
import ssl
import time
import urllib.request
from pathlib import Path


_INSECURE_CTX = ssl.create_default_context()
_INSECURE_CTX.check_hostname = False
_INSECURE_CTX.verify_mode = ssl.CERT_NONE


def _http_json(method: str, url: str, body: dict | None = None, timeout: float = 10.0):
    """Plain JSON POST/GET. Self-signed-friendly for HTTPS — the cert
    is for `<dashed-ip>.my.local-ip.co`, never the bare IP we dial."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method.upper(),
        headers={"Content-Type": "application/json"} if data else {})
    kwargs = {"timeout": timeout}
    if url.startswith("https://"):
        kwargs["context"] = _INSECURE_CTX
    with urllib.request.urlopen(req, **kwargs) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _request_join(mgmt_url: str, node_name: str, host: str,
                  bedrock_pubkey: str, x25519_eph_pub_b64: str,
                  ssh_pubkey: str) -> dict:
    body = {
        "node_name": node_name, "host": host,
        "bedrock_pubkey": bedrock_pubkey,
        "x25519_eph_pubkey": x25519_eph_pub_b64,
        "ssh_pubkey": ssh_pubkey,
    }
    # Retry the initial POST through transient errors: master's mgmt
    # service may still be coming up, or mesh routing may be settling.
    # 30s is the failure budget — beyond that the operator should
    # diagnose; we don't sit here forever.
    deadline = time.monotonic() + 30
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _http_json("POST", f"{mgmt_url}/api/join/request", body)
        except (urllib.error.URLError, TimeoutError,
                ConnectionError, OSError) as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"could not reach {mgmt_url} after 30s: {last_err}")


def _poll_status(mgmt_url: str, request_id: str, *,
                 timeout_s: int = 600, interval_s: float = 2.0) -> dict:
    """Block until the operator approves or rejects, or `timeout_s` elapses.
    Default 10 min — enough for an operator to glance at a popup and click.

    Transient connect / timeout errors are swallowed and retried. Only
    404 (request_id not yet replicated) and explicit reject get
    surfaced; everything else gets one more attempt in `interval_s`.
    Without this tolerance, a single slow round-trip during the
    master's bedrock-d startup kills the joiner with a traceback.
    """
    from urllib.parse import quote
    deadline = time.monotonic() + timeout_s
    last_state = ""
    while time.monotonic() < deadline:
        try:
            r = _http_json("GET",
                f"{mgmt_url}/api/join/status?id={quote(request_id)}",
                timeout=5)
            st = r.get("state", "pending")
            if st != last_state:
                print(f"  join state: {st}")
                last_state = st
            if st == "approved":
                return r
            if st == "rejected":
                raise RuntimeError(
                    f"operator rejected join: {r.get('reason') or 'no reason given'}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Request not yet replicated to this node's snapshot — wait.
                pass
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # mgmt API not reachable right this second (still warming
            # up after init, or transient mesh path flap). Try again.
            if last_state != "warming-up":
                print(f"  join state: waiting for mgmt API ({type(e).__name__})")
                last_state = "warming-up"
        time.sleep(interval_s)
    raise TimeoutError(f"no approval after {timeout_s}s")


def _install_peer_pubkeys(pubkeys: list):
    """Add each peer pubkey to /root/.ssh/authorized_keys (dedup)."""
    if not pubkeys:
        return
    authz = Path("/root/.ssh/authorized_keys")
    authz.parent.mkdir(mode=0o700, exist_ok=True)
    existing = authz.read_text() if authz.exists() else ""
    lines = [ln.strip() for ln in existing.splitlines() if ln.strip()]
    for pk in pubkeys:
        pk = pk.strip()
        if pk and pk not in lines:
            lines.append(pk)
    authz.write_text("\n".join(lines) + "\n")
    authz.chmod(0o600)


def install(witness: str, cluster_info: dict, repo: str):
    """Joiner-side install for ``bedrock join``: run the node_join saga
    (ordered idempotent steps, resumable from crash). The saga reuses the
    join-handshake helpers above (_request_join / _poll_status /
    _install_peer_pubkeys)."""
    import sys as _sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parents[2]
    if str(_root) not in _sys.path:
        _sys.path.insert(0, str(_root))
    for p in ("/usr/local/lib/bedrock",):
        if p not in _sys.path:
            _sys.path.insert(0, p)
    from bedrock_d.install.node_join import run_node_join
    return run_node_join(witness=witness, cluster_info=cluster_info, repo=repo)
