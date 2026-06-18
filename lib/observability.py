"""Metrics + logs HA: 2 designated single-binary backends, agents on every node.

State model lives in the cluster snapshot under `obs_backends`:

    {"metrics": [<node_name_1>, <node_name_2>],
     "logs":    [<node_name_1>, <node_name_2>]}

Set via the `OBS_BACKENDS_SET` log entry. Each node's reactor calls
`reconcile()` on every fold; the function compares "what the snapshot
says my node should run" vs. "what systemd has running" and converges:

  - Always: `bedrock-vmagent` + `bedrock-vlagent` running, configured
    with the current backend URLs (from `nodes[backend].host`).
  - If this node is in `obs_backends.metrics`: `bedrock-vm` running.
  - If this node is in `obs_backends.logs`:    `bedrock-vl` running.

The reconciler is **idempotent** — running it twice in a row writes
nothing the second time. That's the contract that lets the orchestrator
call it on every log fold without burning CPU or restarting daemons.

Promotion of a new backend (the 1→2 transition) is triggered
separately by `mgmt/app.py:join_approve()` — when the cluster has only
1 metrics/logs backend at the moment a new node joins, mgmt appends
`OBS_BACKENDS_SET` adding the joiner to slot #2. The seed step
(vmbackup + ship + vmrestore so the new backend has historical data)
is done synchronously inside `join_approve()` so the snapshot doesn't
advertise a backend that has nothing in it — see `seed_backend()`.

Out of scope for v1.0:
  - Decommissioning a backend (promote a spare). Operator-driven later.
  - N≥3 cluster mode (vminsert/vmselect). Not needed in this design.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# Paths owned by this module.
VMAGENT_BIN = "/opt/bedrock/bin/vmagent"
VLAGENT_BIN = "/opt/bedrock/bin/vlagent"
VM_BIN      = "/opt/bedrock/bin/victoria-metrics"
VL_BIN      = "/opt/bedrock/bin/victoria-logs"
VMBACKUP_BIN  = "/opt/bedrock/bin/vmbackup"
VMRESTORE_BIN = "/opt/bedrock/bin/vmrestore"

VMAGENT_QUEUE = "/var/lib/bedrock/vmagent-queue"
VLAGENT_QUEUE = "/var/lib/bedrock/vlagent-queue"
VM_DATA       = "/opt/bedrock/data/vm"
VL_DATA       = "/opt/bedrock/data/vl"
SCRAPE_FILE   = "/opt/bedrock/scrape.yml"

UNIT_VMAGENT = "bedrock-vmagent.service"
UNIT_VLAGENT = "bedrock-vlagent.service"
UNIT_VM      = "bedrock-vm.service"
UNIT_VL      = "bedrock-vl.service"

# Journal → syslog → vlagent (:5140) → VictoriaLogs. Drop-ins only; the
# stock rsyslog.conf / journald.conf are left untouched.
JOURNAL_FORWARD_DROPIN = "/etc/systemd/journald.conf.d/50-bedrock-forward.conf"
RSYSLOG_VLAGENT_DROPIN = "/etc/rsyslog.d/50-bedrock-vlagent.conf"


def _write_if_changed(path: str, content: str, mode: int = 0o644) -> bool:
    """Atomic tmp+rename write; return True only if the file changed."""
    p = Path(path)
    if p.exists():
        try:
            if p.read_text() == content:
                return False
        except Exception:
            pass
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content)
    os.chmod(tmp, mode)
    tmp.replace(p)
    return True


def _run(cmd: str) -> None:
    subprocess.run(cmd, shell=True, check=False, capture_output=True, timeout=30)


def _systemd_want(unit: str, want_running: bool, restart_if_running: bool = False) -> None:
    """Ensure `unit` is in the requested state. Idempotent — no-ops if
    state already matches. `restart_if_running=True` is used after the
    unit file content changed."""
    is_active = subprocess.run(f"systemctl is-active {unit}", shell=True,
                               capture_output=True, text=True).stdout.strip() == "active"
    if want_running and not is_active:
        # Clear any failed / start-limit state first. A unit that
        # crash-looped (e.g. vmagent before scrape.yml existed) trips
        # systemd's StartLimit and then refuses `start` with "start
        # request repeated too quickly" even once the cause is fixed —
        # reset-failed lets it recover on the next start.
        _run(f"systemctl reset-failed {unit}")
        _run(f"systemctl enable --now {unit}")
    elif want_running and is_active and restart_if_running:
        _run(f"systemctl restart {unit}")
    elif not want_running and is_active:
        _run(f"systemctl disable --now {unit}")


def _backend_url(snapshot: dict, node_name: str, port: int) -> str:
    """Resolve a node name to its reachable URL. Prefer loopback_ip
    (the cluster mesh /32, multi-path failover via bedrock-net); fall
    back to the mgmt host during the bootstrap window."""
    n = (snapshot.get("nodes") or {}).get(node_name) or {}
    addr = n.get("loopback_ip") or n.get("host", "")
    return f"http://{addr}:{port}" if addr else ""


# ── Unit files (generated from the snapshot) ────────────────────────

def _write_scrape_file(snapshot: dict, self_name: str) -> bool:
    """Ensure scrape.yml exists so vmagent can start.

    vmagent runs with `-promscrape.config=SCRAPE_FILE` and EXITS FATALLY
    at startup if that file is missing. cluster_init only writes it on
    the INIT node, so without this every joiner's vmagent crash-loops
    ("cannot read /opt/bedrock/scrape.yml"). Every node that runs vmagent
    must have it — written here as part of reconcile.

    Each node scrapes its OWN local exporters (node-exporter :9100 +
    libvirt/vm metrics :9177); vmagent dual-writes the samples to both
    backends. If this node's identity isn't in the snapshot yet (still
    settling at boot/join), write a valid EMPTY config so vmagent can
    start as a pure forwarder; a later reconcile fills in the real
    targets. Returns True if the content changed."""
    n = (snapshot.get("nodes") or {}).get(self_name) or {}
    addr = n.get("host") or n.get("loopback_ip") or ""
    cluster = snapshot.get("cluster_name", "") or ""
    if addr:
        body = (
            "scrape_configs:\n"
            "  - job_name: node\n"
            "    scrape_interval: 10s\n"
            "    static_configs:\n"
            f"      - targets: ['{addr}:9100']\n"
            "        labels:\n"
            f"          cluster: {cluster}\n"
            "  - job_name: libvirt\n"
            "    scrape_interval: 10s\n"
            "    static_configs:\n"
            f"      - targets: ['{addr}:9177']\n"
            "        labels:\n"
            f"          cluster: {cluster}\n"
        )
    else:
        body = "scrape_configs: []\n"
    return _write_if_changed(SCRAPE_FILE, body)


def _vmagent_unit(metrics_backends: list[str], snapshot: dict) -> str:
    """vmagent dual-writes to each metrics backend's :8428. The persistent
    disk queue at -remoteWrite.tmpDataPath survives reboots and replays
    on backend recovery — that's the only convergence mechanism between
    backends, so don't skimp on the buffer size.
    `-promscrape.config` points at the existing scrape.yml the mgmt
    master maintains. Followers have an empty/stub scrape.yml so vmagent
    just acts as a forwarder."""
    remotes = " ".join(
        f"-remoteWrite.url={_backend_url(snapshot, n, 8428)}/api/v1/write"
        for n in metrics_backends
        if _backend_url(snapshot, n, 8428))
    return f"""[Unit]
Description=Bedrock metrics agent (vmagent, dual-writes to both VM backends)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p {VMAGENT_QUEUE}
ExecStart={VMAGENT_BIN} \\
  -promscrape.config={SCRAPE_FILE} \\
  -remoteWrite.tmpDataPath={VMAGENT_QUEUE} \\
  -remoteWrite.maxDiskUsagePerURL=8GB \\
  {remotes}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


def _vlagent_unit(logs_backends: list[str], snapshot: dict) -> str:
    """vlagent ingests syslog on :5140 and dual-writes to both VL backends.
    Host journal is shipped here by rsyslog (see reconcile_journal_forward)."""
    remotes = " ".join(
        f"-remoteWrite.url={_backend_url(snapshot, n, 9428)}/internal/insert"
        for n in logs_backends
        if _backend_url(snapshot, n, 9428))
    return f"""[Unit]
Description=Bedrock logs agent (vlagent, dual-writes to both VL backends)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p {VLAGENT_QUEUE}
ExecStart={VLAGENT_BIN} \\
  -remoteWrite.tmpDataPath={VLAGENT_QUEUE} \\
  -remoteWrite.maxDiskUsagePerURL=8GB \\
  -syslog.listenAddr.tcp=:5140 \\
  {remotes}
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


def _vm_unit() -> str:
    return f"""[Unit]
Description=Bedrock VictoriaMetrics backend (single-binary, RF=2 via agent dual-write)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p {VM_DATA}
ExecStart={VM_BIN} \\
  -storageDataPath={VM_DATA} \\
  -retentionPeriod=90d \\
  -httpListenAddr=:8428
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


def _vl_unit() -> str:
    return f"""[Unit]
Description=Bedrock VictoriaLogs backend (single-binary, RF=2 via agent dual-write)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStartPre=/bin/mkdir -p {VL_DATA}
ExecStart={VL_BIN} \\
  -storageDataPath={VL_DATA} \\
  -httpListenAddr=:9428 \\
  -syslog.listenAddr.tcp=:5141
Restart=on-failure
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


# ── Backend-start gate ──────────────────────────────────────────────
# We must NOT start `bedrock-vm` on a freshly-promoted backend while its
# data dir is still empty. If we did, agents would dual-write to the
# empty backend (no queue building up) — and then the seed step would
# wipe those writes when it overwrites the data dir, leaving a 2-3s
# gap on the new backend. Instead, the agents see OBS_BACKENDS_SET
# FIRST and start *buffering* their writes for the new target (because
# it's not accepting yet). Mgmt then seeds the data dir and starts the
# backend daemon. Agents drain their buffers → no gap.
#
# VL has no seed path (VictoriaLogs 1.50 has no online snapshot
# endpoint), so VL bypasses the gate and starts as soon as it's named
# a backend — the dual-write keeps both VL backends identical from
# promotion onward; historical data starts fresh on the new one.


def _data_dir_seeded(data_dir: str) -> bool:
    """True if `data_dir` contains real parts (vs. empty scaffolding).
    VM's parts live at data/<small|big|indexdb>/<period>/<part_id>/values.bin
    (or items.bin for indexdb). Any of those means we have data."""
    p = Path(data_dir)
    if not p.is_dir():
        return False
    for marker in ("values.bin", "items.bin"):
        try:
            next(p.rglob(marker))
            return True
        except StopIteration:
            continue
    return False


def _can_start_vm_backend(snapshot: dict, self_name: str) -> bool:
    """Reactor's "should I start bedrock-vm on this node?" rule.
    - Not in the backend list → no.
    - Solo backend (cluster init: only one backend exists) → yes,
      there's nothing to seed from; start fresh and become the source.
    - One of multiple backends → only start when the data dir has been
      seeded by mgmt's vmbackup-vmrestore pipeline. Until then,
      agents queue their writes for me in their disk buffers."""
    backends = (snapshot.get("obs_backends") or {}).get("metrics") or []
    if self_name not in backends:
        return False
    if len(backends) <= 1:
        return True
    return _data_dir_seeded(VM_DATA)


def _journal_forward_dropin() -> str:
    return """# Bedrock — forward journal entries to syslog for vlagent pickup.
# Managed by lib/observability.py
[Journal]
ForwardToSyslog=yes
"""


def _rsyslog_vlagent_dropin() -> str:
    return """# Bedrock — ship journal (via ForwardToSyslog) to local vlagent (:5140).
# Managed by lib/observability.py — do not edit by hand.

# Avoid feedback loops when vl/VL backend logs re-enter the pipeline.
if ($programname == 'vlagent' or $programname == 'victoria-logs') then stop

*.* action(
  type="omfwd"
  protocol="tcp"
  target="127.0.0.1"
  port="5140"
  template="RSYSLOG_SyslogProtocol23Format"
  action.resumeRetryCount="-1"
  queue.type="LinkedList"
  queue.size="20000"
  queue.saveonshutdown="on"
)
"""


def _ensure_rsyslog_package() -> None:
    """rsyslog is the journal→TCP bridge; install if the ISO/bootstrap skipped it."""
    if subprocess.run("rpm -q rsyslog", shell=True,
                      capture_output=True).returncode != 0:
        _run("dnf install -y -q rsyslog")


def reconcile_journal_forward() -> bool:
    """Ensure journald forwards to syslog and rsyslog ships to vlagent.

    Idempotent: writes two drop-ins, (re)starts services only when content
    changed. Returns True if any drop-in changed."""
    _ensure_rsyslog_package()
    j_changed = _write_if_changed(JOURNAL_FORWARD_DROPIN, _journal_forward_dropin())
    r_changed = _write_if_changed(RSYSLOG_VLAGENT_DROPIN, _rsyslog_vlagent_dropin())
    _run("systemctl enable --now rsyslog")
    if j_changed:
        _run("systemctl restart systemd-journald")
    if r_changed:
        _run("systemctl restart rsyslog")
    return j_changed or r_changed


# ── Reconciler ──────────────────────────────────────────────────────

def reconcile(snapshot: dict, self_name: str) -> None:
    """Converge this node's systemd state to match what the snapshot
    asks for. Called from the orchestrator's log-fold subscriber on
    every entry — must be fast + idempotent."""
    obs = (snapshot.get("obs_backends") or {})
    metrics_backends = list(obs.get("metrics") or [])
    logs_backends    = list(obs.get("logs") or [])

    # No backends configured yet → keep this a no-op. The mgmt master
    # at `bedrock init` time appends `OBS_BACKENDS_SET` with itself as
    # the first metrics+logs backend, so any cluster that completed
    # init has at least slot 1 filled.
    if not metrics_backends and not logs_backends:
        return

    # Agent unit files: write whenever they change, restart-if-running
    # to pick up the new backend URLs.
    if metrics_backends:
        # vmagent EXITS FATALLY at startup if SCRAPE_FILE is missing, so
        # write it on EVERY node before (re)starting vmagent — cluster_init
        # only writes it on the init node, which left joiners' vmagent
        # crash-looping. If only the scrape file changed vmagent auto-
        # reloads -promscrape.config, so that alone needs no restart.
        _write_scrape_file(snapshot, self_name)
        ch = _write_if_changed(
            f"/etc/systemd/system/{UNIT_VMAGENT}",
            _vmagent_unit(metrics_backends, snapshot))
        if ch:
            _run("systemctl daemon-reload")
        _systemd_want(UNIT_VMAGENT, want_running=True, restart_if_running=ch)

    if logs_backends:
        reconcile_journal_forward()
        ch = _write_if_changed(
            f"/etc/systemd/system/{UNIT_VLAGENT}",
            _vlagent_unit(logs_backends, snapshot))
        if ch:
            _run("systemctl daemon-reload")
        _systemd_want(UNIT_VLAGENT, want_running=True, restart_if_running=ch)

    # Backend daemons. Unit files are always written when we're a
    # backend, but bedrock-vm only STARTS when the seed gate allows
    # (see `_can_start_vm_backend` for the rule). Mgmt's
    # `observability_promote` / `join_approve` runs the seed and then
    # invokes `systemctl start bedrock-vm` directly over SSH; the next
    # reactor cycle confirms the running state matches the snapshot.
    want_vm = self_name in metrics_backends
    want_vl = self_name in logs_backends

    if want_vm:
        ch = _write_if_changed(f"/etc/systemd/system/{UNIT_VM}", _vm_unit())
        if ch:
            _run("systemctl daemon-reload")
        if _can_start_vm_backend(snapshot, self_name):
            _systemd_want(UNIT_VM, want_running=True, restart_if_running=ch)
        # else: we're a backend but not yet seeded. Do NOT start. Do NOT
        # disable either — mgmt's post-seed `systemctl start` will bring
        # it up and subsequent reactor cycles will see it healthy.
    else:
        _systemd_want(UNIT_VM, want_running=False)

    if want_vl:
        ch = _write_if_changed(f"/etc/systemd/system/{UNIT_VL}", _vl_unit())
        if ch:
            _run("systemctl daemon-reload")
        # VL has no online snapshot endpoint, so no seed gate — start
        # immediately. Historical data is the accepted gap; from now on
        # vlagent dual-write keeps both backends identical.
        _systemd_want(UNIT_VL, want_running=True, restart_if_running=ch)
    else:
        _systemd_want(UNIT_VL, want_running=False)


# ── Seed (vmbackup → ship → vmrestore at promotion) ────────────────

def seed_backend(source_host: str, target_host: str,
                 ssh_runner, sftp_runner, *, force: bool = False) -> dict:
    """Copy the existing backend's VM + VL data dirs to a new backend
    BEFORE its daemons start. Synchronous — caller (mgmt's
    /api/join/approve) blocks the joiner's poll until this finishes.

    `ssh_runner(host, cmd) -> (output, rc)` and `sftp_runner(host,
    local, remote)` are injected so we don't import paramiko here.

    Strategy: VM/VL each expose `/snapshot/create` which atomically
    hardlinks the current data into a snapshot dir. We tar that into a
    stream over SSH and untar on the target. No need for vmbackup-prod
    against a remote object store; LAN tar-over-ssh is faster and we
    don't need cross-version compat (both ends are running the same
    binary).

    Idempotent: if the target's data dir is already non-empty, skip
    (a half-finished seed will look non-empty too — that's the
    operator-restart edge case; documented gap)."""
    report = {"metrics": "skipped", "logs": "skipped"}

    # Helpers we'll call.
    def _data_dir_empty(host: str, path: str) -> bool:
        out, _ = ssh_runner(host, f"[ -d {path} ] && [ -n \"$(ls -A {path} 2>/dev/null)\" ] && echo no || echo yes")
        return out.strip() == "yes"

    def _vmbackup_seed(source: str, target: str,
                       data_dir: str, port: int) -> str:
        """Use vmbackup → ssh-tar → vmrestore to populate target's
        data dir with a runnable copy of source's TSDB.

        `vmbackup` snapshots the source VM via its `/snapshot/create`
        API, then writes the data to fs://<stage>. `vmrestore` reads
        that stage on the other host and lays the data out into the
        target's storage dir. We ship the stage dir between hosts
        with one tar-over-ssh — keeps the observability path free of
        a shared-filesystem dependency.

        Bypasses the symlink-in-snapshot issue that plain
        `tar` on the raw snapshot directory hit: vmbackup writes real
        file copies into the stage; the symlinks in VM's `snapshots/`
        tree never appear in the archive."""
        import time
        stage_name = f"vmbackup-seed-{int(time.time())}"
        stage_src  = f"/tmp/{stage_name}"
        stage_dst  = f"/tmp/{stage_name}"

        # 1. vmbackup on source.
        _, rc = ssh_runner(source,
            f"{VMBACKUP_BIN} "
            f"-storageDataPath={data_dir} "
            f"-snapshot.createURL=http://127.0.0.1:{port}/snapshot/create "
            f"-snapshot.deleteURL=http://127.0.0.1:{port}/snapshot/delete "
            f"-dst=fs://{stage_src} 2>&1",
            timeout=600)
        if rc != 0:
            return f"vmbackup rc={rc}"

        # 2. Ship the stage to target.
        _, rc = ssh_runner(source,
            f"cd /tmp && tar -czf - {stage_name} | "
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"root@{target} 'cd /tmp && tar -xzf -'",
            timeout=600)
        if rc != 0:
            ssh_runner(source, f"rm -rf {stage_src}")
            return f"tar-over-ssh rc={rc}"

        # 3. vmrestore on target into a CLEAN data dir.
        _, rc = ssh_runner(target,
            f"rm -rf {data_dir} && mkdir -p {data_dir} && "
            f"{VMRESTORE_BIN} -src=fs://{stage_dst} "
            f"-storageDataPath={data_dir} 2>&1",
            timeout=600)
        # 4. Clean up stage dirs both sides regardless of outcome.
        ssh_runner(source, f"rm -rf {stage_src}", timeout=30)
        ssh_runner(target, f"rm -rf {stage_dst}", timeout=30)
        if rc != 0:
            return f"vmrestore rc={rc}"
        return "ok"

    # Metrics seed — vmbackup → ssh-tar → vmrestore. `force=True` wipes
    # any leftover data first, used by the --replace path where the
    # target node might have stale data from a previous time as backend.
    if force or _data_dir_empty(target_host, VM_DATA):
        if force and not _data_dir_empty(target_host, VM_DATA):
            # Stop the daemon (idempotent — may not be running) and
            # wipe so vmrestore sees a clean canvas. The reconciler's
            # seed-gate would have kept bedrock-vm stopped anyway when
            # the snapshot first added this node, but a previous
            # tenancy might have left it running.
            ssh_runner(target_host, "systemctl stop bedrock-vm 2>/dev/null", timeout=15)
            ssh_runner(target_host, f"rm -rf {VM_DATA}", timeout=60)
        report["metrics"] = _vmbackup_seed(
            source_host, target_host, VM_DATA, 8428)

    # Logs seed — VictoriaLogs 1.50 does NOT expose `/snapshot/create`.
    # No online snapshot path exists today, so we cannot seed the new
    # backend's history without stopping VL on the source (write
    # downtime) or implementing a vlbackup-based path. v1.0 ships
    # "logs start fresh on the 2nd backend"; vlagent dual-write keeps
    # both backends identical from promotion onward.
    # When VL gains an online snapshot endpoint, swap `_snapshot_and_ship`
    # in here using port 9428.
    if _data_dir_empty(target_host, VL_DATA):
        report["logs"] = "skipped (VL has no online snapshot; starts fresh)"
    return report
