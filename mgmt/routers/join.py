"""Cluster join handshake — UNAUTH /api/join/request (joiner has no creds yet); approval is
operator-gated. X25519 ECDH so cluster.key never crosses the wire in plaintext."""
from __future__ import annotations
from typing import Optional
from pathlib import Path
import sys as _sys_peerauth

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from dependencies import require_operator
from common import load_cluster, push_log, ssh_cmd, ssh_cmd_rc, get_nodes

import sys as _sys
_sys.path.insert(0, "/usr/local/lib/bedrock")
from lib import join_handshake as _join_hs       # noqa: E402
from lib import bedrock_state as _bs             # noqa: E402
from lib import peer_auth as _peer_auth          # noqa: E402
from lib import operator_auth as _op_auth        # noqa: E402
from lib import rqlite_client as _rqlite        # noqa: E402
router = APIRouter(tags=["join"])




# ── Join handshake ──────────────────────────────────────────────────
# A joiner doesn't yet have an operator token or a recognised peer
# identity, so /api/join/request is UNAUTH. The privacy of the
# handshake comes from:
#   - operator visually verifying the Ed25519 fingerprint on approval,
#   - X25519 ECDH so cluster.key never traverses the wire in plaintext.
# The request id alone doesn't authorise anything: it's a handle the
# joiner polls; the master only acts when an operator approves.

class JoinRequest(BaseModel):
    node_name: str
    host: str
    bedrock_pubkey: str           # joiner's Ed25519 identity (hex)
    x25519_eph_pubkey: str        # joiner's X25519 ephemeral (base64)
    ssh_pubkey: str = ""          # joiner's OpenSSH ed25519 line (`ssh-ed25519 …`)




@router.post("/api/join/request")
def join_request(req: JoinRequest):
    """Joiner asks to join. We log the request (replicates everywhere
    so any node's dashboard shows the popup); operator decides.

    `ssh_pubkey` is the joiner's OpenSSH `ssh-ed25519 …` line — kept on
    the in-process pending map (NOT in the log; we don't want to leak
    half-baked SSH identities into the replicated state) and installed
    on every node's authorized_keys when the operator approves.
    """
    rid = _join_hs.new_request_id()
    fp = _join_hs.fingerprint(req.bedrock_pubkey)
    try:
        _bs.join_request(
            request_id=rid,
            node_name=req.node_name,
            host=req.host,
            bedrock_pubkey=req.bedrock_pubkey,
            x25519_eph_pubkey=req.x25519_eph_pubkey,
            fingerprint=fp,
        )
    except Exception as e:
        raise HTTPException(503, f"could not record join request: {e}")
    # Cache the SSH pubkey + host so the approve handler can install it
    # without needing the joiner to re-send.
    _PENDING_SSH_PUBKEYS[rid] = {"ssh_pubkey": req.ssh_pubkey, "host": req.host}
    push_log(f"join request: {req.node_name} ({req.host}) fp={fp}",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"request_id": rid, "fingerprint": fp}




@router.get("/api/join/status")
def join_status(id: str):
    """Joiner polls this to learn whether an operator approved or
    rejected. No auth — the request_id is the handle (unguessable
    192-bit secret)."""
    cluster = load_cluster()
    req = (cluster.get("join_requests") or {}).get(id)
    if not req:
        raise HTTPException(404, "unknown request_id")
    out = {"state": req.get("state", "pending")}
    if out["state"] == "approved":
        # ECDH bundle so the joiner can decrypt cluster.key.
        out["master_eph_pubkey"] = req.get("master_eph_pubkey", "")
        out["ciphertext"] = req.get("ciphertext", "")
        out["nonce"] = req.get("nonce", "")
        # Cluster membership the joiner needs to finish install. All
        # this lives in the replicated snapshot anyway, but inlining it
        # here saves the joiner a second authenticated round-trip.
        node_name = req.get("node_name", "")
        node_info = (cluster.get("nodes") or {}).get(node_name) or {}
        peer_pubkeys = []
        peer_ips = []
        for n_name, n in (cluster.get("nodes") or {}).items():
            if n_name == node_name:
                continue
            if n.get("pubkey"):
                peer_pubkeys.append(n["pubkey"])
            if n.get("host"):
                peer_ips.append(n["host"])
        # mgmt-master's loopback /32 — that's where the joiner's
        # bedrock-rust dials. Falls back to first node with "mgmt"
        # in role if mgmt_master isn't set yet.
        master_name = None
        for n_name, n in (cluster.get("nodes") or {}).items():
            if "mgmt" in (n.get("role", "") or ""):
                master_name = n_name; break
        master_addr = ((cluster.get("nodes") or {}).get(master_name, {})
                       .get("loopback_ip")
                       or (cluster.get("nodes") or {}).get(master_name, {})
                       .get("host", "")) if master_name else ""
        # Full per-node map so the joiner can write a bootstrap
        # cluster.json — required for rqlite_setup.render_env_file()
        # to compute peer loopbacks and the sorted-name node-id.
        node_map = {}
        for n_name, n in (cluster.get("nodes") or {}).items():
            node_map[n_name] = {
                "host":          n.get("host", ""),
                "loopback_ip":   n.get("loopback_ip", ""),
                "role":          n.get("role", "compute"),
                "pubkey":        n.get("pubkey", ""),
                "bedrock_pubkey": n.get("bedrock_pubkey", ""),
            }
        out.update({
            "cluster_name": cluster.get("cluster_name", "bedrock"),
            "cluster_uuid": cluster.get("cluster_uuid", ""),
            "loopback_ip":  node_info.get("loopback_ip", ""),
            "peer_pubkeys": peer_pubkeys,
            "peer_ips":     sorted(set(peer_ips)),
            "master_loopback_ip": master_addr,
            "mgmt_master":  master_name or "",
            "nodes":        list((cluster.get("nodes") or {}).keys()),
            "node_map":     node_map,
            # Cluster CA + the joiner's CA-signed TLS cert. PEM-encoded.
            # The joiner uses these to configure rqlited mTLS as part of
            # its install. Filled by /api/join/approve via
            # cluster_ca.sign_node_cert; default '' if approval came
            # from a pre-TLS master that hasn't been re-installed yet.
            "node_cert_pem": req.get("node_cert_pem", ""),
            "ca_cert_pem":   req.get("ca_cert_pem", ""),
        })
    elif out["state"] == "rejected":
        out["reason"] = req.get("reason", "")
    return out




@router.get("/api/join/pending")
def join_pending(user: str = Depends(require_operator)):
    """Dashboard polls this to drive the approval popup."""
    cluster = load_cluster()
    items = []
    for rid, r in (cluster.get("join_requests") or {}).items():
        if r.get("state") == "pending":
            items.append({"request_id": rid, **r})
    return {"pending": items}




class JoinApprove(BaseModel):
    request_id: str




@router.post("/api/join/approve")
def join_approve(req: JoinApprove, user: str = Depends(require_operator)):
    cluster = load_cluster()
    pending = (cluster.get("join_requests") or {}).get(req.request_id) or {}
    if pending.get("state") != "pending":
        raise HTTPException(400, f"request not pending (state={pending.get('state')!r})")

    # Generate master's ephemeral X25519 + seal cluster.key under the ECDH
    # session key (HKDF salted with request_id).
    master_priv, master_pub_b64 = _join_hs.gen_ephemeral()
    cluster_key = Path("/etc/bedrock/cluster.key").read_bytes()
    ciphertext_b64, nonce_b64 = _join_hs.seal(
        master_priv, pending["x25519_eph_pubkey"],
        req.request_id, cluster_key)

    # Allocate the joiner's loopback /32 by scanning rqlite.nodes for
    # taken indices (the authoritative source; the local replica read
    # below may briefly lag, hence the level='strong' query).
    used_loopbacks: set[str] = set()
    try:
        with _rqlite.RqliteClient() as _rc:
            for row in _rc.query(
                "SELECT loopback_ip FROM nodes WHERE loopback_ip <> ''",
                level="strong",
            ):
                used_loopbacks.add(row["loopback_ip"])
    except Exception:
        used_loopbacks = {n.get("loopback_ip")
                          for n in (cluster.get("nodes") or {}).values()
                          if n.get("loopback_ip")}
    _sys_peerauth.path.insert(0, "/usr/local/lib/bedrock")
    from lib import cluster_addr as _ca
    next_loopback = ""
    for i in range(1, 250):
        cand = _ca.node_loopback_ip(cluster.get("cluster_uuid", ""), i)
        if cand not in used_loopbacks:
            next_loopback = cand; break

    # Pull joiner's SSH pubkey from the in-memory side-channel (was
    # cached at /api/join/request time — see _PENDING_SSH_PUBKEYS).
    ssh_info = _PENDING_SSH_PUBKEYS.pop(req.request_id, {}) or {}
    joiner_ssh_pubkey = (ssh_info.get("ssh_pubkey") or "").strip()

    # Install the joiner's SSH pubkey locally (mgmt → joiner SSH works)
    # AND fan it out to every existing peer (peer → joiner SSH works).
    # Without this, the moment any node tries paramiko-probe the
    # joiner, sshd auth-fails accumulate per-source-IP penalties until
    # OpenSSH PerSourcePenalties stops accepting from that source for
    # up to 10 minutes — manifesting as the joiner flapping Offline on
    # every dashboard.
    if joiner_ssh_pubkey:
        _append_authorized_key(joiner_ssh_pubkey)
        for n_name, n in (cluster.get("nodes") or {}).items():
            host = n.get("host", "")
            if host and host != pending["host"]:
                try:
                    _append_authorized_key(joiner_ssh_pubkey, host)
                except Exception as _e:
                    push_log(f"fan-out pubkey to {host} failed: {_e}",
                             node="mgmt", app="bedrock-mgmt", level="warn")

    # Auto-promote on the 1→2 transition: if the cluster currently has
    # only 1 metrics/logs backend, appoint the joiner as the 2nd one.
    # N≥3 joins do NOT change the backend list — they stay agent-only
    # nodes (decommission/promote-spare is a separate operator action,
    # not implemented yet).
    obs_now = (cluster.get("obs_backends") or {})
    metrics_bk = list(obs_now.get("metrics") or [])
    logs_bk    = list(obs_now.get("logs") or [])
    promote_metrics = len(metrics_bk) < 2 and pending["node_name"] not in metrics_bk
    promote_logs    = len(logs_bk)    < 2 and pending["node_name"] not in logs_bk
    if promote_metrics:
        metrics_bk.append(pending["node_name"])
    if promote_logs:
        logs_bk.append(pending["node_name"])

    # If we're promoting this joiner to a backend slot AND there's an
    # existing backend with data, seed the joiner's data dir from the
    # existing backend BEFORE the snapshot says "joiner is a backend".
    # That way the reactor doesn't start an empty backend that agents
    # then dual-write into — we'd accumulate a gap until 90d
    # Stage the auto-promote: same agents-first → seed → start ordering
    # as `observability_promote`. See that handler for the rationale —
    # this block keeps the same shape so behaviour stays consistent.

    # Phase 1: log node_register + node_loopback + (optionally) the
    # OBS_BACKENDS_SET that adds the joiner. Agents on every node then
    # reconfigure to dual-write toward the joiner (queuing because the
    # joiner's bedrock-vm isn't up yet). The joiner's reactor writes
    # the unit file but `_can_start_vm_backend` keeps bedrock-vm
    # stopped until the seed populates the data dir.
    # Sign the joiner's TLS cert with the cluster CA so the joiner
    # can configure rqlited mTLS as part of its install. The joiner's
    # raw Ed25519 pubkey came in pending["bedrock_pubkey"] (hex). CA
    # key+cert live on the DRBD `cluster` singleton mount (master only) per
    # cluster_ca.py — failure here means we lost the master role
    # mid-handshake and should surface to operator.
    try:
        from lib import cluster_ca as _ca
        joiner_pub_raw = bytes.fromhex(pending["bedrock_pubkey"])
        joiner_node_cert_pem = _ca.sign_node_cert(
            joiner_pub_raw, pending["node_name"], next_loopback
        ).decode("ascii")
        ca_cert_pem = _ca.CA_CERT_DRBD.read_bytes().decode("ascii")
    except Exception as e:
        raise HTTPException(503, f"could not sign joiner cert: {e}")

    try:
        with _rqlite.RqliteClient() as _rc:
            _bs.node_register(
                node_name=pending["node_name"],
                host=pending["host"],
                role="compute",
                pubkey=joiner_ssh_pubkey,
                bedrock_pubkey=pending["bedrock_pubkey"],
                # 'joining' until the joiner's saga self-activates at the
                # end of its join (node_set_active). Keeps the joiner out
                # of the election denominator so the master can't be
                # tipped into NoQuorum mid-join (C1).
                state="joining",
                client=_rc,
            )
            if next_loopback:
                _bs.node_loopback(
                    node_name=pending["node_name"],
                    loopback_ip=next_loopback,
                    client=_rc,
                )
            if promote_metrics or promote_logs:
                _bs.obs_backends_set(
                    metrics=metrics_bk, logs=logs_bk, client=_rc)
            _bs.join_resolved(
                request_id=req.request_id,
                decision="approved",
                master_eph_pubkey=master_pub_b64,
                ciphertext=ciphertext_b64,
                nonce=nonce_b64,
                node_cert_pem=joiner_node_cert_pem,
                ca_cert_pem=ca_cert_pem,
                client=_rc,
            )
    except Exception as e:
        raise HTTPException(503, f"could not record approval: {e}")

    # Phase 2: brief wait for agents to fold the new entry + start
    # queueing writes for the joiner.
    if promote_metrics or promote_logs:
        import time as _t
        _t.sleep(2)

    # Phase 3: seed the joiner's data dir from the existing backend.
    # Writes that arrived between the snapshot point and the joiner's
    # backend-start are safe in the agents' disk queues; they drain
    # in phase 4.
    if (promote_metrics and obs_now.get("metrics")) or \
       (promote_logs and obs_now.get("logs")):
        try:
            from lib import observability as _obs
            existing_metrics_bk = (obs_now.get("metrics") or [])
            source_metrics_host = ((cluster.get("nodes") or {})
                                   .get(existing_metrics_bk[0], {}).get("host", "")) \
                                  if existing_metrics_bk else ""
            target_host = pending["host"]

            def _runner(host: str, cmd: str, timeout: int = 60):
                return ssh_cmd_rc(host, cmd, timeout=timeout)

            if promote_metrics and source_metrics_host:
                push_log(f"seeding metrics backend on {pending['node_name']} from {source_metrics_host}",
                         node="mgmt", app="bedrock-mgmt", level="info")
                rep = _obs.seed_backend(source_metrics_host, target_host,
                                        _runner, None)
                push_log(f"metrics seed: {rep.get('metrics','?')}",
                         node="mgmt", app="bedrock-mgmt", level="info")
        except Exception as e:
            push_log(f"seed_backend warning: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")

    # Phase 4: start bedrock-vm on the joiner (the seed gate kept it
    # stopped; explicit kick gets it running with the seeded data).
    # bedrock-vl was already started by the reactor (no seed for VL).
    if promote_metrics and pending["host"]:
        try:
            ssh_cmd(pending["host"], "systemctl start bedrock-vm.service", timeout=20)
        except Exception as e:
            push_log(f"could not start bedrock-vm on {pending['node_name']}: {e}",
                     node="mgmt", app="bedrock-mgmt", level="warn")
    push_log(f"operator {user!r} approved join {pending['node_name']} ({pending['host']})",
             node="mgmt", app="bedrock-mgmt", level="info")
    return {"state": "approved", "loopback_ip": next_loopback}




class JoinReject(BaseModel):
    request_id: str
    reason: str = ""




@router.post("/api/join/reject")
def join_reject(req: JoinReject, user: str = Depends(require_operator)):
    cluster = load_cluster()
    pending = (cluster.get("join_requests") or {}).get(req.request_id) or {}
    if pending.get("state") != "pending":
        raise HTTPException(400, f"request not pending (state={pending.get('state')!r})")
    try:
        _bs.join_resolved(
            request_id=req.request_id,
            decision="rejected",
            reason=req.reason or "denied by operator",
        )
    except Exception as e:
        raise HTTPException(503, f"could not record rejection: {e}")
    push_log(f"operator {user!r} rejected join {pending.get('node_name','?')}",
             node="mgmt", app="bedrock-mgmt", level="warn")
    return {"state": "rejected"}




def _append_authorized_key(pubkey: str, target_host: Optional[str] = None):
    """Append pubkey to /root/.ssh/authorized_keys on target_host (or local)."""
    line = pubkey.strip()
    if not line:
        return
    if target_host is None:
        authz = Path("/root/.ssh/authorized_keys")
        authz.parent.mkdir(mode=0o700, exist_ok=True)
        existing = authz.read_text() if authz.exists() else ""
        if line not in existing:
            authz.write_text(existing.rstrip() + "\n" + line + "\n")
            authz.chmod(0o600)
        return
    # On a peer over SSH — mgmt already has SSH trust there (peer joined earlier).
    import shlex as _shlex
    quoted = _shlex.quote(line)
    try:
        ssh_cmd(target_host,
            f"mkdir -p -m 700 /root/.ssh && "
            f"grep -qxF {quoted} /root/.ssh/authorized_keys 2>/dev/null || "
            f"echo {quoted} >> /root/.ssh/authorized_keys && "
            f"chmod 600 /root/.ssh/authorized_keys",
            timeout=10)
    except Exception as e:
        push_log(f"Could not push pubkey to {target_host}: {e}",
                 node="mgmt", app="bedrock-mgmt", level="warn")




def _read_local_pubkey() -> str:
    p = Path("/root/.ssh/id_ed25519.pub")
    return p.read_text().strip() if p.exists() else ""
# ── pending joiner SSH pubkeys ──




# Side-channel for the joiner's SSH pubkey + host, by request_id.
# Lives in memory only; lost on restart. If the operator approves after
# a mgmt restart, the approve handler still works for the crypto path
# but skips the SSH pubkey installation — peer-SSH from this node to
# the joiner won't work until `bedrock node refresh-keys` (TODO).
_PENDING_SSH_PUBKEYS: dict[str, dict] = {}
