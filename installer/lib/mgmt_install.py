"""Install full management stack on the first node (`bedrock init`).

Downloads and starts:
  - VictoriaMetrics (port 8428)
  - VictoriaLogs (port 9428, syslog :5140)
  - FastAPI + Svelte dashboard (port 8080)
  - SQLite inventory DB
  - bedrock-witness (podman container, port 9443) — if no external witness
"""

import os
import subprocess
import uuid
from pathlib import Path
from typing import Optional
from . import state, exporters, tier_storage, daemon_setup


BEDROCK_BASE = Path("/opt/bedrock")
BINARIES = BEDROCK_BASE / "bin"
DATA = BEDROCK_BASE / "data"
MGMT = BEDROCK_BASE / "mgmt"


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    return r.stdout.strip()


def _pick_mgmt_ip(hw: dict) -> str:
    """Pick the mgmt NIC IP — prefer br0, else any 192.168.x.x (LAN)."""
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["name"] == "br0" and n["ip"]:
            return n["ip"]
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["ip"] and not n["ip"].startswith("10."):
            return n["ip"]
    for n in hw.get("nics", []):
        if n["state"] == "UP" and n["ip"]:
            return n["ip"]
    return ""


def _download(url: str, dest: Path):
    print(f"  Fetching {url.split('/')[-1]}...")
    run(f"curl -fsSL -o {dest} '{url}'")


def _write_systemd(name: str, content: str):
    path = Path(f"/etc/systemd/system/{name}.service")
    path.write_text(content)
    run("systemctl daemon-reload")


def install_full(cluster_name: str, repo: str):
    """Install FastAPI + VM + VL + SQLite + witness.

    Execution: by default, the saga path
    (``bedrock_d.install.cluster_init.run_cluster_init``) — 21
    ordered idempotent steps, progress persisted to
    ``/var/lib/bedrock/init-progress.json``, resumable from crash.

    Legacy procedural body below is preserved as the
    ``BEDROCK_INIT_SAGA=0`` opt-out for one release while the saga
    path bakes. Operators reach for the legacy path only if a saga
    step has a bug we haven't caught yet. It will be deleted once
    the saga path passes a clean testbed e2e + 0.8-beta tag.
    """
    import os as _os
    if _os.environ.get("BEDROCK_INIT_SAGA", "1") != "0":
        # Default: saga path.
        import sys as _sys
        from pathlib import Path as _Path
        _root = _Path(__file__).resolve().parents[2]
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        # Also add the installed location so on-node imports work.
        for p in ("/usr/local/lib/bedrock",):
            if p not in _sys.path:
                _sys.path.insert(0, p)
        from bedrock_d.install.cluster_init import run_cluster_init
        return run_cluster_init(
            cluster_name=cluster_name,
            repo=repo,
        )
    print("[bedrock init] legacy procedural path (BEDROCK_INIT_SAGA=0)")

    s = state.load()
    hw = s.get("hardware", {})

    # Directories
    for d in (BINARIES, DATA / "vm", DATA / "vl", MGMT):
        d.mkdir(parents=True, exist_ok=True)

    # 1. VictoriaMetrics
    if not (BINARIES / "victoria-metrics").exists():
        _download(f"{repo}/binaries/victoria-metrics", BINARIES / "victoria-metrics")
        os.chmod(BINARIES / "victoria-metrics", 0o755)

    # 2. VictoriaLogs
    if not (BINARIES / "victoria-logs").exists():
        _download(f"{repo}/binaries/victoria-logs", BINARIES / "victoria-logs")
        os.chmod(BINARIES / "victoria-logs", 0o755)

    # 3. ISO library: uploads write directly to /mnt/bedrock/iso/ (the
    #    SeaweedFS FUSE mount on every node), so the filer is the
    #    canonical store. /opt/bedrock/iso is kept as a staging area
    #    for first-boot fixtures (e.g. virtio-win.iso fetched at
    #    `bedrock init` time, then seed-copied into the filer by
    #    seaweedfs.seed_iso_library — a one-time migration helper).
    iso_dir = BEDROCK_BASE / "iso"
    iso_dir.mkdir(parents=True, exist_ok=True)
    (iso_dir / "README.md").write_text(
        "# Bedrock ISO library\n\n"
        "Upload install ISOs via the dashboard (/isos) or scp here directly.\n"
        "Files appear in the 'Create VM' dropdown.\n"
    )
    # Pre-fetch the virtio-win driver ISO. Attached as a 2nd CDROM on every
    # VM install so Windows Setup can load viostor + NetKVM without manual
    # download. Harmless for Linux installs — ignored by the installer.
    virtio_win = iso_dir / "virtio-win.iso"
    if not virtio_win.exists():
        # Prefer the LAN-cached copy (dev box repo); fall back to upstream on
        # first-ever install where the dev box hasn't cached it yet.
        print("  Fetching virtio-win.iso (~750 MB, one-time)...")
        sources = [
            f"{repo}/virtio-win.iso",
            "https://fedorapeople.org/groups/virt/virtio-win/"
            "direct-downloads/stable-virtio/virtio-win.iso",
        ]
        ok = False
        for url in sources:
            r = subprocess.run(
                f"curl -fsSL --connect-timeout 5 -o {virtio_win}.tmp '{url}'",
                shell=True)
            if r.returncode == 0:
                (iso_dir / "virtio-win.iso.tmp").rename(virtio_win)
                ok = True
                break
        if not ok:
            print("  WARN: virtio-win.iso download failed; Windows installs "
                  "will need the driver ISO attached manually.")
    # ISO library lives in the SeaweedFS filer namespace; see
    # seaweedfs.ensure_iso_library_mount() — every node FUSE-mounts
    # the filer root at /mnt/bedrock, so libvirt's --cdrom path
    # /mnt/bedrock/iso/<name>.iso works unchanged everywhere. Seed
    # any virtio-win.iso staged here into the filer namespace once
    # master+volume+filer+s3 are up (handled by promote_to_filer_host's
    # first-time setup).

    # 4. FastAPI + Svelte dashboard files. Same helper runs on
    # followers too — the dashboard is reachable from ANY node.
    # NOTE: Python deps (fastapi, uvicorn, paramiko, websockets, pydantic,
    # python-multipart) installed by packages.install_base() on every
    # node, not here. (Lessons-log L17 — every node may become master.)
    print("  Installing dashboard application...")
    from . import dashboard_install as _di

    # 5. Prometheus scrape config — mgmt app will rewrite this whenever
    #    nodes register/unregister, so we just seed with this node.
    mgmt_ip = _pick_mgmt_ip(hw)
    scrape_conf = f"""scrape_configs:
  - job_name: node
    scrape_interval: 10s
    static_configs:
      - targets: ['{mgmt_ip}:9100']
        labels:
          cluster: {cluster_name}
  - job_name: libvirt
    scrape_interval: 10s
    static_configs:
      - targets: ['{mgmt_ip}:9177']
        labels:
          cluster: {cluster_name}
"""
    (BEDROCK_BASE / "scrape.yml").write_text(scrape_conf)

    # 6. Install node_exporter + vm_exporter (this node is mgmt+compute)
    exporters.install(repo)

    # 7. Systemd units — bedrock-vm + bedrock-vl run on the master only
    # (single VictoriaMetrics + VictoriaLogs instance per cluster).
    # bedrock-mgmt (FastAPI + Svelte UI) is installed by dashboard_install,
    # which also runs on followers so the dashboard is reachable on every
    # node.
    _write_systemd("bedrock-vm", f"""[Unit]
Description=Bedrock VictoriaMetrics
After=network.target

[Service]
ExecStart={BINARIES}/victoria-metrics -storageDataPath={DATA}/vm -promscrape.config={BEDROCK_BASE}/scrape.yml -retentionPeriod=90d -httpListenAddr=:8428
Restart=always

[Install]
WantedBy=multi-user.target
""")
    _write_systemd("bedrock-vl", f"""[Unit]
Description=Bedrock VictoriaLogs
After=network.target

[Service]
ExecStart={BINARIES}/victoria-logs -storageDataPath={DATA}/vl -httpListenAddr=:9428 -syslog.listenAddr.tcp=:5140
Restart=always

[Install]
WantedBy=multi-user.target
""")

    print("  Starting metrics + logs services...")
    run("systemctl enable --now bedrock-vm bedrock-vl", check=False)
    print("  Installing + starting dashboard service (with metrics)...")
    _di.install_dashboard(repo, with_metrics=True)

    # Save state. No witness configured at init — see the saga's
    # docstring + docs/sagas/cluster_init.md for why.
    s["cluster_name"] = cluster_name
    s["cluster_uuid"] = s.get("cluster_uuid") or str(uuid.uuid4())
    s["role"] = "mgmt+compute"
    s["node_id"] = 0
    s["node_name"] = hw.get("hostname", "node1")
    s["mgmt_ip"] = _pick_mgmt_ip(hw)
    # Port 8080 is loopback-only (intra-node); HTTPS on 8443 is the
    # LAN-reachable endpoint and what joiners need to dial.
    s["mgmt_url"] = f"https://{s['mgmt_ip']}:8443"
    # Mgmt master gets the lowest cluster identity. Joiners get the
    # next free index allocated by the register endpoint. The /24
    # comes from cluster_addr.node_loopback_ip(uuid, N) — derived
    # from cluster_uuid, lives in RFC 6598 (100.64.0.0/10), can't
    # collide with operator LANs.
    from . import cluster_addr as _ca
    s["loopback_ip"] = _ca.node_loopback_ip(s["cluster_uuid"], 1)
    state.save(s)

    # Initialise /etc/bedrock/cluster.json with this node registered.
    # No drbd_ip / tb_ip / eno_ip keys: every intra-cluster bind/target
    # is loopback_ip (the /32 on `lo`) and the mesh layer routes it.
    import json as _json
    cluster = {
        "cluster_name": cluster_name,
        "cluster_uuid": s["cluster_uuid"],
        "nodes": {
            s["node_name"]: {
                "host": s["mgmt_ip"],
                "role": "mgmt+compute",
                "loopback_ip": s["loopback_ip"],
                "cockpit": f"https://{s['mgmt_ip']}:9090",
            }
        },
    }
    from pathlib import Path as _Path
    _Path("/etc/bedrock/cluster.json").write_text(_json.dumps(cluster, indent=2))

    print(f"  Cluster UUID: {s['cluster_uuid']}")
    print(f"  Dashboard:    https://{s['mgmt_ip']}:8443")

    # Storage tiers — N=1 single-node setup. Idempotent; safe on re-run.
    # NB: rqlite is not yet up at this point — setup_n1 only writes
    # local tier state (cluster.json). The cluster_init flow mirrors
    # tier rows into rqlite later, after rqlited has reached Leader.
    print()
    print("Setting up storage tiers (N=1: local LV thin)...")
    try:
        tier_storage.setup_n1(write_rqlite=False)
    except Exception as e:
        print(f"  WARN: tier setup failed: {e}")
        print(f"  You can re-run with: bedrock storage init")

    # Cluster HMAC key — shared with every joiner via the register
    # response so witness heartbeats from every node verify against
    # the same secret.
    try:
        daemon_setup.write_cluster_key()
    except Exception as e:
        print(f"  WARN: cluster_key write failed: {e}")

    # Bootstrap the rqlite cluster-state store: apply schema,
    # then seed cluster_info, master node_register, initial
    # operator, mgmt_master, master loopback, and the master as
    # the sole obs_backend. Per D-19, every subsequent mutation
    # rides this same rqlite store + bumps revision.
    try:
        import time as _t
        import json as _json
        import subprocess as _sp
        from pathlib import Path as _Path
        from . import rqlite_setup as _rqs
        from . import rqlite_client as _rc, bedrock_state as _bs
        from . import peer_auth as _pa, operator_auth as _oa

        # Bootstrap chicken-egg fix: rqlite_setup.render_env_file()
        # reads cluster.json to derive node_id (sorted-name index).
        # cluster.json is normally regenerated by view_builder from
        # rqlite — but rqlite isn't up yet at init time. So write a
        # minimal cluster.json with just the master entry, render
        # the env, start rqlited, seed the schema + initial rows,
        # then let the orchestrator's rqlite_subscriber rewrite
        # cluster.json from the canonical store going forward.
        _cluster_json = _Path("/etc/bedrock/cluster.json")
        _cluster_json.parent.mkdir(parents=True, exist_ok=True)
        _bootstrap_cluster_json = {
            "cluster_uuid": s["cluster_uuid"],
            "cluster_name": cluster_name,
            "mgmt_master":  s["node_name"],
            "nodes": {
                s["node_name"]: {
                    "host":          s["mgmt_ip"],
                    "loopback_ip":   s["loopback_ip"],
                    "role":          "mgmt+compute",
                    "pubkey":        "",
                    "bedrock_pubkey": "",
                },
            },
            "tiers": {}, "witnesses": {}, "params": {},
            "vms": {}, "backup_targets": {}, "paths": {},
            "operators": {}, "join_requests": {},
            "obs_backends": {"metrics": [], "logs": []},
            "log_index": 0,
        }
        _cluster_json.write_text(_json.dumps(
            _bootstrap_cluster_json, indent=2))

        try:
            _rqs.render_env_file()
        except Exception as e:
            print(f"  WARN: rqlite_setup render_env failed: {e}")
            raise
        # Start (not enable + start). install.sh writes the unit file
        # but doesn't enable it (would crash-loop without env file).
        # We don't enable here either — bedrock-rqlited.service has
        # `WantedBy=` empty by design (see configs/bedrock-rqlited.service);
        # the saga executor in bedrock-d is what controls its lifecycle.
        # `systemctl enable` on an empty-WantedBy unit just emits a
        # "no installation config" warning and otherwise does nothing.
        #
        # reset-failed clears any stale rate-limit counter. stderr is
        # suppressed because the unit may not yet be in systemd's
        # in-memory state if no daemon-reload ran since install.sh
        # wrote the unit file — the failure mode there is harmless.
        _sp.run(["systemctl", "reset-failed",
                 "bedrock-rqlited.service", "bedrock-d.service"],
                check=False, timeout=10,
                stderr=_sp.DEVNULL)
        _sp.run(["systemctl", "restart", "bedrock-rqlited.service"],
                check=False, timeout=30)
        # Wait for rqlited to be Leader, not just HTTP-up. The
        # /status endpoint serves 200 the moment HTTP binds, but
        # /db/execute returns 503 'leader not found' until Raft
        # elects. Seeding before Leader-up = silent schema-empty
        # cluster.
        leader_reached = False
        last_raft_state = "?"
        for _attempt in range(60):
            _t.sleep(0.5)
            rc = _sp.run(
                ["curl", "-fsSL", "--max-time", "1",
                 "--cert", "/etc/bedrock/node.crt",
                 "--key",  "/etc/bedrock/node.key.pem",
                 "--cacert", "/etc/bedrock/ca.crt",
                 "https://127.0.0.1:4001/status"],
                capture_output=True
            )
            if rc.returncode != 0:
                continue
            try:
                last_raft_state = _json.loads(
                    rc.stdout.decode())["store"]["raft"]["state"]
            except Exception:
                continue
            if last_raft_state == "Leader":
                leader_reached = True
                break
        if not leader_reached:
            # Hard fail — there's nothing useful to do next without
            # a writable rqlite. Surface the actual Raft state so
            # the operator can diagnose (e.g. "Candidate" = quorum
            # failure, "Follower" = elected someone else, "?" = HTTP
            # never came up).
            raise RuntimeError(
                f"rqlited didn't reach Leader within 30s "
                f"(last raft state: {last_raft_state}); "
                f"check `journalctl -u bedrock-rqlited`")
        # Master's Bedrock identity Ed25519 (idempotent across runs).
        master_bedrock_pub = _pa.pubkey_hex()
        # Seed the initial operator account so the dashboard is
        # immediately usable. Test-setup default `root` / `admin`.
        _salt, _phash = _oa.hash_password("admin")

        with _rc.RqliteClient() as rqlite:
            # Schema lives next to this file in the deployed lib dir.
            _schema = _Path(__file__).parent / "bedrock_schema.sql"
            _rc.apply_schema(rqlite, str(_schema))
            _bs.cluster_init(
                cluster_uuid=s["cluster_uuid"],
                cluster_name=cluster_name,
                client=rqlite,
            )
            # Master's own SSH pubkey — required so any future
            # joiner can SSH back here (master → peer SSH is used
            # by storage promote and node leave). Without this,
            # the joiner's _install_peer_pubkeys gets an empty
            # entry for the master.
            try:
                _master_pubkey = _Path("/root/.ssh/id_ed25519.pub") \
                    .read_text().strip()
            except Exception:
                _master_pubkey = ""
            _bs.node_register(
                node_name=s["node_name"],
                host=s["mgmt_ip"],
                role="mgmt+compute",
                pubkey=_master_pubkey,
                bedrock_pubkey=master_bedrock_pub,
                client=rqlite,
            )
            _bs.node_loopback(
                node_name=s["node_name"],
                loopback_ip=s["loopback_ip"],
                client=rqlite,
            )
            _bs.operator_set(
                username="root", salt=_salt, password_hash=_phash,
                client=rqlite,
            )
            _bs.obs_backends_set(
                metrics=[s["node_name"]], logs=[s["node_name"]],
                client=rqlite,
            )
            _bs.set_mgmt_master(s["node_name"], client=rqlite)

        # Now that rqlite is up + schema applied + this node
        # registered, push the local tier_state into rqlite. Done
        # OUTSIDE the `with RqliteClient()` block since
        # mirror_tier_state_to_rqlite opens its own short-lived
        # client.
        try:
            tier_storage.mirror_tier_state_to_rqlite()
        except Exception as e:
            # If THIS fails after rqlite is up + we already registered
            # this node, it's a real bug. Raise so init aborts.
            raise RuntimeError(
                f"tier_state mirror to rqlite failed: {e}") from e
    except Exception as e:
        # The seed phase is load-bearing — if it fails, the cluster
        # is half-initialised and quietly broken. Fail loud so the
        # operator (or test harness) knows to re-run / clean up
        # rather than continuing with a zombie state.
        print(f"  ERROR: rqlite seed failed: {e}", flush=True)
        print(f"         cluster is half-initialised; remediate before "
              f"re-running.", flush=True)
        raise SystemExit(1)

    # bedrock-d unified daemon — mesh discovery, election, witness IO,
    # rqlite_subscriber, fence_responder, boot_orchestrator, the
    # dashboard, and cert refresh all live in this one process. systemd
    # unit was placed by install.sh; we just enable + start.
    print()
    print("Starting bedrock-d unified daemon...")
    try:
        import subprocess as _sp
        _sp.run("systemctl daemon-reload", shell=True, check=False)
        _sp.run("systemctl reset-failed bedrock-d.service 2>/dev/null",
                shell=True, check=False)
        _sp.run("systemctl enable --now bedrock-d.service",
                shell=True, check=False, capture_output=True)
        _sp.run(
            "sysctl -wq net.ipv4.conf.all.rp_filter=2 "
            "net.ipv4.conf.default.rp_filter=2 "
            "net.ipv4.ip_forward=1",
            shell=True, check=False,
        )
    except Exception as e:
        print(f"  WARN: bedrock-net start failed: {e}")

    # SeaweedFS setup. Master + volume run on every node (peer-of-
    # everyone HA pattern); filer + s3 follow the mgmt-master via
    # cluster_arbiter.converge() on the next revision tick.
    print()
    print("Setting up SeaweedFS (master + volume on every node)...")
    try:
        from . import seaweedfs as _sw
        _sw.ensure_install()
        _sw.write_env_file()
        _sw.write_master_config()
        _sw.write_filer_config()
        _sw.write_s3_config()
        _sw.promote_to_master_volume_host()
        # Filer + S3 start on N=1 immediately (no DRBD, master IS filer
        # host). cluster_arbiter.converge() does the same thing but
        # racing with the orchestrator subscriber's first tick — do
        # it inline so `bedrock init` returns with everything up.
        _sw.promote_to_filer_host()
        # Wait for the S3 gateway to actually bind 0.0.0.0:8333 before
        # returning. Without this, `bedrock init` returns while weed-s3
        # is still spinning up and any test or operator script that
        # immediately PUTs to the S3 endpoint gets ECONNREFUSED.
        import socket as _sock, time as _t
        for _attempt in range(30):
            try:
                _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
                _s.settimeout(0.5)
                _s.connect(("127.0.0.1", 8333))
                _s.close()
                break
            except Exception:
                _t.sleep(0.5)
        # ISO library FUSE mount + seed any pre-staged ISOs (e.g.
        # virtio-win.iso shipped in the install ISO).
        _sw.ensure_iso_library_mount()
        _sw.seed_iso_library(Path("/opt/bedrock/iso"))
    except Exception as e:
        print(f"  WARN: SeaweedFS setup failed: {e}")
