"""Install agent stack on secondary nodes (`bedrock join`).

Registers with the cluster's mgmt API, deploys exporters.
"""

import json
import os
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path
from . import (state, exporters, tier_storage, daemon_setup,
               dashboard_install, peer_auth, join_handshake)


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
    Without this tolerance, a single slow round-trip during master's
    bedrock-mgmt startup kills the joiner with a traceback.
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
    """Joiner-side install. By default delegates to the node_join
    saga (bedrock_d.install.node_join.run_node_join). The legacy
    procedural body below is the ``BEDROCK_INIT_SAGA=0`` opt-out
    for one release while the saga path bakes."""
    import os as _os
    if _os.environ.get("BEDROCK_INIT_SAGA", "1") != "0":
        import sys as _sys
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parents[2]
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        for p in ("/usr/local/lib/bedrock",):
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from bedrock_d.install.node_join import run_node_join
        return run_node_join(
            witness=witness, cluster_info=cluster_info, repo=repo,
        )
    print("[bedrock join] legacy procedural path (BEDROCK_INIT_SAGA=0)")

    s = state.load()
    hw = s.get("hardware", {})

    # mgmt_ip = the LAN/bridge address joiners reach the master on.
    # Per cluster_addr.py every intra-cluster bind/target uses the
    # node's loopback /32 (set on `lo` by bedrock-net once the cluster
    # identity is allocated by the master at join approval).
    mgmt_ip = ""
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["name"] == "br0" and n["ip"]:
            mgmt_ip = n["ip"]; break
    if not mgmt_ip:
        for n in hw.get("nics", []):
            if n["state"] == "UP" and n["ip"] and not n["ip"].startswith("169.254."):
                mgmt_ip = n["ip"]; break

    existing = cluster_info.get("nodes", [])
    node_name = hw.get("hostname", f"node{len(existing)+1}")
    # Prefer the mgmt_url from cluster_info (returned by discovery.
    # query_cluster) — that reflects what the master actually serves
    # (HTTPS once cert-refresh has fetched a cert, HTTP fallback
    # before). Fall back to the historical HTTPS-on-8443 hardcoding
    # only if cluster_info didn't provide one. Cert verification
    # stays off; peer trust comes from the operator-approved
    # Ed25519 fingerprint check at the popup, not from TLS PKI.
    mgmt_url = cluster_info.get("mgmt_url") or f"https://{witness}:8443"

    # Deploy exporters first — register makes mgmt rewrite scrape.yml to include us
    print("  Installing exporters...")
    exporters.install(repo)

    # Read our own pubkey to send with register so mgmt (and peers) can SSH in.
    pub_path = Path("/root/.ssh/id_ed25519.pub")
    my_pubkey = pub_path.read_text().strip() if pub_path.exists() else ""

    # Generate this node's Ed25519 Bedrock identity (idempotent — file
    # check first). The pubkey gets registered with the cluster so any
    # peer can verify HTTP requests we sign with `peer_auth.sign(...)`.
    bedrock_pub_hex = peer_auth.pubkey_hex()

    # Approval-based join (piece #3): we ECDH a session key with the master,
    # show the operator our Ed25519 fingerprint, and the master ships
    # cluster.key sealed under the session key. No fallback to plain
    # /api/nodes/register here — the old path stays for backward-compat
    # with installers from before this code lived in the lib tree.
    print(f"  Mgmt:           {mgmt_url}")
    print(f"  Bedrock pubkey: {bedrock_pub_hex}")
    eph_priv, eph_pub_b64 = join_handshake.gen_ephemeral()
    fp = join_handshake.fingerprint(bedrock_pub_hex)
    print(f"  Fingerprint:    {fp}")
    print(f"  → asking master for join approval...")
    req = _request_join(mgmt_url, node_name, mgmt_ip,
                        bedrock_pub_hex, eph_pub_b64, my_pubkey)
    request_id = req["request_id"]
    print(f"  request_id: {request_id}")
    print(f"  Compare the fingerprint above with the popup on the cluster")
    print(f"  dashboard; click Approve to continue.")
    approval = _poll_status(mgmt_url, request_id)

    # ECDH-derived session key opens the AEAD-sealed cluster.key.
    cluster_key = join_handshake.open_seal(
        eph_priv, approval["master_eph_pubkey"],
        request_id, approval["ciphertext"], approval["nonce"])
    Path("/etc/bedrock/cluster.key").write_bytes(cluster_key)
    os.chmod("/etc/bedrock/cluster.key", 0o600)
    print(f"  cluster.key received ({len(cluster_key)} bytes) and stored")

    # Reshape approval response to look like the old register response
    # so the rest of install() can run unchanged.
    result = {
        "nodes": approval.get("nodes", []),
        "node_map": approval.get("node_map", {}),
        "peer_pubkeys": approval.get("peer_pubkeys", []),
        "peer_ips": approval.get("peer_ips", []),
        "master_loopback_ip": approval.get("master_loopback_ip", ""),
        "mgmt_master": approval.get("mgmt_master", ""),
        "loopback_ip": approval.get("loopback_ip", ""),
        "cluster_key_hex": cluster_key.hex(),
    }
    print(f"  Joined. Cluster has {len(result['nodes'])} nodes.")

    # Now safe to commit state — registration was accepted.
    s.update({
        "cluster_name": cluster_info.get("cluster_name", "bedrock"),
        "cluster_uuid": cluster_info.get("cluster_uuid", "unknown"),
        "role": "compute",
        "node_id": len(existing),
        "node_name": node_name,
        "witness_host": witness,
        "mgmt_url": mgmt_url,
        "mgmt_ip": mgmt_ip,
        # Cluster identity for the mesh layer. mgmt allocated this in
        # the register response; bedrock-net.service reads it from
        # state.json on next start to pin the /32 on `lo`. Empty if
        # mgmt is on an older version that didn't allocate one.
        "loopback_ip": result.get("loopback_ip", ""),
    })
    state.save(s)

    # Bootstrap cluster.json on the joiner — without it, rqlite_setup
    # can't render the env-file (it needs the nodes dict for peer
    # loopbacks + sorted-name node-id). bedrock-mgmt's snapshot watcher
    # will overwrite this with the canonical fold output once rqlited
    # is joined and the SQLite DB has replicated.
    try:
        import json as _json
        from pathlib import Path as _Path
        node_map = dict(result.get("node_map") or {})
        # Ensure self entry is present (master may not have folded our
        # node_register into cluster.json yet at /api/join/status time).
        node_map[node_name] = {
            "host":          mgmt_ip,
            "loopback_ip":   result.get("loopback_ip", ""),
            "role":          "compute",
            "pubkey":        my_pubkey,
            "bedrock_pubkey": bedrock_pub_hex,
        }
        _cluster_json = _Path("/etc/bedrock/cluster.json")
        existing_cj = {}
        if _cluster_json.exists():
            try:
                existing_cj = _json.loads(_cluster_json.read_text()) or {}
            except Exception:
                existing_cj = {}
        existing_cj.update({
            "cluster_uuid": s["cluster_uuid"],
            "cluster_name": s["cluster_name"],
            "mgmt_master":  result.get("mgmt_master", ""),
            "nodes":        node_map,
        })
        existing_cj.setdefault("tiers", {})
        existing_cj.setdefault("witnesses", {})
        existing_cj.setdefault("params", {})
        existing_cj.setdefault("vms", {})
        existing_cj.setdefault("backup_targets", {})
        existing_cj.setdefault("paths", {})
        existing_cj.setdefault("operators", {})
        existing_cj.setdefault("join_requests", {})
        existing_cj.setdefault("obs_backends", {"metrics": [], "logs": []})
        existing_cj.setdefault("log_index", 0)
        _cluster_json.write_text(_json.dumps(existing_cj, indent=2))
        print(f"  cluster.json bootstrapped with {len(node_map)} nodes")
    except Exception as e:
        print(f"  WARN: bootstrap cluster.json failed: {e}")

    # Install every peer's pubkey locally so mgmt + peers can SSH to this node.
    _install_peer_pubkeys(result.get("peer_pubkeys", []))

    # Pre-scan peer host keys so `virsh migrate` via qemu+ssh works on first try.
    peer_ips = result.get("peer_ips", [])
    if peer_ips:
        Path("/root/.ssh").mkdir(mode=0o700, exist_ok=True)
        for ip in peer_ips:
            subprocess.run(
                f"ssh-keyscan -H -T 3 {ip} >> /root/.ssh/known_hosts 2>/dev/null",
                shell=True, check=False)
        subprocess.run(
            "sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts",
            shell=True, check=False)
        print(f"  Pre-scanned {len(peer_ips)} peer host keys.")

    # Shared namespace: SeaweedFS FUSE mount at /mnt/bedrock. Set up
    # later (after bedrock-weed-filer is reachable on the cluster) by
    # seaweedfs.ensure_iso_library_mount(). At install time we just
    # ensure the mountpoint exists. ISOs land at /mnt/bedrock/iso/.
    Path("/mnt/bedrock").mkdir(exist_ok=True)

    # Storage tiers — N=1 setup on this node first (creates local LVs
    # and /bedrock/<tier> symlinks). Cluster-wide transition to N>=2
    # (DRBD on the critical tier) is triggered separately via
    # `bedrock storage promote`.
    print("  Setting up storage tiers (local LVs)...")
    try:
        tier_storage.setup_n1()
    except Exception as e:
        print(f"  WARN: tier setup failed: {e}")

    # Cluster HMAC key (used by lib/witness.py to sign Echo heartbeats).
    # The master's key comes down in the register response so every
    # node shares the same secret for witness auth.
    try:
        master_key_hex = result.get("cluster_key_hex")
        if master_key_hex:
            daemon_setup.write_cluster_key(bytes.fromhex(master_key_hex))
        else:
            print("  WARN: master did not send cluster_key_hex; "
                  "witness heartbeats won't match")
            daemon_setup.write_cluster_key()
    except Exception as e:
        print(f"  WARN: cluster_key setup failed: {e}")

    # bedrock-d needs /opt/bedrock/mgmt to import `mgmt.app` +
    # `mgmt.orchestrator` at startup. The full dashboard_install runs
    # later in this flow (line ~428), but if we don't pre-extract the
    # mgmt tarball here bedrock-d crash-loops with ModuleNotFoundError
    # for ~7 restarts (observed in v23 sim-2 join: 22:59:37..22:59:44
    # six crashes before dashboard_install finally extracted /opt/
    # bedrock/mgmt). Pre-extract from the offline payload — cheap +
    # idempotent.
    try:
        os.makedirs("/opt/bedrock", exist_ok=True)
        if os.path.exists("/var/lib/bedrock-install/mgmt.tar.gz"):
            subprocess.run(
                "tar xzf /var/lib/bedrock-install/mgmt.tar.gz "
                "-C /opt/bedrock --strip-components=0",
                shell=True, check=False,
            )
    except Exception as e:
        print(f"  WARN: pre-extract mgmt.tar.gz failed: {e} (dashboard_install will retry)")

    # bedrock-d unified daemon: mesh + mgmt + orchestrator + dashboard.
    # bedrock-d.service is enabled by dashboard_install / mgmt_install.
    # We start it here so the mesh thread can install /32 routes for
    # rqlited (which starts via Requires=bedrock-d.service).
    print("  Starting bedrock-d unified daemon (mesh + mgmt + orchestrator)...")
    try:
        subprocess.run("systemctl daemon-reload", shell=True, check=False)
        subprocess.run(
            "systemctl reset-failed bedrock-d.service 2>/dev/null",
            shell=True, check=False,
        )
        subprocess.run(
            "systemctl enable --now bedrock-d.service",
            shell=True, check=False, capture_output=True,
        )
        # Allow rp_filter loose mode so async return paths through
        # different NICs survive Linux's strict reverse-path check.
        # The mesh layer relies on this — without it, any path that
        # isn't symmetric drops on the receiver.
        subprocess.run(
            "sysctl -wq net.ipv4.conf.all.rp_filter=2 "
            "net.ipv4.conf.default.rp_filter=2 "
            "net.ipv4.ip_forward=1",
            shell=True, check=False,
        )
    except Exception as e:
        print(f"  WARN: bedrock-net start failed: {e}")

    # Start the joiner's own rqlited and -join the leader's Raft so
    # this node becomes a full rqlite voter. Required for bedrock-mgmt
    # on this node to read/write the cluster store locally — without
    # this, the only rqlite voter in the cluster is the master and
    # the joiner's local mgmt API can't query state.
    print("  Starting rqlited (joining leader's Raft)...")
    try:
        from . import rqlite_setup as _rqs
        import time as _t
        # Wait for the master's loopback to be reachable via the mesh
        # before rendering env — rqlited will -join 100.X.Y.1:4002.
        master_loopback = ""
        for n_name, n in (result.get("node_map") or {}).items():
            if n_name == result.get("mgmt_master"):
                master_loopback = n.get("loopback_ip", "")
                break
        if master_loopback:
            for _attempt in range(30):
                rc = subprocess.run(
                    ["ping", "-c", "1", "-W", "1", master_loopback],
                    capture_output=True,
                )
                if rc.returncode == 0:
                    break
                _t.sleep(0.5)
        _rqs.render_env_file()
        # install.sh writes the unit file at firstboot but doesn't
        # enable it (would crash-loop without env file). Now that the
        # env file exists, just `restart` — we skip `enable` because
        # bedrock-rqlited.service has `WantedBy=` empty by design
        # (see configs/bedrock-rqlited.service); the saga executor in
        # bedrock-d controls its lifecycle. `systemctl enable` on an
        # empty-WantedBy unit emits "no installation config" noise
        # and otherwise does nothing.
        #
        # reset-failed clears any stale rate-limit counter; stderr is
        # silenced because the unit may not have been daemon-reloaded
        # yet — the warning is benign.
        subprocess.run(
            ["systemctl", "reset-failed",
             "bedrock-rqlited.service", "bedrock-d.service"],
            check=False, timeout=10,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["systemctl", "restart", "bedrock-rqlited.service"],
            check=False, timeout=30,
        )
        for _attempt in range(30):
            _t.sleep(0.5)
            rc = subprocess.run(
                ["curl", "-fsSL", "--max-time", "1",
                 "http://127.0.0.1:4001/status"],
                capture_output=True,
            )
            if rc.returncode == 0:
                print(f"  rqlited up and joined")
                break
        else:
            print(f"  WARN: rqlited didn't respond within 15s on joiner")
    except Exception as e:
        print(f"  WARN: rqlited start failed: {e}")

    # SeaweedFS master + volume — every node hosts both (peer-of-
    # everyone HA pattern). Filer + s3 stay stopped on followers;
    # cluster_arbiter.converge() promotes them on this node only
    # if/when this node becomes mgmt master.
    print("  Starting SeaweedFS master + volume...")
    try:
        from . import seaweedfs as _sw
        _sw.ensure_install()
        _sw.write_env_file()
        _sw.write_master_config()
        _sw.write_filer_config()
        _sw.write_s3_config()
        _sw.promote_to_master_volume_host()
        # Shared namespace: FUSE-mount the filer root at /mnt/bedrock
        # so libvirt's --cdrom /mnt/bedrock/iso/<name>.iso just works
        # on this node like on every other.
        _sw.ensure_iso_library_mount()
    except Exception as e:
        print(f"  WARN: SeaweedFS setup failed: {e}")

    # Install + start the dashboard (FastAPI + Svelte UI). Reachable
    # at http://<this-node>:8080. The follower's mgmt API serves the
    # same cluster-wide picture from /etc/bedrock/cluster.json (kept
    # in sync by the replicated log + view_builder); writes go through
    # the same code paths and rely on cluster-wide SSH access.
    print("  Installing dashboard application...")
    try:
        dashboard_install.install_dashboard(repo, with_metrics=False)
    except Exception as e:
        print(f"  WARN: dashboard install failed: {e}")

    print()
    print(f"  Joined cluster {s['cluster_name']} as node {s['node_id']}.")
    print(f"  Dashboard: https://{mgmt_ip}:8443  or  {s['mgmt_url']}")
    print(f"  Storage:   /bedrock/{{scratch,bulk,critical}} (local LVs)")
    print(f"  Promote to N>=2 from any node:  bedrock storage promote")
