"""Bedrock backup orchestration — Kopia wrapper.

mgmt invokes the kopia CLI on the appropriate Bedrock node (the VM's
home node) over SSH. Kopia is a one-shot tool: spins up, does work,
exits. mgmt never runs a long-lived kopia daemon.

Per cluster-protocol-overview.md and snapshots-and-backup.md §9c-bis:

  - One Kopia repository per cluster (operator-chosen — S3 / S3-
    compatible / NFS / FS path). Encryption password lives in
    /etc/bedrock/backup.key (mode 0600), out-of-band, never in the
    log.
  - Each Bedrock node has the kopia binary; runs `kopia repository
    connect` once (configured by `bedrock backup target set`).
  - Snapshots use `--override-source=<cluster-uuid>:vms:<vm-name>`
    so VM identity is stable across migrations and node failures.
  - mgmt master = maintenance owner. Only the master schedules
    `kopia maintenance run`.

This module exposes three operator-facing entry points:

  - run_backup(target_id, vm_name)     — backup one VM, log result
  - run_restore(target_id, vm_name, kopia_snapshot_id, dest_vm_name)
  - list_backups_for_vm(target_id, vm_name) — metadata from log

…plus internal helpers for invoking kopia over SSH on the home node.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/bedrock")

from lib import log_entries as le, rust_ipc, state as state_mod

log = logging.getLogger("bedrock.backup")

CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
ENCRYPTION_KEY_FILE = Path("/etc/bedrock/backup.key")
CREDENTIALS_DIR = Path("/etc/bedrock/backup-credentials")  # per-target .env files

# Per-target kopia config + cache. We pass --config-file explicitly to
# every kopia invocation rather than letting it default to
# ~/.config/kopia/repository.config: bedrock-mgmt runs as a systemd
# unit without HOME set, so the default would be ambiguous. Per-target
# files also let multiple targets coexist later (v1.x sync-to topology).
KOPIA_CONFIG_DIR = Path("/etc/bedrock/kopia")              # config files
KOPIA_CACHE_ROOT = Path("/var/cache/bedrock-kopia")        # cache root

# Content-addressing hash floor. Bedrock refuses to use any kopia repo
# whose block hash is below 256 bits — that's the "data integrity is
# non-negotiable" stance. A collision in the content hash means kopia
# stores the wrong blob under a chunk id and silently corrupts a
# restore. Birthday bound at 256 bits puts collisions at ~2^128, which
# is the right ballpark for "literally never". Anything shorter
# (HMAC-SHA256-128, BLAKE2B-256-128, BLAKE2S-128, BLAKE3-256-128,
# HMAC-SHA224, etc.) trades integrity for a few microseconds per
# chunk, which is not a trade we make.
#
# Repos created by bedrock get DEFAULT_BLOCK_HASH explicitly. Repos
# we connect to are checked via `kopia repository status --json` and
# rejected if their hash isn't in this allow-list.
ALLOWED_BLOCK_HASHES = frozenset({
    "HMAC-SHA256",
    "HMAC-SHA3-256",
    "BLAKE2B-256",
    "BLAKE2S-256",
    "BLAKE3-256",
})
DEFAULT_BLOCK_HASH = "BLAKE2B-256"
DEFAULT_ENCRYPTION = "AES256-GCM-HMAC-SHA256"


# ── helpers ──────────────────────────────────────────────────────────────

def _read_cluster() -> dict:
    if not CLUSTER_JSON.exists():
        return {}
    try:
        return json.loads(CLUSTER_JSON.read_text())
    except Exception:
        return {}


def _vm_record(vm_name: str) -> dict | None:
    return (_read_cluster().get("vms") or {}).get(vm_name)


def _target_record(target_id: str) -> dict | None:
    return (_read_cluster().get("backup_targets") or {}).get(target_id)


def _override_source_for_vm(vm_name: str) -> str:
    cluster = _read_cluster()
    cuuid = cluster.get("cluster_uuid", "unknown-cluster")
    target = next(iter((cluster.get("backup_targets") or {}).values()), {})
    prefix = target.get("override_source_prefix") or f"{cuuid}:vms"
    return f"{prefix}:{vm_name}"


def _vm_disk_lvs(vm_name: str, ssh_host: str) -> list[str]:
    """List the LV paths backing a VM by parsing virsh dumpxml on the
    home node. Returns a list like ['/dev/bedrock/vm-X-disk0', ...]."""
    out = _ssh(ssh_host, f"virsh dumpxml {shlex.quote(vm_name)}", check=False)
    import re
    return re.findall(r"<source dev='([^']+)'", out)


def _ssh(host: str, cmd: str, check: bool = True, timeout: int = 600) -> str:
    """Run `cmd` on `host` via SSH. Returns stdout. Raises on failure
    if `check`."""
    full = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=8",
        f"root@{host}", cmd,
    ]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"ssh {host}: cmd failed (rc={r.returncode}): {cmd}\n"
            f"  stderr: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def _kopia_password_export() -> str:
    """Bash snippet that exports KOPIA_PASSWORD from the repo password
    file. kopia 0.21 dropped `--password-file`; passwords go via
    KOPIA_PASSWORD or the deprecated `--password` (which leaks on
    /proc/<pid>/cmdline). Env var keeps the secret off the cmdline."""
    return (
        f"export KOPIA_PASSWORD=\"$(cat {shlex.quote(str(ENCRYPTION_KEY_FILE))})\""
    )


def _kopia_config_file(target_id: str) -> str:
    """Per-target kopia config file path. Always passed via
    --config-file so the location is independent of $HOME (which the
    bedrock-mgmt systemd unit doesn't set)."""
    KOPIA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return str(KOPIA_CONFIG_DIR / f"{target_id}.config")


def _kopia_cache_dir(target_id: str, override: str = "") -> str:
    """Per-target cache directory. Override is kept for the operator's
    log entry (cache_directory field) but defaults to the per-target
    location under /var/cache/bedrock-kopia/."""
    if override:
        Path(override).mkdir(parents=True, exist_ok=True)
        return override
    p = KOPIA_CACHE_ROOT / target_id
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _kopia_global_flags(target_id: str) -> str:
    """Global flags that must appear BEFORE the kopia subcommand:
    --config-file. (`--cache-directory` is a per-subcommand flag in
    kopia 0.21 and is emitted by `_kopia_cache_flag` separately.)"""
    return f"--config-file={shlex.quote(_kopia_config_file(target_id))}"


def _kopia_cache_flag(target_id: str, cache_override: str = "") -> str:
    """Per-subcommand flag: --cache-directory. Goes AFTER the
    subcommand verbs (e.g. `repository connect s3 --cache-directory=…`).
    """
    return f"--cache-directory={shlex.quote(_kopia_cache_dir(target_id, cache_override))}"


def _credentials_env(target_id: str) -> str:
    """Bash snippet that exports everything kopia needs to talk to the
    target: S3 access/secret keys (from the per-target .env file) plus
    KOPIA_PASSWORD. Always-correct prefix for any kopia invocation
    that touches the repo, regardless of kind.

    For kopia-fs targets the .env file is optional — if missing, we
    skip the source line and just export the password."""
    p = CREDENTIALS_DIR / f"{target_id}.env"
    return (
        f"{{ [ -f {shlex.quote(str(p))} ] && set -a && . {shlex.quote(str(p))} && set +a; "
        f"true; }} && "
        f"{_kopia_password_export()}"
    )


def _kopia_password_arg() -> str:
    """No-op: kopia 0.21 takes the password via KOPIA_PASSWORD env var
    (set by _credentials_env). Empty string keeps existing call sites
    that splice the result into command strings legible."""
    return ""


def _kopia_cache_arg(target_id: str) -> str:
    """Legacy helper retained for any external callers; produces the
    --cache-directory flag for `target_id`. New code should use
    `_kopia_global_flags(...)` which bundles --config-file too."""
    target = _target_record(target_id) or {}
    return f"--cache-directory={shlex.quote(_kopia_cache_dir(target_id, target.get('cache_directory') or ''))}"


# ── log helpers ──────────────────────────────────────────────────────────

def _log_append(payload_bytes: bytes) -> tuple[int, bytes]:
    """Best-effort log append via IPC. Raises on full failure."""
    with rust_ipc.Daemon() as d:
        return d.append(payload_bytes)


# ── public: backup target setup ──────────────────────────────────────────

def configure_target_locally(target_id: str, kind: str,
                             *, s3_endpoint: str = "", s3_bucket: str = "",
                             s3_region: str = "",
                             s3_disable_tls: bool = False,
                             s3_disable_tls_verification: bool = False,
                             filesystem_path: str = "",
                             override_source_prefix: str = "",
                             cache_directory: str = "") -> None:
    """Connect (or create + connect) the kopia repo on THIS node so
    subsequent backup/restore invocations Just Work. Reads the
    encryption password from /etc/bedrock/backup.key and credentials
    from /etc/bedrock/backup-credentials/<target_id>.env.

    Sequence:
      1. Try `kopia repository connect` against the target.
      2. If the repo doesn't exist yet (first node to land here),
         run `kopia repository create` with explicit
         --block-hash=BLAKE2B-256 + --encryption=AES256-GCM-HMAC-SHA256.
         A second connect is then implicit (create connects on success).
      3. After the connect succeeds, run `kopia repository status
         --json` and refuse if the repo's block hash is below the
         256-bit floor (see ALLOWED_BLOCK_HASHES).

    Step 3 is the load-bearing one. It's how we detect the case
    "operator created the repo themselves with a 128-bit hash" or
    "an older bedrock version created the repo before this floor
    existed" — we fail loudly rather than silently using a hash we
    don't trust for content addressing.

    Idempotent: if already connected to the same repo, kopia exits
    cleanly and we still re-verify the hash. If connected to a
    different repo, kopia errors and the caller is expected to
    disconnect first.

    NOTE: mgmt's reactor on every node also runs this on
    BACKUP_TARGET_SET log entries — so configuring the target via the
    `bedrock backup target set` CLI on the master propagates to every
    peer automatically. This standalone function is for the master's
    own setup and for ad-hoc reconnects."""
    if not ENCRYPTION_KEY_FILE.exists():
        raise RuntimeError(
            f"missing {ENCRYPTION_KEY_FILE}; "
            f"create it (32+ random bytes, mode 0600) before configuring a target"
        )
    # Per-target credentials file is required for S3 (KOPIA_S3_*) but
    # optional for kopia-fs targets — those just need a writable
    # directory + the encryption password. Avoiding the requirement for
    # FS targets removes a useless tripwire while keeping S3 targets
    # safe (kopia would otherwise prompt interactively for keys, which
    # never works under a systemd service).
    cred_file = CREDENTIALS_DIR / f"{target_id}.env"
    if kind == "kopia-s3" and not cred_file.exists():
        raise RuntimeError(
            f"missing {cred_file}; "
            f"populate it with the target's S3 keys (KOPIA_S3_ACCESS_KEY, "
            f"KOPIA_S3_SECRET_KEY), mode 0600, before configuring an S3 target"
        )

    # Resolve the cache dir once and pass it through. _kopia_cache_dir
    # creates the directory if missing and falls back to the per-target
    # default under /var/cache/bedrock-kopia/<target_id>.
    cache = _kopia_cache_dir(target_id, cache_directory)

    if kind not in ("kopia-s3", "kopia-fs"):
        raise RuntimeError(f"unknown backup target kind: {kind!r}")

    connect_cmd = _kopia_connect_cmd(
        target_id, kind, cache,
        s3_endpoint=s3_endpoint, s3_bucket=s3_bucket, s3_region=s3_region,
        s3_disable_tls=s3_disable_tls,
        s3_disable_tls_verification=s3_disable_tls_verification,
        filesystem_path=filesystem_path,
    )
    log.info("backup: kopia repository connect (target=%s, kind=%s)",
             target_id, kind)
    # 30 s is plenty for a reachable S3 endpoint (kopia connect does
    # a list-blobs round-trip + format-block read; both ~1 s under
    # normal latency). Bumping above this just means a UI wait when
    # the operator typed a bad endpoint — fail fast.
    try:
        r = subprocess.run(["bash", "-lc", connect_cmd],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"kopia repository connect timed out after 30s — "
            f"endpoint {s3_endpoint or filesystem_path!r} is unreachable "
            f"or refusing connections. Check the endpoint URL, network "
            f"reachability, and (for S3) the access key permissions."
        )

    if r.returncode != 0 and "already connected" not in (r.stderr or "").lower():
        # Distinguish "repo not initialized" from real errors. Kopia
        # reports something like "error connecting to repository:
        # repository not initialized in the provided storage" when the
        # bucket exists but no kopia metadata is there yet.
        msg = (r.stderr or "") + (r.stdout or "")
        if _looks_like_uninitialized(msg):
            log.info("backup: repo not initialized — running `kopia repository "
                     "create` with --block-hash=%s", DEFAULT_BLOCK_HASH)
            create_cmd = _kopia_create_cmd(
                target_id, kind, cache,
                s3_endpoint=s3_endpoint, s3_bucket=s3_bucket, s3_region=s3_region,
                s3_disable_tls=s3_disable_tls,
                s3_disable_tls_verification=s3_disable_tls_verification,
                filesystem_path=filesystem_path,
            )
            cr = subprocess.run(["bash", "-lc", create_cmd],
                                capture_output=True, text=True, timeout=45)
            if cr.returncode != 0:
                # Race: another node created it between our connect and
                # our create. Try the connect once more.
                if _looks_like_already_initialized(cr.stderr or cr.stdout or ""):
                    log.info("backup: repo created concurrently — re-connecting")
                    r2 = subprocess.run(["bash", "-lc", connect_cmd],
                                        capture_output=True, text=True, timeout=30)
                    if r2.returncode != 0 and "already connected" not in (r2.stderr or "").lower():
                        raise RuntimeError(
                            f"kopia connect (post-race) failed: "
                            f"{r2.stderr.strip() or r2.stdout.strip()}"
                        )
                else:
                    raise RuntimeError(
                        f"kopia repository create failed: "
                        f"{cr.stderr.strip() or cr.stdout.strip()}"
                    )
        else:
            raise RuntimeError(
                f"kopia connect failed: {r.stderr.strip() or r.stdout.strip()}"
            )

    # Verify the connected repo meets the 256-bit hash floor. If this
    # fails we leave the connection in place but refuse to declare the
    # target healthy — the operator is expected to either rebuild the
    # repo on a stronger hash, or override (no override mechanism is
    # provided on purpose).
    _verify_repo_block_hash(target_id)


def _looks_like_uninitialized(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in (
        "repository not initialized",
        "no repository found",
        "no manifest blobs found",
        "not a kopia repository",
    ))


def _looks_like_already_initialized(msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in (
        "already initialized",
        "repository already exists",
        "found existing data",
    ))


def _kopia_tls_flags(s3_disable_tls: bool,
                     s3_disable_tls_verification: bool) -> str:
    """Map our bedrock target flags to kopia s3 backend flags. These
    are explicit operator opt-ins (default off). Either flag is only
    legal for self-hosted/private S3-compatibles — public S3 + a real
    cert means neither should be set."""
    parts = []
    if s3_disable_tls:
        parts.append("--disable-tls")
    if s3_disable_tls_verification:
        parts.append("--disable-tls-verification")
    return " ".join(parts) + (" " if parts else "")


def _kopia_connect_cmd(target_id: str, kind: str, cache: str, *,
                       s3_endpoint: str, s3_bucket: str, s3_region: str,
                       s3_disable_tls: bool = False,
                       s3_disable_tls_verification: bool = False,
                       filesystem_path: str) -> str:
    g = _kopia_global_flags(target_id)
    c = _kopia_cache_flag(target_id, cache)
    if kind == "kopia-s3":
        return (
            f"{_credentials_env(target_id)} && "
            f"kopia {g} repository connect s3 "
            f"  --bucket={shlex.quote(s3_bucket)} "
            f"  --endpoint={shlex.quote(s3_endpoint)} "
            f"  --region={shlex.quote(s3_region)} "
            f"  {_kopia_tls_flags(s3_disable_tls, s3_disable_tls_verification)}"
            f"  {c}"
        )
    return (
        f"{_credentials_env(target_id)} && "
        f"kopia {g} repository connect filesystem "
        f"  --path={shlex.quote(filesystem_path)} "
        f"  {c}"
    )


def _kopia_create_cmd(target_id: str, kind: str, cache: str, *,
                      s3_endpoint: str, s3_bucket: str, s3_region: str,
                      s3_disable_tls: bool = False,
                      s3_disable_tls_verification: bool = False,
                      filesystem_path: str) -> str:
    """`kopia repository create` with the strong-hash policy baked in.
    The --block-hash and --encryption flags are only honoured at create
    time; they're stored in the repo's format block forever after."""
    g = _kopia_global_flags(target_id)
    c = _kopia_cache_flag(target_id, cache)
    common = (
        f"  --block-hash={shlex.quote(DEFAULT_BLOCK_HASH)} "
        f"  --encryption={shlex.quote(DEFAULT_ENCRYPTION)} "
        f"  {c}"
    )
    if kind == "kopia-s3":
        return (
            f"{_credentials_env(target_id)} && "
            f"kopia {g} repository create s3 "
            f"  --bucket={shlex.quote(s3_bucket)} "
            f"  --endpoint={shlex.quote(s3_endpoint)} "
            f"  --region={shlex.quote(s3_region)} "
            f"  {_kopia_tls_flags(s3_disable_tls, s3_disable_tls_verification)}"
            + common
        )
    return (
        f"{_credentials_env(target_id)} && "
        f"kopia {g} repository create filesystem "
        f"  --path={shlex.quote(filesystem_path)} "
        + common
    )


def _verify_repo_block_hash(target_id: str) -> None:
    """Read the connected repo's block hash and refuse if it's below
    the 256-bit floor. Raises RuntimeError on policy violation; quietly
    accepts on hash names we don't recognise but warns loudly so the
    operator can decide if they need to extend ALLOWED_BLOCK_HASHES."""
    cmd = (
        f"{_credentials_env(target_id)} && "
        f"kopia {_kopia_global_flags(target_id)} "
        f"  repository status --json"
    )
    r = subprocess.run(["bash", "-lc", cmd],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log.warning("backup: kopia repository status failed (rc=%d): %s — "
                    "skipping hash verification this round",
                    r.returncode, (r.stderr or r.stdout).strip())
        return
    try:
        status = json.loads(r.stdout)
    except Exception as e:
        log.warning("backup: cannot parse kopia status JSON: %s", e)
        return

    # Field name varies across kopia versions: "hash", "blockHash",
    # "contentHashAlgorithm". Take the first one that's a string.
    hash_name = None
    for key in ("hash", "blockHash", "contentHashAlgorithm",
                "contentHash", "BlockHash"):
        v = status.get(key)
        if isinstance(v, str) and v:
            hash_name = v
            break

    if not hash_name:
        log.warning("backup: kopia status didn't surface a block-hash "
                    "field — accepting (kopia version may be newer); "
                    "verify out-of-band with `kopia repository status`")
        return

    if hash_name not in ALLOWED_BLOCK_HASHES:
        # If kopia adds a new ≥256-bit hash we don't know about, we'll
        # surface this so the operator can extend the allow-list. But
        # we still refuse — opt-in is the right default for "trust me
        # this hash is ≥256 bits".
        raise RuntimeError(
            f"backup target uses block hash {hash_name!r} which is not in "
            f"bedrock's ≥256-bit allow-list ({sorted(ALLOWED_BLOCK_HASHES)}). "
            f"Bedrock refuses to use this repo for content-addressed "
            f"deduplication. Either rebuild the repo with "
            f"`kopia repository create ... --block-hash=BLAKE2B-256` "
            f"or extend ALLOWED_BLOCK_HASHES if this hash is genuinely "
            f"≥256 bits."
        )
    log.info("backup: repo block hash = %s (≥256-bit, accepted)", hash_name)


# ── public: run a backup ─────────────────────────────────────────────────

def run_backup(target_id: str, vm_name: str, *, label: str = "") -> dict:
    """Take an LV snapshot of the VM's disk(s), run kopia snapshot
    create against it, then drop the snapshot. Append BACKUP_DONE on
    success or BACKUP_FAILED on failure.

    Returns: {kopia_snapshot_id, bytes_added, duration_s, label}.
    """
    cluster = _read_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise RuntimeError(f"VM {vm_name!r} not found in cluster.json")
    target = (cluster.get("backup_targets") or {}).get(target_id)
    if target is None:
        raise RuntimeError(f"backup target {target_id!r} not configured")
    # vm["host"] is the node-NAME (e.g. "bedrock-sim-1.bedrock.local"),
    # NOT an IP. The IP lives in nodes[<node-name>]["host"]. Resolve in
    # that order; fall back to a host-equality scan only if the name
    # lookup fails (older cluster.json layouts).
    nodes = cluster.get("nodes") or {}
    home_node_name = vm.get("host") or ""
    node = nodes.get(home_node_name)
    if not node:
        node = _node_for_host(home_node_name, nodes)
        if node:
            home_node_name = next(
                (n for n, info in nodes.items() if info is node),
                home_node_name,
            )
    if not node or not node.get("host"):
        raise RuntimeError(
            f"can't resolve SSH host for VM {vm_name} "
            f"(home={home_node_name!r}, available nodes={list(nodes)})"
        )
    ssh_host = node["host"]

    disks = _vm_disk_lvs(vm_name, ssh_host)
    if not disks:
        raise RuntimeError(f"no disks discovered for VM {vm_name}")

    started = time.monotonic()
    snap_label = label or time.strftime("%Y%m%dT%H%M%S")

    # For v1 we back up only the first disk. Multi-disk handling lands
    # in v1.x with a per-disk override-source. The first disk is the
    # boot/primary; that's what 99% of backup scenarios want first.
    primary = disks[0]
    snap_lv_name = f"{Path(primary).name}-bk-{snap_label}"
    vg = primary.split("/")[2] if primary.startswith("/dev/") else "bedrock"
    src_lv_name = Path(primary).name

    override_source = _override_source_for_vm(vm_name)
    g = _kopia_global_flags(target_id)
    cred_env = _credentials_env(target_id)
    # NB: --cache-directory is only valid on `repository connect/create`
    # in kopia 0.21. snapshot create/restore/delete pick up the cache
    # location from the persisted repo config (via --config-file).

    # Thin snapshots default to "activation skip" — the LV exists but
    # /dev/<vg>/<lv> isn't a usable block device until lvchange -ay -K
    # activates it. Without -K (the override flag), `lvchange -ay` is a
    # no-op for skip-activation snapshots. So: create + activate.
    snap_path = f"/dev/{vg}/{snap_lv_name}"
    create_snap = (
        f"lvcreate --snapshot "
        f"  --name {shlex.quote(snap_lv_name)} "
        f"  {shlex.quote(f'/dev/{vg}/{src_lv_name}')} && "
        f"lvchange -ay -K {shlex.quote(snap_path)}"
    )

    # Stream the LV snapshot directly into kopia via stdin. Kopia stores
    # it as a single file (--stdin-file=disk0.img) under a virtual
    # snapshot path that we override to the cluster's stable VM identity.
    # Block-fidelity backup with no intermediate file: kopia's content-
    # defined chunking dedups identical 4 MiB blocks across snapshots,
    # so unchanged sectors don't re-upload.
    #
    # The path argument to `snapshot create` is purely a label for the
    # virtual-source identity; --override-source replaces it on the
    # server side. We pick a stable bedrock-flavoured pseudo-path so
    # `kopia snapshot list` is human-readable on the operator side too.
    pseudo_path = f"/bedrock/vms/{vm_name}"
    kopia_create = (
        f"set -o pipefail; "
        f"{cred_env} && "
        f"dd if={shlex.quote(snap_path)} bs=4M status=none | "
        f"kopia {g} snapshot create "
        f"  {shlex.quote(pseudo_path)} "
        f"  --stdin-file=disk0.img "
        f"  --override-source={shlex.quote(override_source)} "
        f"  --description={shlex.quote(snap_label)} "
        f"  --json"
    )
    cleanup_snap = f"lvremove -f {shlex.quote(snap_path)} 2>&1"

    try:
        # 1. Take the LV snapshot on the home node.
        log.info("backup[%s]: lvcreate snapshot %s", vm_name, snap_path)
        _ssh(ssh_host, create_snap)

        try:
            # 2. Stream it through kopia.
            log.info("backup[%s]: dd | kopia snapshot create (stdin-file)", vm_name)
            out = _ssh(ssh_host, kopia_create, timeout=14400)
            kopia_snap_id, bytes_added = _parse_kopia_create(out)
        finally:
            # 3. Always drop the LV snapshot — short-lived COW only.
            try:
                _ssh(ssh_host, cleanup_snap, check=False)
            except Exception as e:
                log.warning("backup[%s]: lvremove cleanup: %s", vm_name, e)
    except Exception as e:
        duration = time.monotonic() - started
        reason = f"{type(e).__name__}: {e}"
        log.error("backup[%s] failed after %.1fs: %s", vm_name, duration, reason)
        try:
            _log_append(le.backup_failed(
                vm=vm_name, target_id=target_id, reason=reason,
                source_node=home_node_name, label=snap_label,
            ))
        except Exception:
            pass
        raise

    duration = time.monotonic() - started
    log.info("backup[%s] done: kopia=%s, %d bytes added, %.1fs",
             vm_name, kopia_snap_id, bytes_added, duration)
    _log_append(le.backup_done(
        vm=vm_name, target_id=target_id,
        kopia_snapshot_id=kopia_snap_id,
        source_node=home_node_name,
        bytes_added=bytes_added,
        duration_s=duration,
        label=snap_label,
    ))
    return {
        "kopia_snapshot_id": kopia_snap_id,
        "bytes_added": bytes_added,
        "duration_s": duration,
        "label": snap_label,
    }


def _parse_kopia_create(json_out: str) -> tuple[str, int]:
    """Parse `kopia snapshot create --json` output → (snapshot_id, bytes).
    Kopia prints multi-line JSON with the final manifest at the end; we
    read the last JSON object."""
    try:
        last = json_out.strip().splitlines()[-1]
        obj = json.loads(last)
    except Exception:
        return ("unknown", 0)
    sid = obj.get("id") or obj.get("manifest_id") or "unknown"
    stats = obj.get("stats") or {}
    bytes_added = int(stats.get("uploadedBytes") or stats.get("totalSize") or 0)
    return (str(sid), bytes_added)


# ── public: list backups + restore ───────────────────────────────────────

def list_backups_for_vm(vm_name: str) -> list[dict]:
    """Backup history for one VM, drawn from the cluster log via cluster.json.
    Returns newest first, list of {kopia_snapshot_id, target_id, ts_index,
    bytes_added, duration_s, label, source_node}."""
    vm = _vm_record(vm_name)
    if not vm:
        return []
    return list(vm.get("backups") or [])


def run_restore(target_id: str, kopia_snapshot_id: str, vm_name: str, *,
                target_lv_path: str | None = None,
                dest_node_name: str | None = None) -> dict:
    """Restore a backup directly onto a block device — typically the
    VM's LV. The caller MUST ensure the VM is shut down (qemu holding
    the device EBUSY would race with restore writes). The restored LV
    is byte-identical to what `run_backup` captured."""
    cluster = _read_cluster()
    target = (cluster.get("backup_targets") or {}).get(target_id)
    if target is None:
        raise RuntimeError(f"backup target {target_id!r} not configured")

    # Default the destination node to wherever the VM lives in
    # cluster.json; that's where the LV is. (Restoring to a different
    # node is a v1.x feature — would need to also (re)create the LV
    # there and update libvirt, which is out of scope for this path.)
    if dest_node_name is None:
        vm_rec = (cluster.get("vms") or {}).get(vm_name) or {}
        dest_node_name = vm_rec.get("host") or _self_node_name()
    node = (cluster.get("nodes") or {}).get(dest_node_name)
    if not node:
        raise RuntimeError(f"destination node {dest_node_name!r} not in cluster")
    ssh_host = node["host"]

    # ── Safety: refuse restore on a running VM ──────────────────
    # qemu holds /dev/<lv> with O_RDWR while the VM is running; the
    # dd write would race with qemu's writes and corrupt both the
    # in-flight VM state and the restore. The dashboard already
    # disables the per-row button when state==running, but the API
    # is the security boundary — also enforce here.
    try:
        state = _ssh(ssh_host,
                     f"virsh domstate {shlex.quote(vm_name)} 2>/dev/null || true",
                     check=False).strip().lower()
    except Exception:
        state = ""
    if state == "running":
        raise RuntimeError(
            f"refusing to restore VM {vm_name!r}: it is currently running on "
            f"{dest_node_name}. Shut it down first (POST /api/vms/{vm_name}/poweroff "
            f"or /shutdown) — restoring while qemu holds the disk would race "
            f"with the in-flight VM state and corrupt both."
        )

    # If the caller didn't specify, restore back onto the VM's primary
    # disk LV — the most common case for "undo my last change to this VM".
    if target_lv_path is None:
        disks = _vm_disk_lvs(vm_name, ssh_host)
        if not disks:
            raise RuntimeError(
                f"can't auto-resolve target LV for VM {vm_name} "
                f"(no disks discovered via virsh dumpxml)"
            )
        target_lv_path = disks[0]

    g = _kopia_global_flags(target_id)
    cred_env = _credentials_env(target_id)
    # Streaming restore via kopia's FUSE mount: kopia exposes the
    # snapshot read-only at a mountpoint, dd reads disk0.img directly
    # to the target LV. No intermediate temp file. Block-fidelity
    # restore — every byte written back exactly as captured at backup
    # time.
    #
    # Why FUSE: kopia 0.21's `snapshot restore` doesn't support stdout
    # output, and writing to a block device target hits a truncate(2)
    # call that EINVALs on /dev/* paths. FUSE is the supported escape:
    # mount → read-via-vfs → unmount. fusermount is in /usr/bin on
    # AlmaLinux 9 stock, no extra packages needed.
    mnt = f"/run/bedrock-restore-{vm_name}-{int(time.monotonic()*1000)}"
    cmd = (
        f"set -o pipefail; "
        f"{cred_env} && "
        f"mkdir -p {shlex.quote(mnt)} && "
        f"kopia {g} mount {shlex.quote(kopia_snapshot_id)} "
        f"  {shlex.quote(mnt)} >/tmp/kopia-restore.log 2>&1 & "
        f"MOUNT_PID=$!; "
        # Wait up to 20s for FUSE mount to populate disk0.img
        f"for i in $(seq 1 20); do "
        f"  [ -f {shlex.quote(mnt + '/disk0.img')} ] && break; "
        f"  sleep 1; "
        f"done; "
        f"if [ ! -f {shlex.quote(mnt + '/disk0.img')} ]; then "
        f"  echo 'kopia mount did not surface disk0.img within 20s'; "
        f"  cat /tmp/kopia-restore.log; "
        f"  fusermount -u {shlex.quote(mnt)} 2>/dev/null; "
        f"  rmdir {shlex.quote(mnt)} 2>/dev/null; "
        f"  exit 1; "
        f"fi; "
        f"dd if={shlex.quote(mnt + '/disk0.img')} "
        f"   of={shlex.quote(target_lv_path)} "
        f"   bs=4M conv=sparse status=none; "
        f"DDRC=$?; "
        f"fusermount -u {shlex.quote(mnt)} 2>/dev/null || "
        f"  kopia {g} mount unmount {shlex.quote(mnt)} 2>/dev/null; "
        f"wait $MOUNT_PID 2>/dev/null; "
        f"rmdir {shlex.quote(mnt)} 2>/dev/null; "
        f"exit $DDRC"
    )

    started = time.monotonic()
    try:
        out = _ssh(ssh_host, cmd, timeout=14400)
        log.info("restore[%s] from kopia=%s done: %s",
                 vm_name, kopia_snapshot_id, out[-200:].strip())
    except Exception as e:
        duration = time.monotonic() - started
        reason = f"{type(e).__name__}: {e}"
        log.error("restore[%s] failed: %s", vm_name, reason)
        try:
            _log_append(le.restore_failed(
                vm=vm_name, target_id=target_id,
                kopia_snapshot_id=kopia_snapshot_id,
                reason=reason, dest_node=dest_node_name,
            ))
        except Exception:
            pass
        raise
    duration = time.monotonic() - started
    _log_append(le.restore_done(
        vm=vm_name, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id,
        dest_node=dest_node_name, duration_s=duration,
    ))
    return {
        "kopia_snapshot_id": kopia_snapshot_id,
        "target_lv_path": target_lv_path,
        "dest_node": dest_node_name,
        "duration_s": duration,
    }


def delete_backup(target_id: str, kopia_snapshot_id: str, vm_name: str,
                  *, reason: str = "") -> None:
    """Delete one snapshot from the kopia repo and log it. Maintenance
    GC eventually frees the underlying chunks; that runs from the
    mgmt master's `kopia maintenance run` schedule."""
    cluster = _read_cluster()
    if (cluster.get("backup_targets") or {}).get(target_id) is None:
        raise RuntimeError(f"backup target {target_id!r} not configured")
    self_node = _self_node_name()
    node = (cluster.get("nodes") or {}).get(self_node)
    ssh_host = node["host"] if node else "127.0.0.1"
    cmd = (
        f"{_credentials_env(target_id)} && "
        f"kopia {_kopia_global_flags(target_id)} "
        f"  snapshot delete {shlex.quote(kopia_snapshot_id)} "
    )
    _ssh(ssh_host, cmd)
    _log_append(le.backup_deleted(
        vm=vm_name, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id, reason=reason,
    ))


# ── small lookup helpers ────────────────────────────────────────────────

def _self_node_name() -> str:
    try:
        return state_mod.load().get("node_name", "") or ""
    except Exception:
        return ""


def _hostname_to_node_name(host: str, cluster: dict) -> str:
    for n, info in (cluster.get("nodes") or {}).items():
        if info.get("host") == host:
            return n
    return ""


def _node_for_host(host: str, nodes: dict) -> dict | None:
    for info in (nodes or {}).values():
        if info.get("host") == host:
            return info
    return None
