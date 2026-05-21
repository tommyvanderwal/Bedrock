"""NodeJoin saga — `bedrock join` flow as ordered idempotent steps.

Sibling of ``cluster_init.py``. Same model: each step's body opens
with its idempotency check, delegates to legacy helpers, and writes
no rqlite state before ``start_rqlited_joiner``.

# Entry point: ``run_node_join(...)``

The caller (``bedrock join``) supplies the master's mgmt URL and
the witness host. The saga:

1. Builds the saga ``ctx`` dict.
2. Opens a ``FileSagaBackend`` at ``/var/lib/bedrock/init-progress.json``
   (same path as cluster_init — different ``kind``, no collision).
3. Submits / resumes / retries the ``node_join`` op.
4. Raises on failure with the failing step name + error.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Path-shim for the still-legacy installer/lib helpers we delegate
# into. Stage 7 of the rewrite plan moves these under bedrock_d/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "installer"))

from bedrock_d.orchestrator.sagas import (  # noqa: E402
    FileSagaBackend, SagaExecutor, SagaState, saga, step,
)

log = logging.getLogger(__name__)

INIT_PROGRESS_PATH = Path("/var/lib/bedrock/init-progress.json")


# ───────────────────────────────────────────────────────────────────
# Saga
# ───────────────────────────────────────────────────────────────────


@saga("node_join")
class NodeJoin:
    """`bedrock join --witness <host>` — joiner-side flow.

    ctx inputs (set by the caller / cmd_join):
      - witness: str (master mgmt URL or hostname/IP)
      - cluster_info: dict (the master's /cluster-info response,
                            including cluster_uuid + cluster_name +
                            mgmt_url + existing nodes list)
      - repo: str (URL of the install repo for binary downloads)

    ctx outputs (set as steps run):
      - mgmt_ip: str (this joiner's br0/LAN IP)
      - node_name: str
      - bedrock_pubkey: str (hex)
      - request_id: str (after request_join_approval)
      - loopback_ip: str (allocated by master, returned in approval)
      - cluster_uuid: str
      - master_loopback: str
    """

    # ─── Local prep ──────────────────────────────────────────────────

    @step("prepare_dirs")
    def step_prepare_dirs(self, ctx):
        """Filesystem dirs joiner needs. Idempotent (mkdir -p)."""
        Path("/mnt/bedrock").mkdir(parents=True, exist_ok=True)
        Path("/opt/bedrock").mkdir(parents=True, exist_ok=True)
        Path("/root/.ssh").mkdir(mode=0o700, exist_ok=True)

    @step("detect_mgmt_ip")
    def step_detect_mgmt_ip(self, ctx):
        """Pick the joiner's LAN/bridge IP. Prefer br0; fall back to
        any non-link-local UP NIC."""
        from lib import state as _state
        s = _state.load()
        hw = s.get("hardware", {})
        ip = ""
        for n in hw.get("nics", []):
            if (n.get("state") == "UP" and n.get("name") == "br0"
                    and n.get("ip")):
                ip = n["ip"]
                break
        if not ip:
            for n in hw.get("nics", []):
                if (n.get("state") == "UP" and n.get("ip")
                        and not n["ip"].startswith("169.254.")):
                    ip = n["ip"]
                    break
        if not ip:
            raise RuntimeError("could not detect mgmt IP — no UP NIC "
                               "with a non-link-local IPv4")
        ctx["mgmt_ip"] = ip

    @step("derive_identity")
    def step_derive_identity(self, ctx):
        """Generate the joiner's Ed25519 inter-node identity +
        derive its node_name. Idempotent — peer_auth.pubkey_hex
        creates the key on first call, reads it on subsequent."""
        from lib import state as _state, peer_auth as _pa
        s = _state.load()
        hw = s.get("hardware", {})
        existing = ctx["cluster_info"].get("nodes", [])
        node_name = hw.get("hostname", f"node{len(existing) + 1}")
        ctx["node_name"] = node_name
        ctx["bedrock_pubkey"] = _pa.pubkey_hex()

    @step("install_exporters")
    def step_install_exporters(self, ctx):
        """node_exporter + vm_exporter so master's scrape config
        gets us. Idempotent — exporters.install checks-and-skips."""
        from lib import exporters as _e
        _e.install(ctx["repo"])

    # ─── Network handshake with the master ───────────────────────────

    @step("request_join_approval")
    def step_request_join_approval(self, ctx):
        """ECDH ephemeral keys + POST /api/join/request to the master,
        await operator approval, decrypt cluster.key from the sealed
        reply. The fingerprint shown here is what the operator clicks
        Approve on at the master's dashboard.

        Not idempotent in the strict sense (re-running re-asks for
        approval), but the master's join_handshake dedupes by
        node_name + request id; if the prior request was approved,
        we hit the same loopback_ip allocation."""
        from lib import join_handshake as _jh
        from pathlib import Path as _P
        import os as _os

        pub_path = _P("/root/.ssh/id_ed25519.pub")
        my_pubkey = pub_path.read_text().strip() if pub_path.exists() else ""

        mgmt_url = ctx["cluster_info"].get("mgmt_url") or \
            f"https://{ctx['witness']}:8443"
        ctx["mgmt_url"] = mgmt_url

        eph_priv, eph_pub_b64 = _jh.gen_ephemeral()
        fp = _jh.fingerprint(ctx["bedrock_pubkey"])
        log.info("node_join: requesting approval; fingerprint=%s", fp)
        # Re-use the legacy _request_join helper to talk to the master.
        from lib import agent_install as _ai
        req = _ai._request_join(
            mgmt_url, ctx["node_name"], ctx["mgmt_ip"],
            ctx["bedrock_pubkey"], eph_pub_b64, my_pubkey,
        )
        ctx["request_id"] = req["request_id"]
        log.info("node_join: request_id=%s; awaiting operator approval",
                 ctx["request_id"])
        approval = _ai._poll_status(mgmt_url, ctx["request_id"])
        cluster_key = _jh.open_seal(
            eph_priv, approval["master_eph_pubkey"],
            ctx["request_id"], approval["ciphertext"], approval["nonce"],
        )
        _P("/etc/bedrock/cluster.key").write_bytes(cluster_key)
        _os.chmod("/etc/bedrock/cluster.key", 0o600)
        ctx["approval"] = {
            "nodes":             approval.get("nodes", []),
            "node_map":          approval.get("node_map", {}),
            "peer_pubkeys":      approval.get("peer_pubkeys", []),
            "peer_ips":          approval.get("peer_ips", []),
            "master_loopback":   approval.get("master_loopback_ip", ""),
            "mgmt_master":       approval.get("mgmt_master", ""),
            "loopback_ip":       approval.get("loopback_ip", ""),
            "cluster_key_hex":   cluster_key.hex(),
        }
        ctx["loopback_ip"]      = ctx["approval"]["loopback_ip"]
        ctx["master_loopback"]  = ctx["approval"]["master_loopback"]
        ctx["cluster_uuid"]     = ctx["cluster_info"].get("cluster_uuid")

    @step("write_state_json")
    def step_write_state_json(self, ctx):
        """Commit cluster identity + role to state.json. Idempotent —
        state.save overwrites atomically."""
        from lib import state as _state
        s = _state.load()
        s.update({
            "cluster_name":  ctx["cluster_info"].get("cluster_name",
                                                     "bedrock"),
            "cluster_uuid":  ctx["cluster_uuid"],
            "role":          "compute",
            "node_id":       len(ctx["cluster_info"].get("nodes", [])),
            "node_name":     ctx["node_name"],
            "witness_host":  ctx["witness"],
            "mgmt_url":      ctx["mgmt_url"],
            "mgmt_ip":       ctx["mgmt_ip"],
            "loopback_ip":   ctx.get("loopback_ip", ""),
        })
        _state.save(s)

    @step("write_bootstrap_cluster_json")
    def step_write_bootstrap_cluster_json(self, ctx):
        """Local cluster.json so rqlite_setup can render its env
        file. The mgmt-side rqlite_subscriber overwrites this from
        canonical rqlite state once we're joined. Idempotent."""
        approval = ctx.get("approval") or {}
        node_map = dict(approval.get("node_map") or {})
        # Ensure SELF is present (master may not have folded our
        # node_register into cluster.json yet at status-poll time).
        pub_path = Path("/root/.ssh/id_ed25519.pub")
        my_pubkey = pub_path.read_text().strip() if pub_path.exists() else ""
        node_map[ctx["node_name"]] = {
            "host":           ctx["mgmt_ip"],
            "loopback_ip":    ctx.get("loopback_ip", ""),
            "role":           "compute",
            "pubkey":         my_pubkey,
            "bedrock_pubkey": ctx["bedrock_pubkey"],
        }
        path = Path("/etc/bedrock/cluster.json")
        existing: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text()) or {}
            except Exception:
                existing = {}
        existing.update({
            "cluster_uuid": ctx["cluster_uuid"],
            "cluster_name": ctx["cluster_info"].get("cluster_name",
                                                    "bedrock"),
            "mgmt_master":  approval.get("mgmt_master", ""),
            "nodes":        node_map,
        })
        for k in ("tiers", "witnesses", "params", "vms",
                  "backup_targets", "paths", "operators",
                  "join_requests"):
            existing.setdefault(k, {})
        existing.setdefault("obs_backends", {"metrics": [], "logs": []})
        existing.setdefault("log_index", 0)
        path.write_text(json.dumps(existing, indent=2))

    @step("install_peer_pubkeys")
    def step_install_peer_pubkeys(self, ctx):
        """Add every peer's SSH pubkey to /root/.ssh/authorized_keys.
        Idempotent — _install_peer_pubkeys de-duplicates."""
        from lib import agent_install as _ai
        approval = ctx.get("approval") or {}
        _ai._install_peer_pubkeys(approval.get("peer_pubkeys", []))

    @step("prescan_peer_hostkeys")
    def step_prescan_peer_hostkeys(self, ctx):
        """ssh-keyscan each peer so `virsh migrate` over qemu+ssh
        works first try. Idempotent (`sort -u`)."""
        approval = ctx.get("approval") or {}
        peer_ips = approval.get("peer_ips", []) or []
        if not peer_ips:
            return
        for ip in peer_ips:
            subprocess.run(
                f"ssh-keyscan -H -T 3 {ip} >> /root/.ssh/known_hosts 2>/dev/null",
                shell=True, check=False,
            )
        subprocess.run(
            "sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts",
            shell=True, check=False,
        )

    # ─── Storage + daemon ────────────────────────────────────────────

    @step("provision_storage_n1")
    def step_provision_storage_n1(self, ctx):
        """LVM thinpool + local tier LVs. Idempotent. As with init,
        write_rqlite=False — rqlite isn't up yet on the joiner."""
        from lib import tier_storage as _ts
        _ts.setup_n1(write_rqlite=False)

    @step("pre_extract_mgmt")
    def step_pre_extract_mgmt(self, ctx):
        """Pre-extract mgmt.tar.gz to /opt/bedrock so bedrock-d can
        import mgmt.app + mgmt.orchestrator at startup. Without
        this bedrock-d crash-loops while waiting for dashboard_install.
        Idempotent (tar overwrites existing files)."""
        if Path("/var/lib/bedrock-install/mgmt.tar.gz").exists():
            subprocess.run(
                "tar xzf /var/lib/bedrock-install/mgmt.tar.gz "
                "-C /opt/bedrock --strip-components=0",
                shell=True, check=False,
            )

    @step("start_bedrock_d")
    def step_start_bedrock_d(self, ctx):
        """Enable + start the unified daemon. The orchestrator will
        come up first, the mesh thread next; rqlited follows in the
        next step because it Requires=bedrock-d.service."""
        subprocess.run(["systemctl", "daemon-reload"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "reset-failed",
                        "bedrock-d.service"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "enable", "--now",
                        "bedrock-d.service"],
                       check=False, timeout=30)
        # rp_filter loose mode for the mesh's asymmetric paths.
        subprocess.run(
            "sysctl -wq net.ipv4.conf.all.rp_filter=2 "
            "net.ipv4.conf.default.rp_filter=2 "
            "net.ipv4.ip_forward=1",
            shell=True, check=False,
        )

    # ─── rqlite join ────────────────────────────────────────────────

    @step("wait_master_reachable")
    def step_wait_master_reachable(self, ctx):
        """Ping the master's loopback via the mesh. Bounded poll —
        if the mesh hasn't installed the /32 route in 15 s, fail
        loud rather than letting rqlited's -join silently hang."""
        master_lo = ctx.get("master_loopback") or ""
        if not master_lo:
            return  # No master loopback known (older master version)
        for _ in range(30):
            rc = subprocess.run(
                ["ping", "-c", "1", "-W", "1", master_lo],
                capture_output=True,
            )
            if rc.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"master loopback {master_lo} unreachable after 15s; "
            f"check `bedrock-d` mesh status (`journalctl -u bedrock-d`)"
        )

    @step("render_rqlited_env")
    def step_render_rqlited_env(self, ctx):
        """Write /etc/bedrock/rqlited.env from cluster.json + state.json.
        Idempotent."""
        from lib import rqlite_setup as _rqs
        _rqs.render_env_file()

    @step("start_rqlited_joiner")
    def step_start_rqlited_joiner(self, ctx):
        """Enable + start bedrock-rqlited with -join pointing at the
        master. Poll /status until raft is in (Leader, Follower,
        Voter) — we just need to be a voter, not the leader."""
        subprocess.run(["systemctl", "reset-failed",
                        "bedrock-rqlited.service"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "enable",
                        "bedrock-rqlited.service"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "restart",
                        "bedrock-rqlited.service"],
                       check=False, timeout=30)
        last_state = "?"
        for _ in range(60):  # 60 × 0.5 s = 30 s
            time.sleep(0.5)
            rc = subprocess.run(
                ["curl", "-fsSL", "--max-time", "1",
                 "http://127.0.0.1:4001/status"],
                capture_output=True,
            )
            if rc.returncode != 0:
                continue
            try:
                last_state = json.loads(
                    rc.stdout.decode())["store"]["raft"]["state"]
            except Exception:
                continue
            # Joiner becomes Follower (or Leader if elected); either
            # means "voter in the Raft group".
            if last_state in ("Leader", "Follower"):
                return
        raise RuntimeError(
            f"rqlited didn't join the cluster within 30s "
            f"(last raft state: {last_state}); "
            f"check `journalctl -u bedrock-rqlited`"
        )

    # ─── SeaweedFS local services ───────────────────────────────────

    @step("install_dashboard")
    def step_install_dashboard(self, ctx):
        """Joiners also serve the dashboard (operator can reach it
        on any node). Idempotent — dashboard_install handles re-runs."""
        from lib import dashboard_install as _di
        _di.install_dashboard(ctx["repo"], with_metrics=False)

    @step("seaweedfs_install")
    def step_seaweedfs_install(self, ctx):
        """Confirm /usr/local/bin/weed is present. Idempotent."""
        from lib import seaweedfs as _sw
        _sw.ensure_install()

    @step("seaweedfs_configs")
    def step_seaweedfs_configs(self, ctx):
        """Render the seaweed configs + env. Idempotent."""
        from lib import seaweedfs as _sw
        _sw.write_env_file()
        _sw.write_master_config()
        _sw.write_filer_config()
        _sw.write_s3_config()

    @step("seaweedfs_start_local")
    def step_seaweedfs_start_local(self, ctx):
        """Enable + start weed-master (if in Raft-3 set), weed-volume
        + weed-s3 on this node. Filer stays on the master only —
        cluster_arbiter on the master owns its lifecycle.
        Idempotent."""
        from lib import seaweedfs as _sw
        _sw.promote_to_master_volume_host()

    @step("fuse_mount")
    def step_fuse_mount(self, ctx):
        """FUSE-mount the filer at /mnt/bedrock pointing at the
        cluster VIP (.254:8888). Idempotent."""
        from lib import seaweedfs as _sw
        try:
            _sw.ensure_iso_library_mount()
        except Exception as e:
            # Mount failure is non-fatal at join time — the filer
            # may not be ready yet; auto-mount will keep retrying.
            log.warning("fuse_mount: %s (will retry async)", e)

    @step("cluster_tier_join_peer")
    def step_cluster_tier_join_peer(self, ctx):
        """Wait for the master's cluster_tier_promote_master saga to
        flip ``tiers.critical.mode`` to ``drbd`` in rqlite (projected
        to cluster.json by the local subscriber), then join the DRBD
        secondary so the initial sync carries the master's filer
        leveldb3 + arbiter rqlite data over.

        At N=1 this step is a no-op (no peer; tier stays local until
        a 2nd node joins). At N>=2 it polls cluster.json then runs
        ``tier_storage.transition_to_n2_peer``.

        Idempotent: ``transition_to_n2_peer`` checks for existing
        LVs/config before creating, and DRBD ``up`` on an already-up
        resource is a noop. A resumed saga that hit a transient
        timeout earlier just polls again."""
        import time as _t
        from pathlib import Path as _Path
        import json as _json
        cluster_path = _Path("/etc/bedrock/cluster.json")

        def _state():
            try:
                c = _json.loads(cluster_path.read_text())
                return c, ((c.get("tiers") or {})
                           .get("critical") or {}).get("mode", "local")
            except Exception:
                return {}, "?"

        cluster, mode = _state()
        if len(cluster.get("nodes") or {}) < 2:
            log.info("cluster_tier_join_peer: cluster is N=1; nothing "
                     "to do (this step fires on N>=2)")
            return
        timeout_s = 120
        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            cluster, mode = _state()
            if mode == "drbd":
                break
            _t.sleep(2)
        if mode != "drbd":
            raise RuntimeError(
                f"cluster_tier_join_peer: master never promoted "
                f"critical tier to DRBD after {timeout_s}s "
                f"(last mode={mode!r}). Check the "
                f"cluster_tier_promote_master saga on the master.")

        from lib import tier_storage as _ts
        from pathlib import Path as _P
        # Build peer list (master + every recorded peer + self).
        nodes = cluster.get("nodes") or {}
        tier = (cluster.get("tiers") or {}).get("critical") or {}
        peer_names = tier.get("peers") or []
        master_name = cluster.get("mgmt_master") or ""
        master_lo = (nodes.get(master_name) or {}).get("loopback_ip", "")
        peers = []
        for nm in peer_names:
            info = nodes.get(nm) or {}
            peers.append({"name": nm,
                          "loopback_ip": info.get("loopback_ip", "")})
        my_name = ctx.get("node_name") or ""
        my_lo = ctx.get("loopback_ip") or ""
        if my_name and not any(p["name"] == my_name for p in peers):
            peers.append({"name": my_name, "loopback_ip": my_lo})
        _ts.transition_to_n2_peer(
            self_loopback_ip=my_lo,
            master={"name": master_name, "loopback_ip": master_lo},
            peers=peers,
        )


# ───────────────────────────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────────────────────────


def run_node_join(*, witness: str, cluster_info: dict,
                  repo: str) -> None:
    """Entry point for `bedrock join` via the saga path.

    On crash mid-step, the FileSagaBackend at INIT_PROGRESS_PATH
    records progress; re-running `bedrock join` resumes from the
    first not-done step.
    """
    INIT_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    backend = FileSagaBackend(path=INIT_PROGRESS_PATH)
    requested_by = (os.environ.get("SUDO_USER")
                    or os.environ.get("USER") or "operator")

    # Find an existing in-flight or failed node_join for this node
    # (cluster_init done; now resuming a previously-failed join).
    raw = (json.loads(backend.path.read_text())
           if backend.path.exists() else {})
    existing_id = None
    existing_state = None
    node_name = _local_node_name()
    for op in (raw.get("ops") or {}).values():
        if (op.get("kind") == "node_join"
                and op.get("target_node") == node_name
                and op.get("state") != "completed"):
            existing_id = op["id"]
            existing_state = op["state"]
            break

    backend_params = {
        "witness":      witness,
        "cluster_info": cluster_info,
        "repo":         repo,
    }
    _enrich_params_from_state(backend_params)

    executor = SagaExecutor(backend=backend, this_node=node_name)

    if existing_id is not None:
        log.info("node_join: picking up existing op id=%d (state=%s)",
                 existing_id, existing_state)
        _update_op_params(backend, existing_id, backend_params)
        result = (executor.retry(existing_id)
                  if existing_state == "failed"
                  else executor.execute_one(existing_id))
    else:
        op_id = executor.submit(
            kind="node_join", target_node=node_name,
            params=backend_params, requested_by=requested_by,
        )
        log.info("node_join: submitted new op id=%d", op_id)
        result = executor.execute_one(op_id)

    if result.state != SagaState.COMPLETED:
        raise RuntimeError(
            f"node_join failed at step {result.last_step!r}: "
            f"{result.error}"
        )


def _local_node_name() -> str:
    import socket as _sock
    try:
        return _sock.gethostname()
    except OSError:
        return "joiner"


def _enrich_params_from_state(params: dict) -> None:
    """Mutate ``params`` in-place, adding durable identity from
    state.json + the on-disk Ed25519 key so resumed steps see what
    earlier steps wrote.

    On a fresh run, ``derive_identity`` writes ``bedrock_pubkey`` into
    ctx. On a resumed run, the step is skipped (already ``done``)
    and a later step like ``request_join_approval`` would KeyError
    without this enrichment. ``peer_auth.pubkey_hex`` is idempotent
    — it reads the existing key or creates one — so we can call it
    unconditionally here."""
    try:
        from lib import state as _state
        s = _state.load()
    except Exception:
        s = {}
    for k in ("cluster_uuid", "node_name", "loopback_ip", "mgmt_ip"):
        if s.get(k):
            params.setdefault(k, s[k])
    # node_name has two possible sources before the saga has finished:
    # the projected state.json (handled above) OR the hardware sub-dict
    # that derive_identity itself consults. Mirror derive_identity's
    # logic here so a resumed saga gets the same answer.
    if "node_name" not in params:
        hw = (s.get("hardware") or {}) if isinstance(s, dict) else {}
        host = hw.get("hostname") or _local_node_name()
        if host:
            params["node_name"] = host
    try:
        from lib import peer_auth as _pa
        params.setdefault("bedrock_pubkey", _pa.pubkey_hex())
    except Exception:
        # peer_auth is only available post-install; on the first
        # call this is fine — derive_identity will populate it.
        pass


def _update_op_params(backend, op_id: int, new_params: dict) -> None:
    raw = json.loads(backend.path.read_text())
    op = raw["ops"].get(str(int(op_id)))
    if op is None:
        return
    op["params"] = dict(new_params)
    op["updated_at"] = int(time.time())
    backend._write(raw)
