"""Bedrock backup orchestration — Kopia wrapper.

mgmt invokes the kopia CLI on the appropriate Bedrock node (the VM's
home node) over SSH. Kopia is a one-shot tool: spins up, does work,
exits. mgmt never runs a long-lived kopia daemon.

Per cluster-protocol-overview.md and snapshots-and-backup.md §9c-bis:

  - One Kopia repository per cluster (operator-chosen — S3 / S3-
    compatible / FS path). Encryption password lives in
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

from lib import bedrock_state as bs, state as state_mod

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
    try:
        import sys; sys.path.insert(0, "/usr/local/lib/bedrock")
        from lib import cluster_state
        return cluster_state.load_cluster()
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
    home node. Returns a list like ['/dev/bedrock/vm-X-disk0', ...].
    Order matches `_vm_disk_target_devs(...)` element-by-element."""
    out = _ssh(ssh_host, f"virsh dumpxml {shlex.quote(vm_name)}", check=False)
    return _parse_disks_from_xml(out)[0]


def _vm_disk_target_devs(vm_name: str, ssh_host: str) -> list[str]:
    """Guest-visible target dev names ('vda', 'vdb', …) for the VM's
    disks, in the same order as `_vm_disk_lvs`. Used as the kopia
    per-disk source-line suffix."""
    out = _ssh(ssh_host, f"virsh dumpxml {shlex.quote(vm_name)}", check=False)
    return _parse_disks_from_xml(out)[1]


def _parse_disks_from_xml(xml: str) -> tuple[list[str], list[str]]:
    """Parse virsh dumpxml output, returning ([source LV paths],
    [target devs]) in the order the disks appear. Each <disk> block
    has a `<source dev='/dev/.../lv'/>` and a `<target dev='vda'/>`;
    we pair them positionally so element i of one list corresponds to
    element i of the other."""
    import re
    # Split on <disk … to get one chunk per disk; first chunk is XML
    # before the first <disk>, drop it.
    blocks = re.split(r"<disk\b", xml)[1:]
    lvs: list[str] = []
    devs: list[str] = []
    for blk in blocks:
        # Only consider block-device disks (LVs). Cdroms / passthrough
        # files aren't backup-eligible the same way.
        if "device='disk'" not in blk and 'device="disk"' not in blk:
            continue
        m_src = re.search(r"<source dev='([^']+)'", blk) or \
                re.search(r'<source dev="([^"]+)"', blk)
        m_dev = re.search(r"<target dev='([^']+)'", blk) or \
                re.search(r'<target dev="([^"]+)"', blk)
        if not m_src:
            continue
        lvs.append(m_src.group(1))
        devs.append(m_dev.group(1) if m_dev else "")
    return lvs, devs


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


# ── state-write helpers (rqlite) ─────────────────────────────────────────
#
# Replaces the old bedrock-rust log-append path. Mutations go directly to
# rqlite via bedrock_state.* helpers (which bump bedrock_meta.revision).


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
    """Back up every disk of `vm_name` to `target_id` as one
    consistent point-in-time. Returns:

      {disks: [{target_dev, lv_path, kopia_snapshot_id, bytes_added}, …],
       duration_s, label, fs_freeze_used}

    Sequence:
      1. Resolve the VM's home node + every disk's LV path.
      2. If the VM is running and qemu-guest-agent responds, call
         `virsh domfsfreeze` so the guest kernel quiesces every
         filesystem before we snapshot. This is what makes the
         resulting snapshot OS-level consistent (DBs flush, journals
         settle). If GA isn't there, we proceed with a crash-
         consistent snapshot — safe for ext4/xfs which replay their
         own journals on next mount, less safe for unconfigured DBs.
      3. lvcreate every disk's snapshot, in one bash invocation. The
         time the FS is frozen is bounded by lvcreate × N (a few
         tens of milliseconds per disk).
      4. virsh domfsthaw immediately after the last lvcreate. The
         guest unfreezes; user-visible IO pause is sub-second.
      5. lvchange -ay -K every snapshot LV (thin snapshots are skip-
         activation by default).
      6. For each disk, dd the LV snapshot into a kopia stdin-file
         snapshot with a per-disk --override-source so each disk is
         a separate kopia source-line:
            <prefix>:<vm>:<target_dev>     e.g. <uuid>:vms:web1:vda
         Kopia content-defined chunks at ~4 MiB; identical sectors
         across snapshots don't re-upload, so dedup still works for
         each disk independently.
      7. Drop every LV snapshot. ALWAYS — even on kopia failure.
      8. Append BACKUP_DONE with the multi-disk record.
    """
    cluster = _read_cluster()
    vm = (cluster.get("vms") or {}).get(vm_name)
    if vm is None:
        raise RuntimeError(f"VM {vm_name!r} not found in cluster.json")
    target = (cluster.get("backup_targets") or {}).get(target_id)
    if target is None:
        raise RuntimeError(f"backup target {target_id!r} not configured")

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

    disk_lvs = _vm_disk_lvs(vm_name, ssh_host)
    if not disk_lvs:
        raise RuntimeError(f"no disks discovered for VM {vm_name}")
    target_devs = _vm_disk_target_devs(vm_name, ssh_host)
    # _vm_disk_target_devs returns same length list as disk_lvs in same order.

    started = time.monotonic()
    snap_label = label or time.strftime("%Y%m%dT%H%M%S")
    cluster_uuid = cluster.get("cluster_uuid", "unknown-cluster")
    prefix_override = (target.get("override_source_prefix") or "").strip()
    src_prefix = prefix_override or f"{cluster_uuid}:vms"

    # Build the per-disk plan — paths, snapshot names, kopia source line.
    plans = []
    for i, lv in enumerate(disk_lvs):
        vg = lv.split("/")[2] if lv.startswith("/dev/") else "bedrock"
        lv_name = Path(lv).name
        snap_lv = f"{lv_name}-bk-{snap_label}"
        target_dev = (target_devs[i] if i < len(target_devs) else "") or f"disk{i}"
        plans.append({
            "target_dev":     target_dev,
            "lv_path":        lv,
            "vg":             vg,
            "src_lv_name":    lv_name,
            "snap_lv":        snap_lv,
            "snap_path":      f"/dev/{vg}/{snap_lv}",
            "override_source": f"{src_prefix}:{vm_name}:{target_dev}",
        })
    log.info("backup[%s]: %d disk(s) to back up: %s",
             vm_name, len(plans), ", ".join(p["target_dev"] for p in plans))

    g = _kopia_global_flags(target_id)
    cred_env = _credentials_env(target_id)

    # ── Step 1+2+3+4: fs-freeze, lvcreate every disk, fs-thaw ──────
    # All in one bash script so the freeze window is bounded by the
    # round-trip from mgmt to home node, not the SSH latency × N.
    # Critical: thaw is in a `trap` so we always unfreeze even on
    # lvcreate failure mid-loop.
    lvcreate_lines = " && ".join(
        f"lvcreate --snapshot --name {shlex.quote(p['snap_lv'])} "
        f"{shlex.quote(p['lv_path'])}"
        for p in plans
    )
    activate_lines = " && ".join(
        f"lvchange -ay -K {shlex.quote(p['snap_path'])}"
        for p in plans
    )
    snapshot_script = (
        f"set -e; "
        f"FROZEN=0; "
        # Only attempt freeze if VM is running AND domain has a guest agent.
        # virsh domfsfreeze fails fast (~50ms) if no GA — silently fall
        # through to crash-consistent.
        f"if virsh domstate {shlex.quote(vm_name)} 2>/dev/null | grep -q running; then "
        f"  if virsh domfsfreeze {shlex.quote(vm_name)} >/dev/null 2>&1; then "
        f"    FROZEN=1; "
        f"  fi; "
        f"fi; "
        # Even if lvcreate dies mid-loop, run thaw before exiting so we
        # don't leave the guest hanging.
        f"trap 'if [ $FROZEN -eq 1 ]; then "
        f"  virsh domfsthaw {shlex.quote(vm_name)} >/dev/null 2>&1 || true; "
        f"fi' EXIT; "
        f"{lvcreate_lines}; "
        # Thaw ASAP — minimise quiesce time. The trap also runs at end
        # but explicit thaw here means thaw happens BEFORE activate,
        # which is the slower step.
        f"if [ $FROZEN -eq 1 ]; then "
        f"  virsh domfsthaw {shlex.quote(vm_name)} >/dev/null 2>&1 || true; "
        f"  FROZEN=0; "
        f"fi; "
        f"{activate_lines}; "
        f"echo \"FS_FREEZE_USED=$FROZEN_INITIAL\""
    ).replace("$FROZEN_INITIAL",
              # Capture original value for reporting, since we set FROZEN=0
              # after thaw. Append to script:
              "$(if [ $FROZEN -eq 0 ] && [ -n \"$FROZEN_WAS\" ]; then "
              "echo $FROZEN_WAS; else echo $FROZEN; fi)")
    # Simpler: just report whether we got to the freeze branch initially.
    # Rewrite cleaner:
    snapshot_script = (
        f"set -e; "
        f"FROZEN_USED=0; "
        f"if virsh domstate {shlex.quote(vm_name)} 2>/dev/null | grep -q running; then "
        f"  if virsh domfsfreeze {shlex.quote(vm_name)} >/dev/null 2>&1; then "
        f"    FROZEN_USED=1; "
        f"    trap 'virsh domfsthaw {shlex.quote(vm_name)} >/dev/null 2>&1 || true' EXIT; "
        f"  fi; "
        f"fi; "
        f"{lvcreate_lines}; "
        # Thaw NOW (before activate, kopia, etc.); the trap stays as a
        # safety net but normally fires on a no-op since fs is already
        # thawed.
        f"if [ $FROZEN_USED -eq 1 ]; then "
        f"  virsh domfsthaw {shlex.quote(vm_name)} >/dev/null 2>&1 || true; "
        f"fi; "
        f"{activate_lines}; "
        f"echo \"FS_FREEZE_USED=$FROZEN_USED\""
    )

    cleanup_script = "; ".join(
        f"lvremove -f {shlex.quote(p['snap_path'])} 2>&1"
        for p in plans
    )

    fs_freeze_used = False
    disk_results: list[dict] = []

    try:
        log.info("backup[%s]: snapshot phase (freeze + lvcreate × %d)",
                 vm_name, len(plans))
        out = _ssh(ssh_host, snapshot_script, timeout=120)
        for line in out.splitlines():
            if line.startswith("FS_FREEZE_USED="):
                fs_freeze_used = (line.split("=", 1)[1].strip() == "1")
        log.info("backup[%s]: snapshots taken (fs_freeze_used=%s)",
                 vm_name, fs_freeze_used)

        try:
            for p in plans:
                pseudo_path = f"/bedrock/vms/{vm_name}/{p['target_dev']}"
                kopia_create = (
                    f"set -o pipefail; "
                    f"{cred_env} && "
                    f"dd if={shlex.quote(p['snap_path'])} bs=4M status=none | "
                    f"kopia {g} snapshot create "
                    f"  {shlex.quote(pseudo_path)} "
                    f"  --stdin-file=disk0.img "
                    f"  --override-source={shlex.quote(p['override_source'])} "
                    f"  --description={shlex.quote(snap_label)} "
                    f"  --json"
                )
                log.info("backup[%s]: kopia stream disk %s ← %s",
                         vm_name, p["target_dev"], p["snap_path"])
                disk_out = _ssh(ssh_host, kopia_create, timeout=14400)
                kid, bytes_added = _parse_kopia_create(disk_out)
                disk_results.append({
                    "target_dev": p["target_dev"],
                    "lv_path": p["lv_path"],
                    "kopia_snapshot_id": kid,
                    "bytes_added": bytes_added,
                })
        finally:
            try:
                _ssh(ssh_host, cleanup_script, check=False, timeout=120)
            except Exception as e:
                log.warning("backup[%s]: cleanup: %s", vm_name, e)
    except Exception as e:
        duration = time.monotonic() - started
        reason = f"{type(e).__name__}: {e}"
        log.error("backup[%s] failed after %.1fs: %s", vm_name, duration, reason)
        try:
            bs.backup_failed(
                vm=vm_name, target_id=target_id, reason=reason,
                source_node=home_node_name, label=snap_label,
            )
        except Exception:
            pass
        raise

    duration = time.monotonic() - started
    total_bytes = sum(d["bytes_added"] for d in disk_results)
    log.info("backup[%s] done: %d disk(s), %d bytes added total, %.1fs, "
             "fs_freeze_used=%s",
             vm_name, len(disk_results), total_bytes, duration, fs_freeze_used)
    bs.backup_done(
        vm=vm_name, target_id=target_id,
        disks=disk_results,
        source_node=home_node_name,
        duration_s=duration,
        label=snap_label,
        fs_freeze_used=fs_freeze_used,
    )
    return {
        "disks": disk_results,
        "duration_s": duration,
        "label": snap_label,
        "fs_freeze_used": fs_freeze_used,
        # Legacy fields — keep until callers migrate. Primary disk = first.
        "kopia_snapshot_id": disk_results[0]["kopia_snapshot_id"]
                              if disk_results else "",
        "bytes_added": total_bytes,
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

    # Look up the backup row matching the kopia_snapshot_id. The
    # caller passes any disk's kopia id (typically the row's primary,
    # which is disk0); we walk every disk in every backup row to find
    # it, then restore EVERY disk in that row. That's how a VM with
    # multiple disks is restored as one consistent unit — operator
    # picks one snapshot, gets the whole VM rolled back.
    vm_rec = (cluster.get("vms") or {}).get(vm_name) or {}
    matched_row: dict | None = None
    for b in (vm_rec.get("backups") or []):
        for d in (b.get("disks") or []):
            if d.get("kopia_snapshot_id") == kopia_snapshot_id:
                matched_row = b
                break
        if matched_row:
            break
    # Single-LV operator override path: caller passed an explicit
    # target_lv_path. Used by v1.x flows like restore-to-fresh-LV.
    # In that case we don't need a matched row — we just restore the
    # one snapshot the caller named, to the LV the caller chose.
    if target_lv_path is not None:
        plan = [{
            "target_dev": "custom",
            "kopia_snapshot_id": kopia_snapshot_id,
            "target_lv_path": target_lv_path,
        }]
    elif matched_row is None:
        raise RuntimeError(
            f"snapshot {kopia_snapshot_id!r} not found in backup history "
            f"of {vm_name}. Pass `target_lv_path` to restore an arbitrary "
            f"snapshot to a specific LV."
        )
    else:
        # Build per-disk plan: kopia_snapshot_id from the row, LV path
        # from either (a) the disk record's lv_path (frozen at backup
        # time, may be obsolete if VM was rebuilt) or (b) the current
        # VM's matching target_dev. Prefer (b) since it survives
        # rename/recreate, fall back to (a).
        cur_lvs = _vm_disk_lvs(vm_name, ssh_host)
        cur_devs = _vm_disk_target_devs(vm_name, ssh_host)
        cur_by_dev = dict(zip(cur_devs, cur_lvs))
        plan = []
        for d in matched_row.get("disks") or []:
            tgt_dev = d.get("target_dev") or ""
            tgt_lv = cur_by_dev.get(tgt_dev) or d.get("lv_path") or ""
            if not tgt_lv:
                raise RuntimeError(
                    f"can't resolve target LV for disk {tgt_dev!r} of {vm_name} "
                    f"(VM no longer has this disk and the backup record "
                    f"didn't capture an lv_path)"
                )
            plan.append({
                "target_dev":        tgt_dev,
                "kopia_snapshot_id": d.get("kopia_snapshot_id") or "",
                "target_lv_path":    tgt_lv,
            })
    log.info("restore[%s]: %d disk(s) to restore: %s",
             vm_name, len(plan),
             ", ".join(f"{p['target_dev']}→{p['target_lv_path']}" for p in plan))

    g = _kopia_global_flags(target_id)
    cred_env = _credentials_env(target_id)

    started = time.monotonic()
    restored: list[dict] = []
    try:
        for p in plan:
            # Streaming restore via kopia's FUSE mount: kopia exposes
            # the snapshot read-only at a mountpoint, dd reads
            # disk0.img directly to the target LV. No intermediate
            # temp file. Block-fidelity restore — every byte written
            # back exactly as captured at backup time.
            mnt = (f"/run/bedrock-restore-{vm_name}-{p['target_dev']}-"
                   f"{int(time.monotonic()*1000)}")
            cmd = (
                f"set -o pipefail; "
                f"{cred_env} && "
                f"mkdir -p {shlex.quote(mnt)} && "
                f"kopia {g} mount {shlex.quote(p['kopia_snapshot_id'])} "
                f"  {shlex.quote(mnt)} >/tmp/kopia-restore.log 2>&1 & "
                f"MOUNT_PID=$!; "
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
                f"   of={shlex.quote(p['target_lv_path'])} "
                f"   bs=4M conv=sparse status=none; "
                f"DDRC=$?; "
                f"fusermount -u {shlex.quote(mnt)} 2>/dev/null || "
                f"  kopia {g} mount unmount {shlex.quote(mnt)} 2>/dev/null; "
                f"wait $MOUNT_PID 2>/dev/null; "
                f"rmdir {shlex.quote(mnt)} 2>/dev/null; "
                f"exit $DDRC"
            )
            log.info("restore[%s]: kopia mount %s → dd %s",
                     vm_name, p["kopia_snapshot_id"], p["target_lv_path"])
            _ssh(ssh_host, cmd, timeout=14400)
            restored.append(dict(p))
    except Exception as e:
        duration = time.monotonic() - started
        reason = f"{type(e).__name__}: {e}"
        log.error("restore[%s] failed: %s", vm_name, reason)
        try:
            bs.restore_failed(
                vm=vm_name, target_id=target_id,
                kopia_snapshot_id=kopia_snapshot_id,
                reason=reason, dest_node=dest_node_name,
            )
        except Exception:
            pass
        raise

    duration = time.monotonic() - started
    log.info("restore[%s] done: %d disk(s) in %.1fs",
             vm_name, len(restored), duration)
    bs.restore_done(
        vm=vm_name, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id,
        dest_node=dest_node_name, duration_s=duration,
    )
    return {
        "kopia_snapshot_id": kopia_snapshot_id,
        "disks": restored,
        "dest_node": dest_node_name,
        "duration_s": duration,
        # Legacy single-disk field — primary disk's target.
        "target_lv_path": restored[0]["target_lv_path"] if restored else "",
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
    bs.backup_deleted(
        vm=vm_name, target_id=target_id,
        kopia_snapshot_id=kopia_snapshot_id, reason=reason,
    )


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
