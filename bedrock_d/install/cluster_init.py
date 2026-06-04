"""ClusterInit saga — `bedrock init` flow as ordered idempotent steps.

# Entry point: ``run_cluster_init(...)``

Callers (``bedrock init`` via ``mgmt_install.install_full``) invoke
``run_cluster_init(cluster_name, repo)`` which:

1. Constructs the saga ``ctx`` dict.
2. Builds a ``FileSagaBackend`` at
   ``/var/lib/bedrock/init-progress.json`` (rqlite isn't up yet).
3. Submits a new ``cluster_init`` operation, OR resumes an existing
   in-flight one (idempotent re-run is safe; previously-done steps
   are skipped).
4. Runs steps in order via ``SagaExecutor.execute_one``.
5. Raises on failure (caller surfaces to operator).


This file IS the flow chart for what `bedrock init` does. Reading
the @step list top-to-bottom tells you exactly what runs in what
order. Each step body has its idempotency-check pattern as the
first executable lines.

# Why this lives in bedrock_d/install/ but not yet replaces install_full

The migration is a two-stage move:

1. **Now (Stage 2.a)** — this file defines the saga structure and
   delegates step bodies to the existing helpers in
   ``installer/lib/*``. The legacy ``mgmt_install.install_full``
   keeps running for end-to-end traffic. The saga is exercised by
   unit tests that verify step ordering + idempotency contract.

2. **Next (Stage 2.b)** — ``mgmt_install.install_full`` becomes a
   thin shim that builds a ``ctx`` dict and runs the saga via
   ``SagaExecutor`` + a file-backed ``SagaBackend``
   (``/var/lib/bedrock/init-progress.json``). At that point the
   procedural body of ``install_full`` is deleted; the step
   methods here are the only init code.

# Why a FILE backend, not rqlite

``cluster_init`` is what BRINGS UP rqlite. Steps 1-9 run before
``start_rqlited``; they can't persist anything to rqlite because
rqlite isn't up. A small JSON file (one operations row + per-step
state) lives at ``/var/lib/bedrock/init-progress.json`` and is
read on init re-run so a half-completed init resumes cleanly.
Stage 3+ sagas (node_join, vm_create, etc.) run against rqlite
since rqlite is up by then.

# Contract for step bodies

- Idempotent: re-running the step on already-done state is a
  no-op. Most steps' first 3 lines say "is this work already
  done?  return."
- No rqlite reads/writes before step ``start_rqlited``.
- No long-running blocking calls (>~30 s). If a step needs to
  wait (e.g. for a service to be Leader), it has a bounded poll
  loop with a clear timeout + diagnostic error message.
- Step methods take a ``ctx`` dict and return None. Mutations
  to ``ctx`` flow to subsequent steps in the same run.
- Failures raise. The executor records the failure; the operator
  decides whether to ``retry``.
"""
from __future__ import annotations

import json
import logging
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


# Path the FileSagaBackend persists init progress to. Survives
# `bedrock init` crash; resumable on re-run.
INIT_PROGRESS_PATH = Path("/var/lib/bedrock/init-progress.json")


def run_cluster_init(*, cluster_name: Optional[str] = None,
                     repo: str) -> None:
    """Entry point for `bedrock init` via the saga path.

    Builds the saga ``ctx``, opens the FileSagaBackend at
    INIT_PROGRESS_PATH, submits (or resumes) the ``cluster_init``
    operation, and runs it. On crash + re-run, the executor picks
    up at the first not-``done`` step.

    ``cluster_name`` is a display tag (defaults to
    ``bedrock-<hostname>`` when omitted); the cluster's real
    identity is the ``cluster_uuid`` allocated in
    ``step_allocate_identity``. Renamable later via
    ``bedrock cluster rename``.

    ``repo`` is the install repo URL used to fetch binaries during
    the install_obs_binaries / install_exporters steps.

    Raises ``RuntimeError`` on saga failure with the failed
    step name + the underlying error.
    """
    if not cluster_name:
        import socket as _socket
        cluster_name = f"bedrock-{_socket.gethostname()}"
    INIT_PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    backend = FileSagaBackend(path=INIT_PROGRESS_PATH)
    # Best-effort: identify the operator who triggered this for
    # the audit trail.
    import os as _os
    requested_by = _os.environ.get("SUDO_USER") or _os.environ.get(
        "USER") or "operator"

    # Find an existing cluster_init for this node — could be
    # in-flight (crash mid-step) OR failed (last attempt errored).
    # Both cases get the same "pick up where we left off" treatment:
    # in-flight → execute_one resumes from first not-done step;
    # failed → retry() resets to in_progress then executes_one
    # (same skip-already-done semantics).
    raw = json.loads(backend.path.read_text()) if backend.path.exists() else {}
    existing_id = None
    existing_state = None
    for op in (raw.get("ops") or {}).values():
        if (op.get("kind") == "cluster_init"
                and op.get("target_node") == _local_node_name()
                and op.get("state") != "completed"):
            existing_id = op["id"]
            existing_state = op["state"]
            break

    executor = SagaExecutor(backend=backend,
                            this_node=_local_node_name())

    # Seed durable identity into params so steps run on a resume
    # see what earlier steps (allocate_identity) already wrote to
    # state.json. Steps MUST NOT rely on ctx-only state across runs;
    # rebuild from durable sources (state.json / cluster.json /
    # rqlite). This is the "every step idempotent" contract — on
    # resume the ctx is reconstructed from params; we extend params
    # with the durable bits before submitting.
    _enrich_params_from_state(backend_params := {
        "cluster_name": cluster_name,
        "repo": repo,
    })

    if existing_id is not None:
        log.info("cluster_init: picking up existing op id=%d "
                 "(state=%s)", existing_id, existing_state)
        # Update params so resumed steps see current durable state.
        _update_op_params(backend, existing_id, backend_params)
        if existing_state == "failed":
            result = executor.retry(existing_id)
        else:
            result = executor.execute_one(existing_id)
    else:
        op_id = executor.submit(
            kind="cluster_init",
            target_node=_local_node_name(),
            params=backend_params,
            requested_by=requested_by,
        )
        log.info("cluster_init: submitted new op id=%d", op_id)
        result = executor.execute_one(op_id)
    if result.state != SagaState.COMPLETED:
        raise RuntimeError(
            f"cluster_init failed at step {result.last_step!r}: "
            f"{result.error}"
        )


def _local_node_name() -> str:
    """Best-effort: derive the local node name. Used as
    ``target_node`` so resume_in_flight finds OUR ops, not
    someone else's."""
    import socket as _sock
    try:
        # Same logic as legacy: prefer hardware-detected hostname
        # over a system one — but at install time they're the same.
        return _sock.gethostname()
    except OSError:
        return "node1"


def _enrich_params_from_state(params: dict) -> None:
    """Mutate ``params`` in-place, adding identity fields from
    state.json if present. Lets resumed steps see values that
    earlier steps (allocate_identity) wrote, since ctx itself is
    not persisted between runs."""
    try:
        from lib import state as _state
        s = _state.load()
    except Exception:
        return
    for k in ("cluster_uuid", "node_name", "loopback_ip", "mgmt_ip"):
        if s.get(k):
            params.setdefault(k, s[k])


def _update_op_params(backend, op_id: int, new_params: dict) -> None:
    """Overwrite an operation row's params on disk. Used on resume
    to merge in current durable state. FileSagaBackend-only path —
    rqlite backend won't need this since other sagas run after
    rqlite is up and read from rqlite directly."""
    raw = json.loads(backend.path.read_text())
    op = raw["ops"].get(str(int(op_id)))
    if op is None:
        return
    op["params"] = dict(new_params)
    op["updated_at"] = int(time.time())
    backend._write(raw)


@saga("cluster_init")
class ClusterInit:
    """`bedrock init <name>` — first-node bootstrap.

    ctx inputs (set by the caller / cmd_init):
      - cluster_name: str  (display tag; cluster identity is cluster_uuid)
      - repo: str          (URL of the install repo for binary downloads)

    ctx outputs (set as steps run):
      - cluster_uuid: str
      - node_name: str
      - loopback_ip: str
      - mgmt_ip: str
    """

    # ─── Identity + filesystem prep ──────────────────────────────────

    @step("prepare_dirs")
    def step_prepare_dirs(self, ctx):
        """Create /opt/bedrock + /var/lib/bedrock structure. Idempotent
        (mkdir -p)."""
        from lib import mgmt_install as _m
        # Use the legacy constants until storage/lvm + paths move.
        for d in (_m.BINARIES, _m.DATA / "vm", _m.DATA / "vl", _m.MGMT,
                  _m.BEDROCK_BASE / "iso"):
            d.mkdir(parents=True, exist_ok=True)

    @step("allocate_identity")
    def step_allocate_identity(self, ctx):
        """Generate cluster_uuid + derive loopback_ip + write state.json.
        Idempotent: re-uses existing values from state.json if present."""
        from lib import state as _state, cluster_addr as _ca
        import uuid as _uuid

        s = _state.load()
        hw = s.get("hardware", {})
        s["cluster_name"] = ctx["cluster_name"]
        s["cluster_uuid"] = s.get("cluster_uuid") or str(_uuid.uuid4())
        s["role"] = "mgmt+compute"
        s["node_id"] = 0
        s["node_name"] = s.get("node_name") or hw.get("hostname", "node1")
        s["mgmt_ip"] = self._pick_mgmt_ip(hw)
        s["mgmt_url"] = f"https://{s['mgmt_ip']}:8443"
        s["loopback_ip"] = _ca.node_loopback_ip(s["cluster_uuid"], 1)
        _state.save(s)
        ctx.update({
            "cluster_uuid": s["cluster_uuid"],
            "node_name": s["node_name"],
            "loopback_ip": s["loopback_ip"],
            "mgmt_ip": s["mgmt_ip"],
        })

    @step("write_cluster_key")
    def step_write_cluster_key(self, ctx):
        """Generate /etc/bedrock/cluster.key (32 random bytes, AEAD key).
        Idempotent: respects existing file."""
        from lib import daemon_setup as _ds
        _ds.write_cluster_key()

    @step("write_bootstrap_cluster_json")
    def step_write_bootstrap_cluster_json(self, ctx):
        """Write /etc/bedrock/cluster.json with this node as the only
        member. The orchestrator's rqlite-snapshot task overwrites
        this once rqlite is up. Idempotent — writes verbatim every
        time."""
        import json as _json
        cluster = {
            "cluster_uuid": ctx["cluster_uuid"],
            "cluster_name": ctx["cluster_name"],
            "mgmt_master": ctx["node_name"],
            "nodes": {
                ctx["node_name"]: {
                    "host": ctx["mgmt_ip"],
                    "loopback_ip": ctx["loopback_ip"],
                    "role": "mgmt+compute",
                    "pubkey": "",
                    "bedrock_pubkey": "",
                },
            },
            "tiers": {}, "witnesses": {}, "params": {},
            "vms": {}, "backup_targets": {}, "paths": {},
            "operators": {}, "join_requests": {},
            "obs_backends": {"metrics": [], "logs": []},
            "log_index": 0,
        }
        Path("/etc/bedrock/cluster.json").write_text(
            _json.dumps(cluster, indent=2))

    # ─── Observability binaries + units ──────────────────────────────

    @step("install_obs_binaries")
    def step_install_obs_binaries(self, ctx):
        """Download VictoriaMetrics + VictoriaLogs from the install
        repo. Idempotent — skips if binary already at expected path."""
        from lib import mgmt_install as _m
        import os as _os
        repo = ctx["repo"]
        for name in ("victoria-metrics", "victoria-logs"):
            dst = _m.BINARIES / name
            if dst.exists():
                continue
            _m._download(f"{repo}/binaries/{name}", dst)
            _os.chmod(dst, 0o755)

    @step("install_exporters")
    def step_install_exporters(self, ctx):
        """node_exporter + vm_exporter for this node. Idempotent."""
        from lib import exporters as _e
        _e.install(ctx["repo"])

    @step("write_obs_services")
    def step_write_obs_services(self, ctx):
        """Write systemd units for bedrock-vm, bedrock-vl + dashboard
        + the prometheus scrape config. Idempotent — overwrites with
        current content (cheap)."""
        from lib import mgmt_install as _m, dashboard_install as _di
        import textwrap as _tw

        mgmt_ip = ctx["mgmt_ip"]
        cluster_name = ctx["cluster_name"]
        scrape_conf = _tw.dedent(f"""\
            scrape_configs:
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
            """)
        (_m.BEDROCK_BASE / "scrape.yml").write_text(scrape_conf)
        _m._write_systemd("bedrock-vm", _tw.dedent(f"""\
            [Unit]
            Description=Bedrock VictoriaMetrics
            After=network.target

            [Service]
            ExecStart={_m.BINARIES}/victoria-metrics -storageDataPath={_m.DATA}/vm -promscrape.config={_m.BEDROCK_BASE}/scrape.yml -retentionPeriod=90d -httpListenAddr=:8428
            Restart=always

            [Install]
            WantedBy=multi-user.target
            """))
        _m._write_systemd("bedrock-vl", _tw.dedent(f"""\
            [Unit]
            Description=Bedrock VictoriaLogs
            After=network.target

            [Service]
            ExecStart={_m.BINARIES}/victoria-logs -storageDataPath={_m.DATA}/vl -httpListenAddr=:9428 -syslog.listenAddr.tcp=:5140
            Restart=always

            [Install]
            WantedBy=multi-user.target
            """))
        _di.install_dashboard(ctx["repo"], with_metrics=True)

    @step("start_obs_services")
    def step_start_obs_services(self, ctx):
        """Enable + start bedrock-vm and bedrock-vl. Idempotent
        (systemctl enable --now is a no-op if already on)."""
        subprocess.run(
            ["systemctl", "enable", "--now",
             "bedrock-vm.service", "bedrock-vl.service"],
            check=False, timeout=30,
        )

    # ─── Storage ─────────────────────────────────────────────────────

    @step("provision_storage_n1")
    def step_provision_storage_n1(self, ctx):
        """LVM thinpool + local tier LVs. Idempotent — every helper
        checks existence first. write_rqlite=False because rqlite
        isn't up yet; mirror happens after step seed_cluster_state."""
        from lib import tier_storage as _ts
        _ts.setup_n1(write_rqlite=False)

    @step("bootstrap_cluster_ca")
    def step_bootstrap_cluster_ca(self, ctx):
        """Generate the cluster TLS CA + sign this master's per-node cert
        + sign the arbiter cert. Must run BEFORE rqlited starts (rqlited
        unit reads the cert files at process start; no hot-reload).

        Files written:
          /var/lib/bedrock/cluster/ca/ca.{key,crt}   — CA (master only)
          /var/lib/bedrock/cluster/ca/arbiter.{key,key.pem,crt}
                                                      — arbiter TLS
          /etc/bedrock/ca.crt                         — replicated CA cert
          /etc/bedrock/node.crt                       — master's node cert
          /etc/bedrock/node.key.pem                   — PEM of master's seed

        At N=1, /var/lib/bedrock/cluster is a plain dir on the root FS;
        tier_storage.promote_local_to_drbd_master snapshots+restores its
        contents during the N=1→N=2 promote, so the CA migrates onto the
        DRBD volume automatically when storage promotes. Same paths
        before and after.

        Idempotent: cluster_ca.generate_ca / generate_arbiter_keypair_and_cert
        both check for existing files. Master's node cert is re-signed on
        every run (cheap; deterministic from the same key+SAN)."""
        from lib import cluster_ca as _ca, peer_auth as _pa

        # 1. Ensure /var/lib/bedrock/cluster exists (cluster_arbiter
        #    promote_to_arbiter_host creates this on every promote tick,
        #    but at init time we may be there first).
        from pathlib import Path as _P
        _P("/var/lib/bedrock/cluster").mkdir(parents=True, exist_ok=True)

        # 2. Generate the CA (idempotent).
        _ca.generate_ca(cluster_name=ctx["cluster_name"])

        # 3. Ensure peer_auth keypair exists and sign this master's
        #    per-node cert. peer_auth.ensure_node_key is lazy — calling
        #    it here also covers any later step (e.g. seed_cluster_state)
        #    that expects the keypair on disk.
        priv_seed, pub_raw = _pa.ensure_node_key()
        node_cert_pem = _ca.sign_node_cert(
            pub_raw, ctx["node_name"], ctx["loopback_ip"])
        _ca.install_node_cert(
            node_cert_pem=node_cert_pem,
            ca_cert_pem=_ca.CA_CERT_DRBD.read_bytes(),
            node_seed=priv_seed,
        )

        # 4. Sign the arbiter cert. The arbiter's loopback IP is the
        #    cluster VIP (.254) — single source in cluster_addr.cluster_vip.
        from lib import cluster_addr as _caddr
        arbiter_ip = _caddr.cluster_vip(ctx["cluster_uuid"])
        _ca.generate_arbiter_keypair_and_cert(arbiter_ip)

    # ─── rqlite ──────────────────────────────────────────────────────

    @step("render_rqlited_env")
    def step_render_rqlited_env(self, ctx):
        """Write /etc/bedrock/rqlited.env from cluster.json + state.json.
        Idempotent — overwrites file with current rendering."""
        from lib import rqlite_setup as _rqs
        _rqs.render_env_file()

    @step("start_rqlited")
    def step_start_rqlited(self, ctx):
        """Enable + start bedrock-rqlited; poll /status until raft=Leader.
        Idempotent — restart on already-running rqlited is cheap.
        FAILS LOUD on 30s timeout; the seed step needs a writable
        leader, anything else is unrecoverable here."""
        import json as _json
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
                 "--cert", "/etc/bedrock/node.crt",
                 "--key",  "/etc/bedrock/node.key.pem",
                 "--cacert", "/etc/bedrock/ca.crt",
                 "https://127.0.0.1:4001/status"],
                capture_output=True,
            )
            if rc.returncode != 0:
                continue
            try:
                last_state = _json.loads(
                    rc.stdout.decode())["store"]["raft"]["state"]
            except Exception:
                continue
            if last_state == "Leader":
                return
        raise RuntimeError(
            f"rqlited didn't reach Leader within 30s "
            f"(last raft state: {last_state}); "
            f"check `journalctl -u bedrock-rqlited`")

    @step("apply_schema")
    def step_apply_schema(self, ctx):
        """Apply bedrock_schema.sql to rqlite. Idempotent — every
        CREATE uses IF NOT EXISTS."""
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            _st.apply_schema(client, str(_st.schema_path()))

    @step("seed_cluster_state")
    def step_seed_cluster_state(self, ctx):
        """Insert cluster_info + this node + operator + mgmt_master +
        obs_backends. Each helper is INSERT OR REPLACE so re-running
        on a partially-seeded cluster converges."""
        from bedrock_d import state as _st
        from lib import peer_auth as _pa, operator_auth as _oa

        try:
            master_pubkey = Path("/root/.ssh/id_ed25519.pub") \
                .read_text().strip()
        except OSError:
            master_pubkey = ""
        salt, phash = _oa.hash_password("admin")
        master_bedrock_pub = _pa.pubkey_hex()

        with _st.RqliteClient() as client:
            _st.cluster_init(
                cluster_uuid=ctx["cluster_uuid"],
                cluster_name=ctx["cluster_name"],
                client=client,
            )
            _st.node_register(
                node_name=ctx["node_name"],
                host=ctx["mgmt_ip"],
                role="mgmt+compute",
                pubkey=master_pubkey,
                bedrock_pubkey=master_bedrock_pub,
                client=client,
            )
            _st.node_loopback(
                node_name=ctx["node_name"],
                loopback_ip=ctx["loopback_ip"],
                client=client,
            )
            _st.operator_set(
                username="root", salt=salt, password_hash=phash,
                client=client,
            )
            _st.obs_backends_set(
                metrics=[ctx["node_name"]], logs=[ctx["node_name"]],
                client=client,
            )
            _st.set_mgmt_master(ctx["node_name"], client=client)

    @step("mirror_tier_state")
    def step_mirror_tier_state(self, ctx):
        """Push local tier_state (set in provision_storage_n1) into
        rqlite. Idempotent — bedrock_state.tier_state is INSERT OR
        REPLACE."""
        from lib import tier_storage as _ts
        _ts.mirror_tier_state_to_rqlite()

    # ─── bedrock-d ───────────────────────────────────────────────────

    @step("start_bedrock_d")
    def step_start_bedrock_d(self, ctx):
        """Enable + start the unified bedrock-d daemon. Idempotent."""
        subprocess.run(["systemctl", "daemon-reload"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "reset-failed",
                        "bedrock-d.service"],
                       check=False, timeout=10)
        subprocess.run(["systemctl", "enable", "--now",
                        "bedrock-d.service"],
                       check=False, timeout=30)

    # ─── SeaweedFS ───────────────────────────────────────────────────

    @step("seaweedfs_install")
    def step_seaweedfs_install(self, ctx):
        """Confirm /usr/local/bin/weed is present (staged by install.sh).
        Idempotent — just a check."""
        from lib import seaweedfs as _sw
        _sw.ensure_install()

    @step("seaweedfs_configs")
    def step_seaweedfs_configs(self, ctx):
        """Render master.toml + filer.toml + s3.json + seaweedfs.env.
        Idempotent — overwrites with current rendering."""
        from lib import seaweedfs as _sw
        _sw.write_env_file()
        _sw.write_master_config()
        _sw.write_filer_config()
        _sw.write_s3_config()

    @step("seaweedfs_start_local")
    def step_seaweedfs_start_local(self, ctx):
        """Enable + start weed-master (if in Raft-3 set) + weed-volume +
        weed-s3 on this node. Idempotent."""
        from lib import seaweedfs as _sw
        _sw.promote_to_master_volume_host()

    @step("seaweedfs_start_filer")
    def step_seaweedfs_start_filer(self, ctx):
        """Start the filer singleton. At N=1 this runs on the local
        loopback (no DRBD yet); at N≥2 cluster_arbiter has flipped
        the .254 VIP and mounted the DRBD volume first. Idempotent."""
        from lib import seaweedfs as _sw
        _sw.promote_to_filer_host()
        # Wait briefly for S3 to bind so post-init smoke tests can
        # PUT objects without ECONNREFUSED.
        import socket as _sock
        for _ in range(30):
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", 8333))
                return
            except OSError:
                pass
            finally:
                s.close()
            time.sleep(0.5)
        # If S3 didn't come up, surface that — it's not catastrophic
        # (clients retry) but indicates a configuration drift.
        log.warning("weed-s3 didn't bind 127.0.0.1:8333 in 15 s")

    @step("seaweedfs_init_collections")
    def step_seaweedfs_init_collections(self, ctx):
        """Configure scratch (000) / standard (001) / critical (002)
        collections via `weed shell`. Idempotent — `fs.configure
        -apply` overwrites the previous config for the same
        locationPrefix."""
        from lib import seaweedfs as _sw
        _sw.init_collections()

    @step("seed_iso_library")
    def step_seed_iso_library(self, ctx):
        """Copy any ISOs staged at /opt/bedrock/iso/ into the filer
        namespace under /mnt/bedrock/iso/. Idempotent — skips files
        that already exist in the filer."""
        from lib import seaweedfs as _sw
        try:
            _sw.seed_iso_library()
        except Exception as e:
            # ISO seeding is convenience; don't fail init for it.
            log.warning("seed_iso_library: %s", e)

    # ─── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _pick_mgmt_ip(hw: dict) -> str:
        """Same logic as legacy mgmt_install._pick_mgmt_ip; copied
        here so the saga is self-contained. Picks the first non-
        loopback IPv4 from the detected hardware."""
        from lib import mgmt_install as _m
        return _m._pick_mgmt_ip(hw)
