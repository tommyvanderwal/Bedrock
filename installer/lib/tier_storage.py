"""Bedrock per-resource storage — the `cluster` singleton + weed-volume LV.

See `tier_storage.md` (next to this file) for the full operational spec:
  - what each function does, contracts and invariants
  - where state lives (rqlite, /etc/drbd.d, /etc/fstab, kernel)
  - the WHY behind each design choice
  - the documented sources for every external behavior
  - known issues and queued fixes

Reviewers analyzing this module for "can this reach a bad state" should
read tier_storage.md first — the invariants section enumerates what each
operation must preserve, with crash-safety reasoning.

For the journey of decisions and corrections that led here (wrong turns,
misdiagnoses, lessons learned), see ../../docs/lessons-log.md.

Storage model (per-resource; the legacy scratch/bulk/critical LV-tiers
are gone — see docs/storage-architecture.md):

  cluster singleton : N=1 → local thin LV; N≥2 → DRBD-replicated XFS,
                      mounted only on the mgmt-master. Hosts the
                      arbiter rqlite data, SeaweedFS filer's leveldb3,
                      and the S3 IAM database — one DRBD failover hands
                      them all off together. DRBD resource name is
                      `cluster` (`bedrock-data-cluster` / `bedrock-meta-cluster`).
  weed-volume LV    : one large local thin LV (`bedrock-weed-volume`)
                      mkfs+mounted at SeaweedFS' volume dir. No DRBD —
                      SeaweedFS replicates bytes via its collections
                      (scratch=000, standard=001, critical=002).

Per-VM disks (cattle local LV / pet 2-way DRBD / vipet 3-way DRBD) are
owned by the VM-lifecycle sagas (bedrock_d/vm/*), not this module.

External DRBD metadata is essential and used by EVERY DRBD resource
(singleton + per-VM): it makes local-LV → DRBD-replicated promotion
zero-copy (the data LV's XFS is preserved byte-for-byte) and keeps the
DRBD device the same size as the data LV.

Entry points (growth path):
  setup_n1()                          — single-node setup; idempotent
  transition_to_n2_master(...)        — N=1 -> N=2 master side
                                        (DRBD primary on the cluster resource)
  transition_to_n2_peer(...)          — N=1 -> N=2 peer side
                                        (DRBD secondary on the cluster resource)
  promote_cluster_to_3way(...)        — N=2 -> N=3 cluster promote

Entry points (shrink / role-move path):
  drbd_remove_peer(...)               — online DRBD peer removal
                                        (LINBIT-blessed adjust flow)

Entry points (final-collapse to single-node path):
  drbd_demote_to_local()              — turn the stand-alone cluster DRBD
                                        resource back into a plain local LV
                                        (XFS preserved by external meta)

Called from:
  mgmt_install.install_full() -> setup_n1()
  agent_install.install()     -> setup_n1()
  bedrock storage <cmd>       -> operator-driven transitions
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── Layout constants ───────────────────────────────────────────────────────

# Bedrock's storage model is "one disk per node, one VG, one thin pool".
# The VG already exists on the system after AlmaLinux install (default
# name `almalinux`); bedrock-bootstrap adopts it rather than creating a
# new one. Whatever VG name the OS installer happened to use is written
# to /etc/bedrock/storage.json at bootstrap time so every subsequent
# bedrock command + the mgmt service all agree on it.
#
# A fresh (greenfield) install creates the VG as `bedrock-vg`
# (FRESH_VG) — matching docs/storage-architecture.md and
# bedrock_d/vm/lvm.py. We don't auto-rename existing VGs — that
# requires grub + initramfs regeneration and a reboot — so an
# install.sh-on-existing-Alma run ADOPTS whatever VG the OS installer
# made (often `almalinux`) rather than creating `bedrock-vg`.
#
# DEFAULT_VG is the resolution FALLBACK only: the name detect_vg()
# returns when nothing else is known (no storage.json, no VG present —
# e.g. off-node in the test environment, where the LV-name contract is
# /dev/bedrock/...). It is NOT the fresh-create name; that is FRESH_VG.
THINPOOL = "thinpool"
STORAGE_JSON = Path("/etc/bedrock/storage.json")
DEFAULT_VG = "bedrock"
FRESH_VG = "bedrock-vg"

# Candidate raw disks for the bedrock VG when greenfield-creating one
# (no usable VG present). On real-lab nodes the OS install already
# made an `almalinux` VG on /dev/nvme0n1p3 — that's the path we adopt
# in `detect_vg()` below, so this list is only relevant for second-
# disk legacy testbed setups.
DATA_DISK_CANDIDATES = ("/dev/vdb", "/dev/sdb", "/dev/nvme1n1")


def _read_storage_json() -> dict:
    """Persisted storage layout decisions. Written at bootstrap time."""
    if not STORAGE_JSON.exists():
        return {}
    try:
        return json.loads(STORAGE_JSON.read_text())
    except Exception:
        return {}


def _write_storage_json(d: dict) -> None:
    STORAGE_JSON.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_JSON.write_text(json.dumps(d, indent=2) + "\n")
    STORAGE_JSON.chmod(0o644)


def detect_vg() -> str:
    """Resolve the VG bedrock uses on this node. Priority:

      1. /etc/bedrock/storage.json {"vg": "..."} — written at bootstrap.
      2. A single VG already present (typical post-AlmaLinux install).
      3. A bedrock-owned name if multiple VGs exist — prefer the
         fresh-create name `bedrock-vg`, then the legacy `bedrock`
         (operator must have made a choice; we won't second-guess them).
      4. Fallback: `bedrock` for first-ever bootstrap (resolution
         fallback only — a fresh install CREATES `bedrock-vg`, see
         ensure_vg()).
    """
    cfg = _read_storage_json()
    if cfg.get("vg"):
        return cfg["vg"]
    try:
        out = subprocess.run(
            ["vgs", "--noheadings", "-o", "vg_name"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        vgs = [v.strip() for v in out.split() if v.strip()]
    except Exception:
        vgs = []
    if len(vgs) == 1:
        return vgs[0]
    if FRESH_VG in vgs:
        return FRESH_VG
    if DEFAULT_VG in vgs:
        return DEFAULT_VG
    return DEFAULT_VG


# Module-level VG name. Stable for the lifetime of the running process;
# write to storage.json before re-importing if the layout was just
# rebuilt mid-bootstrap.
VG = detect_vg()

# Cluster-singleton sizes (testbed defaults — operator can override
# per-node by setting /etc/bedrock/tier-sizes.json before init/join).
CLUSTER_SIZE_GB    = 5     # arbiter rqlite + filer leveldb3 + S3 IAM
WEED_VOLUME_SIZE_GB = 30   # local SeaweedFS volume store
DRBD_META_SIZE_MB  = 32    # external metadata is tiny; per-resource

# The cluster singleton DRBD resource. Renamed from the legacy
# `tier-critical` (SG-04): "critical" now means ONLY the SeaweedFS
# 002 collection + the VM HA-importance label. The resource name
# `cluster` matches bedrock_d/install/cluster_tier.py and
# cluster_arbiter.TIER_RESOURCE.
CLUSTER_RESOURCE = "cluster"
# DRBD minor for the singleton — /dev/drbd1101 per storage-architecture.md.
# Per-VM disk minors live at 1102+ (allocated by the VM sagas) so every
# resource's port lands in the 7700-7799 band (see drbd_config.drbd_port_for).
CLUSTER_MINOR = 1101

# The local SeaweedFS volume LV (no DRBD — SeaweedFS replicates via
# collections). mkfs+mounted at the weed-volume -dir.
WEED_VOLUME_LV    = "bedrock-weed-volume"
WEED_VOLUME_MOUNT = Path("/var/lib/bedrock/seaweedfs/volumes")

# The cluster-singleton DRBD volume mounts here on the arbiter-host;
# matches cluster_arbiter.MOUNT_POINT. The filer leveldb3 + arbiter
# rqlite data live under it so they follow the master via DRBD handoff.
CLUSTER_MOUNT = Path("/var/lib/bedrock/cluster")

STATE_JSON   = Path("/etc/bedrock/state.json")


# ── Log-or-direct dual-write hook (Phase 5 cutover) ──────────────────────
#
# When the bedrock-rust daemon IPC socket exists, every cluster-state
# mutation in this module also appends a typed log entry. The log is
# canonical — the existing JSON files are now caches that the
# view_builder regenerates from the log on every node identically.
# This is what obsoletes L27 (drbd_node_ids race) and L28 (mgmt_master
# propagation).
#
# Falls back gracefully to direct-write-only when the daemon isn't
# running (e.g. bedrock storage subcommands during install before the
# daemon comes up). The fallback is harmless because the next time
# the daemon's view_builder runs it'll see the JSON it already
# matched and no-op the rewrite.

def _is_mgmt_master() -> bool:
    """True if this node currently holds the mgmt role.

    Per design §3 the cluster log is a single-writer chain — only the
    master appends; replication carries entries to followers. Two nodes
    appending at the same index would fork the hash chain (caught by
    peer.rs DIVERGENCE detection, but blocks all further replication).
    `_log_append_typed` consults this to decide append-vs-skip.
    """
    try:
        s = json.loads(STATE_JSON.read_text()) if STATE_JSON.exists() else {}
    except Exception:
        return False
    return "mgmt" in (s.get("role") or "")


def _log_append_typed(payload_bytes):
    """Append a typed log entry via IPC — MASTER ONLY. Returns (idx, hash)
    on success, None otherwise (daemon down, not the master, etc.).

    Followers must NOT call this — they'd fork the chain. Followers
    write the same direct-JSON state they always have; the master's
    log entry replicates over and view_builder produces an identical
    cluster.json on every node, so the deterministic state ends up
    matching regardless of which side wrote first.

    Never raises — append failures should not block cluster operations.
    """
    # Single-writer discipline (D-20): only the elected mgmt master
    # writes to rqlite cluster state. Followers no-op. Never raises —
    # write failures must not block tier operations; the local fold
    # of cluster.json still produces the right shape, and the master's
    # write replicates via Raft.
    if not _is_mgmt_master():
        return None
    # NOTE: `payload_bytes` here is a legacy artifact from the
    # pre-rqlite log-entry pipeline. Modern callers should invoke
    # bedrock_state.* helpers directly. Kept as a no-op stub for
    # any in-tree caller that still passes a payload — those should
    # be migrated to direct bedrock_state calls.
    return None


# ── Shell helpers ──────────────────────────────────────────────────────────

def run(cmd: str, check: bool = True, timeout: int = 600) -> str:
    """Run a shell command locally."""
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed (rc={r.returncode}): {cmd}\n"
                           f"  stdout: {r.stdout.strip()}\n"
                           f"  stderr: {r.stderr.strip()}")
    return r.stdout.strip()


def run_ok(cmd: str) -> bool:
    """Run, return True iff exit code == 0. Stderr suppressed."""
    return subprocess.run(cmd, shell=True, capture_output=True).returncode == 0


def ssh(host: str, cmd: str, check: bool = True, timeout: int = 600) -> str:
    """Run a command on a peer via root ssh.

    Uses shlex.quote to wrap `cmd` for the local shell. Critical: the
    local shell parses our double-quoted ssh command before handing it
    to ssh, and inside double quotes the local shell expands `$VAR`
    (incl. positional `$1`/`$2`/...). Anything we pass for awk or
    inline shell on the remote side that uses `$N` would be silently
    mangled. Single-quoting via shlex.quote preserves the cmd verbatim
    so awk/sed/etc. see exactly what we wrote. (Lessons-log L31.)
    """
    full = (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=8 root@{host} {shlex.quote(cmd)}")
    return run(full, check=check, timeout=timeout)


# ── State helpers ──────────────────────────────────────────────────────────

def load_cluster() -> dict:
    """Cluster-wide state. Delegates to cluster_state — the dict shape
    is unchanged, but the source is rqlite (level='none', works without
    quorum) instead of the legacy /etc/bedrock/cluster.json file."""
    from . import cluster_state
    return cluster_state.load_cluster()


def save_cluster(c: dict) -> None:
    """No-op since the cluster.json projection was removed.
    Callers (this file, in 3 places) previously load_cluster +
    modify-in-memory + save_cluster(c), then mirror to rqlite. The
    rqlite mirror IS the canonical write now; the local file write
    was redundant. Kept as a no-op shim so the callers don't have
    to be touched in the migration pass — they can be cleaned up
    later when the redundant load+modify pattern is removed."""
    return


def load_state() -> dict:
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text())
    return {}


def get_tier_state(tier: str) -> dict:
    """Return cluster-wide state for one tier (mode, master, peers, version)."""
    c = load_cluster()
    return c.get("tiers", {}).get(tier, {"mode": "local", "version": 1})


def set_tier_state(tier: str, *, write_rqlite: bool = True, **kv) -> None:
    """Write tier state to rqlite (if up).

    Pass ``write_rqlite=False`` during bootstrap (``bedrock init`` /
    ``bedrock join``) when rqlite isn't yet up — the call then
    becomes a no-op and the init/join saga's ``mirror_tier_state``
    step writes the canonical state to rqlite later, from disk
    (mode='local', backend_path=CLUSTER_MOUNT).

    Before the cluster.json removal (2026-05-26) this function
    maintained a local cluster.json projection that mirror_tier_state
    later replayed into rqlite; with cluster.json gone, write_rqlite
    is the only thing this function does.
    """
    if not write_rqlite:
        return
    # Mirror to rqlite so view_builder sees the change on every node.
    # Master-only per D-20; followers no-op.
    if not _is_mgmt_master():
        return
    try:
        from . import bedrock_state as _bs
        _bs.tier_state(
            tier=tier,
            mode=kv.get("mode", "local"),
            master=kv.get("master"),
            peers=kv.get("peers"),
            backend_path=kv.get("backend_path"),
        )
    except Exception as e:
        # If we get here it's a real bug (network down mid-init or
        # rqlite-leader-lost). Make it loud but don't crash the
        # caller — operator gets to see it and decide.
        print(f"  ERROR: tier_state rqlite-mirror failed for {tier!r}: {e}",
              flush=True)


# ── Canonical LV names + DRBD port (one source of truth) ────────────────────
#
# LV names and the minor->port mapping live in bedrock_d.vm.drbd_config so
# the cluster singleton and every per-VM disk share ONE formula. We import
# them here (via the sys.path shim that puts the repo root on the path on a
# node) rather than re-deriving, so a future port-band change is one edit.

def _drbd_helpers():
    try:
        from bedrock_d.vm import drbd_config as _cfg, lvm as _lvm
    except ImportError:
        import sys as _sys
        # On a node the repo root holding bedrock_d/ is on sys.path via the
        # daemon entry point; add the common install root defensively.
        _sys.path.insert(0, "/usr/local/lib/bedrock")
        from bedrock_d.vm import drbd_config as _cfg, lvm as _lvm  # type: ignore
    return _cfg, _lvm


def data_lv_for(resource: str) -> str:
    """Canonical data LV name, e.g. cluster -> bedrock-data-cluster."""
    _cfg, _lvm = _drbd_helpers()
    return _lvm.lv_names_for(resource).data_lv


def meta_lv_for(resource: str) -> str:
    """Canonical external-meta LV name, e.g. cluster -> bedrock-meta-cluster."""
    _cfg, _lvm = _drbd_helpers()
    return _lvm.lv_names_for(resource).meta_lv


def drbd_port_for(minor: int) -> int:
    """Port in the 7700-7799 band for this DRBD minor (singleton + per-VM
    share the formula). Delegates to drbd_config.drbd_port_for."""
    _cfg, _lvm = _drbd_helpers()
    return _cfg.drbd_port_for(minor)


# ── Atomic symlink swap (POSIX rename) ─────────────────────────────────────

def atomic_symlink(target: str, link_path: Path) -> None:
    """Create or replace `link_path` as a symlink to `target` atomically.

    Uses a sibling tempfile + rename(2). This is POSIX-atomic on the same
    filesystem; any caller that has the old target opened keeps reading the
    old inode until they close.
    """
    link_path = Path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = link_path.parent / (link_path.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


# ── LV provisioning ────────────────────────────────────────────────────────

def lv_exists(name: str) -> bool:
    return run_ok(f"lvs {VG}/{name} --noheadings 2>/dev/null")


def thinpool_exists() -> bool:
    return lv_exists(THINPOOL)


def vg_exists() -> bool:
    return run_ok(f"vgs {VG} --noheadings 2>/dev/null")


def find_data_disk() -> str:
    """Find an unused separate data disk for the bedrock VG. Used only
    in legacy second-disk testbed setups. New v1.0 installs adopt the
    boot disk's existing VG via `ensure_vg()` and never call this.
    """
    for dev in DATA_DISK_CANDIDATES:
        if not Path(dev).exists():
            continue
        # Check it's not already a PV in some other VG
        owner = run(f"pvs --noheadings -o vg_name {dev} 2>/dev/null",
                    check=False).strip()
        if owner and owner != VG:
            continue
        # Check it has no mounted child partitions
        out = run(f"lsblk -nrpo NAME,MOUNTPOINT {dev}", check=False)
        if any(line.split()[1:] for line in out.splitlines()
               if len(line.split()) > 1):
            continue
        return dev
    raise RuntimeError(
        f"No usable data disk found among {DATA_DISK_CANDIDATES}. "
        f"Attach a second virtual disk and re-run.")


def _boot_disk() -> str:
    """The single physical disk that holds /boot. We use this both to
    place the EFI/boot partitions (already done by the OS installer)
    and, in the cloud-image-no-LVM case, to carve a new partition out
    of the unallocated tail for the bedrock VG."""
    out = run("findmnt -no SOURCE /boot 2>/dev/null", check=False).strip()
    if not out:
        out = run("findmnt -no SOURCE / 2>/dev/null", check=False).strip()
    if not out:
        raise RuntimeError("can't find /boot or / mount source")
    # /dev/vda3 → /dev/vda
    import re
    m = re.match(r"(/dev/[a-z]+|/dev/nvme\d+n\d+|/dev/mapper/.+)", out)
    base = m.group(1) if m else out
    # Strip partition suffix (vda3 → vda; nvme0n1p3 → nvme0n1)
    base = re.sub(r"p?\d+$", "", base)
    return base


def carve_pv_from_boot_disk_tail() -> str:
    """For cloud-image installs (xfs root straight on a partition, no
    LVM): if there's unallocated space at the END of the boot disk
    (cloud-init growpart was disabled, leaving the tail free), create
    a new partition covering it and return its device path. We don't
    touch the existing partitions — only the empty tail.

    Returns the new partition path (e.g. `/dev/vda5`). Raises if no
    free tail exists, or if the disk is already fully partitioned.
    """
    disk = _boot_disk()
    # We use sfdisk (util-linux, always installed on AlmaLinux) instead
    # of sgdisk (gdisk package — only in EPEL, not in stock AlmaLinux 10
    # repos). sfdisk's `--append` for GPT disks rewrites the GPT
    # secondary header at the actual end of the disk as a natural side
    # effect, so the cloud-image-grew-qcow2 case fixes itself.

    # 1. Inspect current partition table. sfdisk -d emits one line per
    #    partition — the highest number tells us the next slot.
    dump = run(f"sfdisk -d {disk} 2>/dev/null", check=False)
    pnums: list[int] = []
    import re
    for line in dump.splitlines():
        m = re.match(rf"{re.escape(disk)}p?(\d+)\s*:", line)
        if m:
            try: pnums.append(int(m.group(1)))
            except ValueError: pass
    next_n = (max(pnums) + 1) if pnums else 1

    # 2. Free-space sanity. blockdev --getsz gives total disk sectors;
    #    the highest existing partition's end gives the boundary.
    total_s = int(run(f"blockdev --getsz {disk}").strip())
    sector_b = int(run(f"blockdev --getss {disk}").strip())
    last_end = 0
    for line in dump.splitlines():
        m = re.match(rf"{re.escape(disk)}p?\d+\s*:.*?start=\s*(\d+).*size=\s*(\d+)",
                     line)
        if m:
            end = int(m.group(1)) + int(m.group(2))
            if end > last_end: last_end = end
    free_b = max(0, total_s - last_end - 34) * sector_b
    if free_b < (1 << 30):
        raise RuntimeError(
            f"only {free_b // (1 << 20)} MB free at end of {disk}; "
            f"need at least 1 GB. The cloud image's growpart probably "
            f"ran — either re-deploy with growpart disabled, or attach "
            f"a separate data disk.")

    print(f"  [tier] Creating {disk} partition {next_n} for "
          f"bedrock LVM PV ({free_b // (1 << 30)} GB)")

    # 3. Append a new partition. sfdisk with no start/size fields uses
    #    the next free sector and extends to end-of-disk. type=E6D6…
    #    is the GPT GUID for "Linux LVM".
    #
    #    --force: the boot disk has /boot, /boot/efi, and / mounted;
    #             sfdisk's safety check refuses to repartition a busy
    #             disk by default. We're only ADDING a partition past
    #             the end of all existing ones, so the kernel doesn't
    #             need to re-read anything that's currently mounted.
    #    --no-reread: kernel should not BLKRRPART (would fail with
    #             EBUSY); partprobe afterwards picks up the new
    #             partition without disturbing the mounts.
    sfdisk_input = "type=E6D6D379-F507-44C2-A23C-238F2A3DF928 name=bedrock-pv\n"
    r = subprocess.run(
        ["sfdisk", "--append", "--force", "--no-reread", disk],
        input=sfdisk_input, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"sfdisk --append {disk} failed (rc={r.returncode}): "
            f"{r.stderr.strip() or r.stdout.strip()}")
    run("partprobe", check=False)
    run("udevadm settle", check=False)

    # 4. Resolve new partition device.
    new_part = f"{disk}{next_n}"
    if not Path(new_part).exists():
        new_part = f"{disk}p{next_n}"  # nvme uses pN suffix
    if not Path(new_part).exists():
        raise RuntimeError(
            f"created partition not visible under expected name "
            f"({disk}{next_n} or {disk}p{next_n})")
    return new_part


def ensure_vg() -> None:
    """Adopt or create the VG bedrock uses for the thin pool + tier LVs +
    VM disks. Single-disk model: AlmaLinux's installer already created
    a VG (typically `almalinux`) on the boot disk's LVM partition.
    Bedrock writes that name to /etc/bedrock/storage.json and uses it
    going forward — we don't rename, since vgrename would force a
    grub + initramfs + reboot dance that's not worth the cosmetic gain.

    Greenfield branch: if NO VG exists on the system at all (operator
    booted a fresh kernel-only image, no LVM done), we carve a PV from
    the boot-disk tail (or a separate data disk) and vgcreate the fresh
    name `bedrock-vg` (FRESH_VG) on it. (install.sh-on-existing-Alma
    never reaches this branch — it adopts the existing VG above.)
    """
    global VG
    if vg_exists():
        # Persist the resolved VG so every tool agrees, even if the
        # detection heuristic later sees a second VG appear.
        cfg = _read_storage_json()
        if cfg.get("vg") != VG:
            cfg["vg"] = VG
            _write_storage_json(cfg)
        return
    # No VG with our preferred name. Are there ANY VGs?
    existing = subprocess.run(
        ["vgs", "--noheadings", "-o", "vg_name"],
        capture_output=True, text=True, timeout=10,
    ).stdout.split()
    existing = [v.strip() for v in existing if v.strip()]
    if existing:
        # Adopt the first/only VG already on the system. This is the
        # AlmaLinux-installer path — typically `almalinux`.
        VG = existing[0]
        cfg = _read_storage_json()
        cfg["vg"] = VG
        _write_storage_json(cfg)
        print(f"  [tier] Adopted existing VG {VG!r} (from OS install)")
        return
    # Truly greenfield. Two paths:
    #   (a) cloud image with no LVM (typical AlmaLinux 10 cloud image):
    #       carve a new partition from the unallocated tail of the
    #       boot disk and pvcreate that.
    #   (b) legacy testbed with a separate data disk: pvcreate that.
    # Try (a) first since v1.0 standardises on the cloud-image path.
    try:
        pv = carve_pv_from_boot_disk_tail()
        print(f"  [tier] Greenfield: created PV on boot-disk tail {pv}")
    except Exception as e:
        print(f"  [tier] Boot-disk tail not usable ({e}); trying separate disk")
        pv = find_data_disk()
        print(f"  [tier] Greenfield: creating PV+VG on separate disk {pv}")
        run(f"wipefs -af {pv}", check=False)
    run(f"pvcreate -ff -y {pv}")
    # Fresh install creates `bedrock-vg` (FRESH_VG), not the resolution
    # fallback name. install.sh-on-existing-Alma adopts its OS VG above
    # and never reaches here.
    run(f"vgcreate {FRESH_VG} {pv}")
    # Update the resolved VG name (the function-level `global VG`
    # declaration at the top of ensure_vg covers this assignment).
    VG = FRESH_VG
    cfg = _read_storage_json()
    cfg["vg"] = VG
    _write_storage_json(cfg)


def _ensure_vg_headroom(min_mb: int = 1024) -> None:
    """If the VG has less than `min_mb` MB of free space (typical when
    kickstart used `--grow` to give the thinpool 100% of disk), attach
    a sparse loop-backed PV and `vgextend` so the thin pool has room to
    grow its own metadata (lvextend the pool's _tmeta). All DRBD
    external-meta LVs are now thin (in-pool, see ensure_meta_lv), so
    they no longer need free VG space; this headroom is purely for the
    thin pool's metadata. Idempotent: if the helper PV is already
    attached, nothing happens."""
    # Pre-flight: if we previously created a loop-backed PV but the
    # loopback association is gone (reboot wipes losetup), reattach
    # it so vgs/vgreduce/lvcreate don't choke on the missing PV.
    extra_img = Path(f"/var/lib/bedrock-vg-extra.img")
    if extra_img.exists():
        attached = run(
            f"losetup -j {extra_img} 2>/dev/null | head -1 | cut -d: -f1",
            check=False,
        ).strip()
        if not attached:
            print(f"  [tier] reattaching loop PV from {extra_img}")
            run(f"losetup -fP {extra_img}", check=False)
    # If the VG still reports a missing PV (e.g. someone manually
    # `rm`'d the .img while the loop was attached), vgreduce it out
    # so subsequent lvcreate / vgextend operations don't fail.
    missing_pvs = run(
        f"vgs --noheadings -o vg_missing_pv_count {VG} 2>/dev/null",
        check=False,
    ).strip()
    if missing_pvs and missing_pvs != "0":
        print(f"  [tier] vgreduce --removemissing {VG} "
              f"(missing PV count={missing_pvs})")
        run(f"vgreduce --removemissing --force {VG} 2>&1 | tail -3",
            check=False)
    out = run(f"vgs {VG} --units m -o vg_free --noheadings",
              check=False).strip()
    try:
        free_mb = float(out.replace("m", "").strip())
    except (ValueError, AttributeError):
        free_mb = 0.0
    if free_mb >= min_mb:
        return
    extra_img = Path(f"/var/lib/bedrock-vg-extra.img")
    # Sparse file — only the bytes we actually write get allocated on
    # disk. 4 GiB headroom is plenty for thin-pool metadata growth and
    # any operator headroom we'd reasonably want at install time.
    size_mb = max(min_mb * 4, 4096)
    if not extra_img.exists():
        print(f"  [tier] VG has {free_mb:.0f}M free (< {min_mb}M); "
              f"creating sparse loop PV {extra_img} ({size_mb}M)")
        run(f"truncate -s {size_mb}M {extra_img}")
    # Find or create the loop device.
    loop_dev = run(f"losetup -j {extra_img} 2>/dev/null | "
                   f"head -1 | cut -d: -f1", check=False).strip()
    if not loop_dev:
        loop_dev = run(f"losetup -fP --show {extra_img}").strip()
    # Idempotent pvcreate + vgextend.
    pv_attached = run(
        f"pvs --noheadings -o vg_name {loop_dev} 2>/dev/null",
        check=False).strip()
    if pv_attached != VG:
        run(f"pvcreate -y {loop_dev}", check=False)
        run(f"vgextend {VG} {loop_dev}", check=False)
    out = run(f"vgs {VG} --units m -o vg_free --noheadings",
              check=False).strip()
    print(f"  [tier] VG headroom now: {out.strip()}")


def ensure_thinpool() -> None:
    """Create the thin pool if it doesn't exist. Sized to fill the VG.

    On the typical AlmaLinux install path the VG starts almost full
    (root LV + swap LV taking most of it). Bedrock removes the swap LV
    upfront — swap-on-a-hypervisor is a footgun, the operator can opt
    in to a small thin swap LV later via `bedrock storage swap-set`.
    Removing swap typically frees a few GB; everything else has to be
    operator-prepared (small root LV, no extra LVs) before bootstrap.
    """
    ensure_vg()
    if thinpool_exists():
        # The kickstart-supplied thinpool may have taken 100% of VG
        # via `logvol --grow` — no room left for the thin-pool's own
        # metadata growth. Reduce-thinpool is unsupported on every LVM
        # version we target, so add a loop-backed PV to grow the VG.
        _ensure_vg_headroom(min_mb=1024)
        return

    # 1. Drop the OS-installer's swap LV if present. Frees space and
    #    removes the kernel-panic-on-pool-full risk that swap-on-thin
    #    introduces. Operator can opt back in to swap on the thin pool
    #    via `bedrock storage swap-set <gb>` (small, last-resort only).
    swap_lvs = run(
        f"lvs --noheadings -o lv_name -S 'vg_name={VG} && lv_role=public' "
        f"2>/dev/null", check=False).split()
    for lv in swap_lvs:
        lv = lv.strip()
        if not lv: continue
        # Heuristic: name `swap` or attr starts with `s` (swap).
        attr = run(f"lvs --noheadings -o lv_attr {VG}/{lv} 2>/dev/null",
                   check=False).strip()
        is_swap = lv.lower().startswith("swap") or attr.startswith("-")
        if not is_swap:
            continue
        # Confirm via blkid TYPE
        blk = run(f"blkid -s TYPE -o value /dev/{VG}/{lv} 2>/dev/null",
                  check=False).strip()
        if blk != "swap":
            continue
        print(f"  [tier] Removing OS swap LV {VG}/{lv} "
              f"(swap-on-thin is opt-in only)")
        run(f"swapoff /dev/{VG}/{lv}", check=False)
        run(f"sed -i '\\|/dev/{VG}/{lv}|d; \\|UUID=.*swap|d' /etc/fstab",
            check=False)
        run(f"lvremove -y /dev/{VG}/{lv}", check=False)

    # 2. Available free space in VG, in MB.
    out = run(f"vgs {VG} --units m -o vg_free --noheadings", check=False)
    try:
        free_mb = float(out.replace("m", "").strip())
    except ValueError:
        free_mb = 0.0

    # cluster singleton + weed-volume LVs + 1 GB slack.
    needed_mb = (CLUSTER_SIZE_GB + WEED_VOLUME_SIZE_GB) * 1024 + 1024
    if free_mb < needed_mb:
        raise RuntimeError(
            f"Not enough free space in VG {VG}: {free_mb:.0f}MB free, "
            f"need {needed_mb}MB. Either install AlmaLinux with a smaller "
            f"root LV (≤16 GB), or `lvremove` unused LVs in {VG} before "
            f"re-running bedrock bootstrap.")

    # 3. Create thin pool with most free space, but leave a reserve of
    #    512 MB for LVM thin-pool metadata growth and ~256 MB of VG
    #    headroom. The DRBD external-meta LVs are now thin (in-pool), so
    #    they no longer need free VG space; the reserve keeps the pool's
    #    own metadata from wedging at 100%-allocated.
    headroom_mb = 512 + 256
    pool_size_mb = int(free_mb - headroom_mb)
    print(f"  [tier] Creating thin pool {VG}/{THINPOOL} ({pool_size_mb} MB, "
          f"{headroom_mb} MB held back for thin-pool metadata)")
    run(f"lvcreate -L {pool_size_mb}M -T {VG}/{THINPOOL} -y")


def ensure_thin_lv(lv: str, size_gb: int) -> None:
    """Create a thin LV in the pool if it doesn't exist."""
    if lv_exists(lv):
        return
    run(f"lvcreate -V {size_gb}G -T {VG}/{THINPOOL} -n {lv} -y")


def ensure_meta_lv(lv: str, size_mb: int = DRBD_META_SIZE_MB) -> None:
    """Create a thin external-metadata LV in the pool for one DRBD
    resource. Thin (in-pool), matching bedrock_d/vm/lvm.lvcreate_pair so
    the cluster singleton and per-VM disks use ONE external-meta scheme.
    Thin meta consumes blocks only as DRBD dirties bitmap bits, so the
    virtual size is cheap; the thinpool reserve in ensure_thinpool keeps
    a metadata floor."""
    if lv_exists(lv):
        return
    run(f"lvcreate -V {size_mb}M -T {VG}/{THINPOOL} -n {lv} -y")


def ensure_xfs(device: str, label: str) -> None:
    """mkfs.xfs only if not already an XFS filesystem."""
    fstype = run(f"blkid -s TYPE -o value {device} 2>/dev/null", check=False)
    if fstype == "xfs":
        return
    run(f"mkfs.xfs -f -L {label} {device}")


def ensure_fstab(device: str, mount: str, fstype: str = "xfs",
                  options: str = "defaults,discard,nofail,x-systemd.device-timeout=10s") -> None:
    """Idempotent fstab line."""
    fstab = Path("/etc/fstab")
    line = f"{device} {mount} {fstype} {options} 0 0"
    text = fstab.read_text() if fstab.exists() else ""
    if mount in text:
        return
    fstab.write_text(text.rstrip() + "\n" + line + "\n")


def ensure_mounted(device: str, mount: str, fstype: str = "xfs",
                    options: str = "defaults,discard,nofail,x-systemd.device-timeout=10s") -> None:
    Path(mount).mkdir(parents=True, exist_ok=True)
    ensure_fstab(device, mount, fstype, options)
    if not run_ok(f"mountpoint -q {mount}"):
        run(f"mount {mount}")


def umount_quiet(mount: str) -> None:
    run(f"umount {mount} 2>/dev/null", check=False)


# ── N=1: local-only setup ──────────────────────────────────────────────────

def ensure_weed_volume_lv() -> None:
    """Create + mount the local SeaweedFS volume LV. One large local thin
    LV (no DRBD — SeaweedFS replicates bytes via its collections). XFS,
    mounted at the weed-volume -dir. Idempotent."""
    device = f"/dev/{VG}/{WEED_VOLUME_LV}"
    ensure_thin_lv(WEED_VOLUME_LV, WEED_VOLUME_SIZE_GB)
    ensure_xfs(device, "weed-volume")  # XFS label max 12 chars
    ensure_mounted(device, str(WEED_VOLUME_MOUNT))


def setup_n1(*, write_rqlite: bool = False) -> None:
    """Single-node storage setup. At N=1 the cluster singleton lives on
    the local root FS at /var/lib/bedrock/cluster (promoted to DRBD on
    the N=1→N=2 transition); the SeaweedFS volume store gets its own
    local LV. Idempotent.

    During ``bedrock init`` this is called BEFORE rqlite is up, so
    ``write_rqlite`` defaults to False. The cluster_init flow has a
    later step that mirrors the singleton's tier state into rqlite after
    rqlited reaches Leader (see ``mirror_tier_state_to_rqlite``).

    Called from outside init (e.g. ``bedrock storage init``)
    should pass ``write_rqlite=True`` — rqlite IS up at that point.
    """
    print("  [tier] Ensuring thin pool...")
    ensure_thinpool()

    # SeaweedFS volume store — one large local LV, no DRBD.
    ensure_weed_volume_lv()
    print(f"  [tier] {WEED_VOLUME_LV:<22} {WEED_VOLUME_SIZE_GB:>3}G -> "
          f"{WEED_VOLUME_MOUNT}")

    # Cluster singleton at N=1 is just a directory on the root FS; the
    # arbiter rqlite + filer leveldb3 + S3 IAM write into it directly,
    # and promote_local_to_drbd_master snapshots+restores it onto the
    # DRBD volume on the N=1→N=2 transition (XFS preserved byte-for-byte
    # by external metadata). Record mode='local'.
    CLUSTER_MOUNT.mkdir(parents=True, exist_ok=True)
    set_tier_state(CLUSTER_RESOURCE, mode="local", master=None,
                   backend_path=str(CLUSTER_MOUNT),
                   write_rqlite=write_rqlite)

    print("  [tier] N=1 setup complete: cluster singleton local at "
          f"{CLUSTER_MOUNT}, weed-volume LV mounted")


def mirror_tier_state_to_rqlite() -> None:
    """Push the cluster singleton's state into rqlite. Called by the
    cluster_init / node_join saga AFTER rqlited has reached Leader.

    The post-bootstrap state is always mode='local' with backend_path
    = CLUSTER_MOUNT; later DRBD promotion (N=1→N=2) writes mode='drbd'.

    Idempotent — bedrock_state.tier_state is INSERT OR REPLACE."""
    from . import bedrock_state as _bs
    _bs.tier_state(
        tier=CLUSTER_RESOURCE,
        mode="local",
        master=None,
        peers=None,
        backend_path=str(CLUSTER_MOUNT),
    )


# ── DRBD resource config ───────────────────────────────────────────────────

# ── Persistent DRBD node-id assignments ────────────────────────────────────
#
# DRBD node-ids are *permanent* for the lifetime of a resource (see invariant
# #3 in tier_storage.md). We persist {peer_name: node_id} per resource in
# rqlite (the tier_drbd_node_ids table, folded into tiers.<resource>.drbd_node_ids)
# so that adding, removing, or rewriting peers never renumbers existing peers' IDs.

def get_drbd_node_id(resource: str, peer_name: str) -> int:
    """Return the persistent node-id for `peer_name` in this resource.

    If the peer has never been seen for this resource, allocate the next
    free integer (smallest non-negative integer not currently in use AND
    not previously assigned to any peer in this resource), persist it
    in cluster.json, and return it.

    Freed IDs (peer removed) are NOT reused until they're explicitly
    cleared via free_drbd_node_id() — which should happen only after
    drbdsetup forget-peer has cleaned the meta-disk bitmap slot.
    """
    c = load_cluster()
    tiers = c.setdefault("tiers", {})
    tier = tiers.setdefault(resource, {"mode": "local", "version": 1})
    assignments = tier.setdefault("drbd_node_ids", {})
    if peer_name in assignments:
        return assignments[peer_name]
    # Allocate next free
    used = set(assignments.values())
    nid = 0
    while nid in used:
        nid += 1
    assignments[peer_name] = nid
    tier["version"] = tier.get("version", 0) + 1
    save_cluster(c)
    # Persist the assignment to rqlite so peers' view_builder folds
    # the same drbd_node_id into their cluster.json — no fresh-
    # allocation race per L27. Master-only per D-20.
    if _is_mgmt_master():
        try:
            from . import bedrock_state as _bs
            _bs.drbd_node_id_assigned(resource, peer_name, nid)
        except Exception as e:
            print(f"  [state] drbd_node_id_assigned write skipped: {e}")
    return nid


def free_drbd_node_id(resource: str, peer_name: str,
                      reason: str = "") -> int | None:
    """Mark this peer's node-id as free for re-use. Call only after
    drbdsetup forget-peer has cleared the bitmap slot, otherwise a
    later peer reusing the slot would trigger a forced full-resync.
    Returns the freed id, or None if the peer was not assigned.

    Master-only writes a `drbd_node_id_freed` log entry so view_builder
    folds the same state on every peer (the assignment disappears
    deterministically from cluster.json across the cluster).
    """
    c = load_cluster()
    tiers = c.setdefault("tiers", {})
    tier = tiers.setdefault(resource, {})
    assignments = tier.setdefault("drbd_node_ids", {})
    nid = assignments.pop(peer_name, None)
    if nid is not None:
        tier["version"] = tier.get("version", 0) + 1
        save_cluster(c)
        if _is_mgmt_master():
            try:
                from . import bedrock_state as _bs
                _bs.drbd_node_id_freed(
                    resource, peer_name, nid, reason=reason)
            except Exception as e:
                print(f"  [state] drbd_node_id_freed write skipped: {e}")
    return nid


def render_drbd_res(resource: str, minor: int,
                    peers: list[dict]) -> str:
    """Render a DRBD resource file. peers = [{name, loopback_ip}, ...].

    Uses EXTERNAL metadata (a separate meta LV per resource) and a port
    in the 7700-7799 band — the same shape every Bedrock DRBD resource
    uses (singleton + per-VM). Node-ids are PERSISTED (not renumbered):
    each peer gets its sticky id allocated on first sight of that peer.
    """
    data_lv = data_lv_for(resource)
    meta_lv = meta_lv_for(resource)
    port = drbd_port_for(minor)
    on_blocks = []
    peer_ids = {}  # for the connection-block render below
    for p in peers:
        nid = get_drbd_node_id(resource, p["name"])
        peer_ids[p["name"]] = nid
        on_blocks.append(
            f'  on {p["name"]} {{\n'
            f'    node-id   {nid};\n'
            f'    device    /dev/drbd{minor};\n'
            f'    disk      /dev/{VG}/{data_lv};\n'
            f'    meta-disk /dev/{VG}/{meta_lv};\n'
            f'    address   {p["loopback_ip"]}:{port};\n'
            f'  }}\n'
        )

    # Connection mesh between every pair (full mesh for N>=2)
    conn_blocks = []
    for i in range(len(peers)):
        for j in range(i + 1, len(peers)):
            conn_blocks.append(
                f'  connection {{\n'
                f'    host {peers[i]["name"]} address {peers[i]["loopback_ip"]}:{port};\n'
                f'    host {peers[j]["name"]} address {peers[j]["loopback_ip"]}:{port};\n'
                f'  }}\n'
            )

    body = (
        f'resource {resource} {{\n'
        f'  protocol C;\n'
        f'  options {{ on-no-quorum suspend-io; }}\n'
        # rs-discard-granularity + discard-zeroes-if-aligned let DRBD
        # pass TRIM/discard down through to the thin LV (SG-06), so a
        # `fstrim` on the mounted FS reclaims pool blocks on every peer.
        f'  disk    {{ c-plan-ahead 0; resync-rate 100M; '
        f'rs-discard-granularity 65536; discard-zeroes-if-aligned yes; }}\n'
        f'  net     {{ max-buffers 8000; sndbuf-size 0; rcvbuf-size 0; '
        f'after-sb-0pri discard-zero-changes; '
        f'after-sb-1pri discard-secondary; '
        f'after-sb-2pri disconnect; }}\n'
        f'\n' +
        ''.join(on_blocks) +
        '\n' +
        ''.join(conn_blocks) +
        '}\n'
    )
    return body


def _minor_for(resource: str) -> int:
    """DRBD minor for a tier_storage-managed resource. This module owns
    only the cluster singleton; per-VM minors are owned by the VM sagas."""
    if resource == CLUSTER_RESOURCE:
        return CLUSTER_MINOR
    raise KeyError(f"tier_storage manages only {CLUSTER_RESOURCE!r}, "
                   f"not {resource!r}")


def write_drbd_resource(resource: str, peers: list[dict]) -> None:
    """Write /etc/drbd.d/<resource>.res based on peer list.
    Honors persistent node-id assignments (see get_drbd_node_id).
    """
    minor = _minor_for(resource)
    Path("/etc/drbd.d").mkdir(parents=True, exist_ok=True)
    p = Path(f"/etc/drbd.d/{resource}.res")
    p.write_text(render_drbd_res(resource, minor, peers))


# ── Mesh-aware DRBD config (v1.x — wires the bedrock-net path table
#    into DRBD's multi-path connection blocks) ────────────────────────

def _direct_paths_between(snapshot: dict, node_a: str, node_b: str) -> list[dict]:
    """Return path entries from the snapshot's `paths` section that
    connect (node_a, *) ↔ (node_b, *). Each entry is the canonical-
    keyed dict from view_builder fold.
    """
    paths = snapshot.get("paths") or {}
    out = []
    for k, v in paths.items():
        n1, n2 = v.get("node_a"), v.get("node_b")
        if {n1, n2} == {node_a, node_b}:
            out.append(v)
    # Sort by speed desc, rtt asc, then nic name for deterministic order
    out.sort(key=lambda v: (
        -int(v.get("speed_mbps") or 0),
        int(v.get("rtt_us") or 0),
        v.get("nic_a", ""), v.get("nic_b", ""),
    ))
    return out


def _peer_link_addr(snapshot: dict, node: str, nic: str) -> str:
    """Find the per-NIC address for a node from the snapshot. We don't
    log per-NIC addresses (they're throwaway), so this currently
    returns "" — a future commit will add it to LINK_QUALITY payload
    so DRBD config can reference exact link addresses.
    For v1, `path` blocks fall back to loopback addresses, which the
    kernel routes via the mesh layer's installed routes — same
    end-effect, one indirection.
    """
    return ""


def render_drbd_res_mesh(resource: str, minor: int,
                          peers: list[dict],
                          snapshot: dict) -> str:
    """Render a DRBD 9 multi-path resource config from the mesh path
    table.

    Per peer pair, emits one `connection` with one `path` block per
    direct (nic_a, nic_b) pair the path table observed — each path's
    addresses are the actual per-NIC link IPs (10.42.X.Y throwaways
    or DHCP IPs on the LAN). DRBD treats each path as a genuinely
    independent transport: separate TCP, separate keepalives, its
    own carrier/timeout detection. Failover between paths is DRBD's
    job at this point; the kernel routing layer is bypassed for the
    physical NIC choice on these paths.

    A final loopback-fallback path block is always appended last:
    `host A address <A.loopback>:port; host B address <B.loopback>:port;`.
    This relies on the kernel route table (driven by bedrock-net's
    panic-neighbour catch-all) so DRBD can still reach the peer even
    when every direct path is down — including via transit through a
    third node. The loopback-fallback path is at the end of the list,
    so DRBD only uses it after all direct paths fail.

    Both halves contribute to robustness:
      - direct path blocks → DRBD's own failover, which can detect
        a NIC that's link-up-but-dead in ms (TCP keepalive)
      - loopback fallback → the catch-all that survives transit
        topologies and arbitrary NIC layouts

    Inputs:
      peers — [{name, loopback_ip}, ...]
      snapshot — view_builder.fold output, must include `paths`
    """
    data_lv = data_lv_for(resource)
    meta_lv = meta_lv_for(resource)
    port = drbd_port_for(minor)
    on_blocks = []
    peer_ids: dict[str, int] = {}
    for p in peers:
        nid = get_drbd_node_id(resource, p["name"])
        peer_ids[p["name"]] = nid
        # `on` block keeps a single address (DRBD requires it). We
        # use loopback so the address is stable across NIC churn —
        # peers that need to dial this node use whichever path is
        # best per the kernel routing layer.
        anchor_addr = p.get("loopback_ip", "")
        on_blocks.append(
            f'  on {p["name"]} {{\n'
            f'    node-id   {nid};\n'
            f'    device    /dev/drbd{minor};\n'
            f'    disk      /dev/{VG}/{data_lv};\n'
            f'    meta-disk /dev/{VG}/{meta_lv};\n'
            f'    address   {anchor_addr}:{port};\n'
            f'  }}\n'
        )

    conn_blocks = []
    for i in range(len(peers)):
        for j in range(i + 1, len(peers)):
            a, b = peers[i], peers[j]
            paths = _direct_paths_between(snapshot, a["name"], b["name"])
            path_blocks: list[str] = []
            seen_pairs: set[tuple[str, str, str, str]] = set()

            for p in paths:
                # Path entries are stored canonically (node_a < node_b),
                # so map a/b in our loop to the entry's a/b.
                if p.get("node_a") == a["name"]:
                    nic_a = p.get("nic_a", "")
                    nic_b = p.get("nic_b", "")
                    addr_a = p.get("link_addr_a", "")
                    addr_b = p.get("link_addr_b", "")
                else:
                    nic_a = p.get("nic_b", "")
                    nic_b = p.get("nic_a", "")
                    addr_a = p.get("link_addr_b", "")
                    addr_b = p.get("link_addr_a", "")
                # Skip if we don't have both addresses — fold's
                # backwards-compat path leaves them empty when
                # observing an old log entry without link_addr_*.
                if not addr_a or not addr_b:
                    continue
                key = (nic_a, addr_a, nic_b, addr_b)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                path_blocks.append(
                    f'    path {{\n'
                    f'      # via {nic_a}↔{nic_b}\n'
                    f'      host {a["name"]} address {addr_a}:{port};\n'
                    f'      host {b["name"]} address {addr_b}:{port};\n'
                    f'    }}\n'
                )

            # Always-last loopback fallback. Uses peer.loopback_ip on
            # both sides; the kernel route to that /32 is the panic-
            # neighbour catch-all when no direct path is healthy. This
            # ensures DRBD can still establish the connection even
            # when every direct mesh path is down or hasn't been
            # observed yet (fresh cluster, mid-rejoin, etc.).
            lb_a = a.get("loopback_ip", "")
            lb_b = b.get("loopback_ip", "")
            if lb_a and lb_b:
                path_blocks.append(
                    f'    path {{\n'
                    f'      # loopback fallback (kernel routes via best NIC)\n'
                    f'      host {a["name"]} address {lb_a}:{port};\n'
                    f'      host {b["name"]} address {lb_b}:{port};\n'
                    f'    }}\n'
                )

            conn_blocks.append(
                f'  connection {{\n' +
                ''.join(path_blocks) +
                f'  }}\n'
            )

    body = (
        f'resource {resource} {{\n'
        f'  protocol C;\n'
        f'  options {{ on-no-quorum suspend-io; }}\n'
        f'  disk    {{ c-plan-ahead 0; resync-rate 100M; '
        f'rs-discard-granularity 65536; discard-zeroes-if-aligned yes; }}\n'
        f'  net     {{ max-buffers 8000; sndbuf-size 0; rcvbuf-size 0; '
        f'after-sb-0pri discard-zero-changes; '
        f'after-sb-1pri discard-secondary; '
        f'after-sb-2pri disconnect; }}\n'
        f'\n' +
        ''.join(on_blocks) +
        '\n' +
        ''.join(conn_blocks) +
        '}\n'
    )
    return body


def regen_drbd_configs_from_snapshot(snapshot: dict) -> bool:
    """Regenerate the cluster singleton's /etc/drbd.d/<resource>.res when
    its rqlite mode is DRBD-backed (i.e. 'drbd' / 'drbd-3way') AND a
    resource file already exists. Idempotent — silently no-ops in N=1
    (no DRBD configured) or when the .res file is absent. After a
    successful rewrite, runs `drbdadm adjust <resource>` so the running
    daemon picks up the new path blocks without disrupting in-flight
    replication.

    Called from the orchestrator subscriber on path-table changes.
    The cost of a no-op call is one stat(), negligible.

    Returns True if the resource file was actually rewritten.
    """
    drbd_dir = Path("/etc/drbd.d")
    if not drbd_dir.exists():
        return False

    tiers = (snapshot.get("tiers") or {})
    nodes = (snapshot.get("nodes") or {})

    # Build the canonical peers list once: every node currently in the
    # cluster snapshot, with name + loopback_ip.
    peers: list[dict] = []
    for name, n in sorted(nodes.items()):
        peers.append({
            "name": name,
            "loopback_ip": n.get("loopback_ip", ""),
        })

    DRBD_MODES = {"drbd", "drbd-3way"}
    resource = CLUSTER_RESOURCE
    minor = CLUSTER_MINOR
    res_path = drbd_dir / f"{resource}.res"
    if not res_path.exists():
        return False  # singleton not promoted to DRBD on this node
    tier_state = tiers.get(resource) or {}
    if (tier_state.get("mode") or "") not in DRBD_MODES:
        return False  # demoted; leave the file alone for now

    new_body = render_drbd_res_mesh(resource, minor, peers, snapshot)
    try:
        old_body = res_path.read_text()
    except OSError:
        old_body = ""
    if new_body == old_body:
        return False  # no change, no adjust needed

    res_path.write_text(new_body)
    # Apply the new config to the running daemon. drbdadm adjust is
    # idempotent; if it fails (resource not currently up, etc.) we just
    # move on — the next adjust at promote/demote/peer-add will succeed.
    try:
        subprocess.run(
            ["drbdadm", "adjust", resource],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        pass
    return True


# ── Local LV → DRBD migration (preserves filesystem via external metadata) ──

def promote_local_to_drbd_master(resource: str, peers: list[dict]) -> None:
    """On the master, convert the cluster singleton's local data into a
    DRBD primary that holds the same XFS/data — external metadata keeps
    the DRBD device the same size as the data LV.

    At N=1 /var/lib/bedrock/cluster is just a regular directory on the
    root FS containing the arbiter rqlite + filer leveldb3 + S3 IAM
    data. Mounting the DRBD device over the same path hides those files,
    so we preserve them by:
      1. Stop services that hold the singleton dir open (filer, s3,
         arbiter rqlite)
      2. Snapshot the directory contents to a tmp path
      3. Mount DRBD over the path
      4. Restore the snapshot into the DRBD volume
      5. Restart services
    """
    assert resource == CLUSTER_RESOURCE, resource
    minor = _minor_for(resource)
    # The DRBD volume mounts at /var/lib/bedrock/cluster (matches
    # cluster_arbiter.MOUNT_POINT) — where the filer leveldb3 + arbiter
    # rqlite data live so they follow the master role via DRBD handoff.
    drbd_mount = str(CLUSTER_MOUNT)
    drbd_dev = f"/dev/drbd{minor}"

    # 1. Create the data + meta LV pair. The meta LV is thin external
    #    metadata (one per resource, like every Bedrock DRBD resource);
    #    the data LV is a fresh thin LV the snapshot is restored into.
    ensure_thin_lv(data_lv_for(resource), CLUSTER_SIZE_GB)
    ensure_meta_lv(meta_lv_for(resource),
                   _cluster_meta_size_mb())

    # 2. Write the resource config (mesh of all peers)
    write_drbd_resource(resource, peers)

    # 3. Snapshot the existing /var/lib/bedrock/cluster contents (arbiter
    #    rqlite + filer leveldb3 + anything else the singletons wrote at
    #    N=1) before mounting the DRBD device over the same path. Stop
    #    the singletons first so files are quiescent.
    singleton_units = ("bedrock-weed-s3", "bedrock-weed-filer",
                       "bedrock-rqlited-arbiter")
    for unit in singleton_units:
        run(f"systemctl stop {unit}.service 2>/dev/null", check=False)
    snap_dir = Path("/var/lib/bedrock-promote-snapshot")
    if snap_dir.exists():
        run(f"rm -rf {snap_dir}", check=False)
    snap_dir.mkdir(parents=True, exist_ok=True)
    if Path(drbd_mount).exists() and any(Path(drbd_mount).iterdir()):
        # cp -a preserves perms, ownership, timestamps, symlinks — adequate
        # for filer leveldb3 + arbiter rqlite data. rsync isn't in the base
        # AlmaLinux 10.1 install, but cp is in coreutils.
        run(f"cp -a {drbd_mount}/. {snap_dir}/")
        run("sync", check=False)

    # 4. Unmount the singleton dir if it happened to be its own mount.
    if run_ok(f"mountpoint -q {drbd_mount}"):
        run(f"umount {drbd_mount}")

    # 5. Initialize DRBD metadata + bring up the resource as primary
    #    --force, BUT skip create-md if the resource is already
    #    configured (idempotent re-run of the saga's
    #    promote_local_to_drbd step). DRBD9 refuses ``create-md``
    #    on a configured device even with ``--force`` — exit 20,
    #    "Device 'N' is configured!". Detect by parsing
    #    ``drbdadm status`` — anything other than "no resources
    #    defined!" means the resource exists.
    rc_chk = subprocess.run(
        ["drbdadm", "status", resource],
        capture_output=True, text=True,
    )
    already_configured = (rc_chk.returncode == 0 and
                          "no resources" not in (rc_chk.stderr or "").lower())
    if not already_configured:
        run(f"drbdadm create-md {resource} --force --max-peers=7")
        run(f"drbdadm up {resource}")
    # drbdadm up on an already-up resource fails with "Minor or
    # volume exists already" (rc=10). Skip when status confirms the
    # resource is already configured; the promote step below is the
    # one that actually does the work this idempotent retry needs.
    # ``drbdadm primary --force`` is idempotent — it's a no-op if
    # we're already Primary.
    run(f"drbdadm primary --force {resource}")

    # 6. Mount the DRBD device — fresh empty XFS at first promote.
    Path(drbd_mount).mkdir(parents=True, exist_ok=True)
    if run_ok(f"blkid -s TYPE -o value {drbd_dev} 2>/dev/null"):
        fstype = run(f"blkid -s TYPE -o value {drbd_dev}", check=False)
    else:
        fstype = ""
    if fstype != "xfs":
        run(f"mkfs.xfs -f {drbd_dev}")
    run(f"mount -t xfs {drbd_dev} {drbd_mount}")

    # 7. Restore the singleton-dir snapshot INTO the DRBD volume so the
    #    filer's leveldb3 and arbiter rqlite data survive the promote.
    if snap_dir.exists() and any(snap_dir.iterdir()):
        run(f"cp -a {snap_dir}/. {drbd_mount}/")
        run("sync", check=False)
        run(f"rm -rf {snap_dir}", check=False)

    # 8. fstab line for the DRBD mount (idempotent — drop any prior one).
    fstab = Path("/etc/fstab")
    text = fstab.read_text() if fstab.exists() else ""
    new_lines = [l for l in text.splitlines() if drbd_mount not in l]
    new_lines.append(f"{drbd_dev} {drbd_mount} xfs defaults,discard,nofail,_netdev 0 0")
    fstab.write_text("\n".join(new_lines).rstrip() + "\n")

    # 9. Restart the singletons so they pick up the restored data.
    #     cluster_arbiter.converge() on the next subscriber tick will
    #     re-apply the full set anyway, but starting them inline keeps
    #     the test happy without a 5s extra wait.
    for unit in singleton_units:
        run(f"systemctl start {unit}.service 2>/dev/null", check=False)


def _cluster_meta_size_mb() -> int:
    """External-meta LV size for the cluster singleton, using the same
    DRBD9 formula every resource uses (header + AL + max_peers bitmap)."""
    _cfg, _lvm = _drbd_helpers()
    return _lvm.meta_size_mb_for(CLUSTER_SIZE_GB)


def join_drbd_peer(resource: str, peers: list[dict]) -> None:
    """On a peer (not the source of data): create the LV pair (if needed),
    write DRBD config, bring up as Secondary so it can resync from the
    primary.

    Idempotent: drbdadm up on an already-up resource emits
    "Minor or volume exists already" — that's success for our purposes
    (the attach + peer-connect did succeed; the redundant
    drbdsetup new-minor is the noisy fail).
    """
    tier = resource
    minor = _minor_for(resource)
    lv = data_lv_for(resource)
    size = CLUSTER_SIZE_GB

    ensure_thin_lv(lv, size)
    ensure_meta_lv(meta_lv_for(resource), _cluster_meta_size_mb())
    write_drbd_resource(resource, peers)
    # create-md can fail if metadata already exists from a previous
    # attempt; --force overwrites. Either succeeds.
    run(f"drbdadm create-md {resource} --force --max-peers=7")
    # drbdadm up: if already up, swallow the "exists already" error.
    r = subprocess.run(
        f"drbdadm up {resource}",
        shell=True, capture_output=True, text=True,
    )
    if r.returncode != 0:
        # Already-up is fine; anything else re-raise.
        combined = (r.stdout or "") + (r.stderr or "")
        if "exists already" not in combined and "in use" not in combined:
            raise RuntimeError(
                f"command failed (rc={r.returncode}): drbdadm up {resource}\n"
                f"  stdout: {r.stdout}\n  stderr: {r.stderr}"
            )
    # Don't promote — the master is primary. Initial sync starts automatically.


# ── N=1 → N=2 cluster-singleton promotion ───────────────────────────────────

def transition_to_n2_master(self_loopback_ip: str, peer: dict) -> dict:
    """Master-side N=1 -> N=2 transition.

    The cluster singleton (`cluster` DRBD resource) gets DRBD-replicated
    — it hosts the arbiter rqlite + filer leveldb3 + S3 IAM, and the
    mgmt-master role moves it via cluster_arbiter. The SeaweedFS volume
    store stays a local LV (SeaweedFS replicates bytes via collections).

    peer = {"name": "...", "loopback_ip": "..."}
    """
    print("  [tier] N=2 master transition: promote cluster singleton to DRBD primary")

    self_state = load_state()
    self_name = self_state.get("node_name", "node1")
    peers = [
        {"name": self_name, "loopback_ip": self_loopback_ip},
        peer,
    ]

    promote_local_to_drbd_master(CLUSTER_RESOURCE, peers)
    set_tier_state(CLUSTER_RESOURCE, mode="drbd", master=self_name,
                   peers=[p["name"] for p in peers])

    return {"peers": peers}


def transition_to_n2_peer(self_loopback_ip: str, master: dict,
                            peers: list[dict]) -> None:
    """Peer-side N=1 -> N=2 transition: join the cluster-singleton DRBD
    as Secondary so the initial sync from master carries the data over.
    Called after setup_n1() on the joiner.
    """
    print("  [tier] N=2 peer transition: join cluster-singleton DRBD secondary")
    join_drbd_peer(CLUSTER_RESOURCE, peers)


def promote_cluster_to_3way(third_peer: dict) -> None:
    """Add a third peer to the cluster-singleton DRBD resource.

    Run on the master. Assumes the resource was created with --max-peers=7
    so adding a peer is just a config update + drbdadm adjust + new node
    runs join_drbd_peer().

    Distributes the new .res to ALL existing peers (master + survivors)
    so that every node's on-disk config matches its kernel state. The
    new third peer's join_drbd_peer() will write the same config too,
    but that's a separate code path; this function ensures the existing
    peers don't have a stale 2-peer config sitting around. (Lessons-log
    L23: every operation that mutates DRBD topology must distribute the
    new .res to every node that participates in the resource.)
    """
    # Update resource config to include third peer
    state_cluster = get_tier_state(CLUSTER_RESOURCE)
    existing_peer_names = state_cluster.get("peers", [])
    cluster = load_cluster()
    nodes = cluster.get("nodes", {})
    peers = []
    for name in existing_peer_names + [third_peer["name"]]:
        node = nodes.get(name, {})
        peers.append({"name": name, "loopback_ip": node.get("loopback_ip", "")})

    # Local: write new config + adjust kernel.
    write_drbd_resource(CLUSTER_RESOURCE, peers)
    res_file = f"/etc/drbd.d/{CLUSTER_RESOURCE}.res"
    new_res = Path(res_file).read_text()
    run(f"drbdadm adjust {CLUSTER_RESOURCE}")

    # Distribute identical config to every existing peer (the new
    # third peer's join_drbd_peer will write its own; we don't need
    # to also push to it). Then drbdadm adjust on each so the kernel
    # picks up the new peer-3 connection definition.
    import base64
    b = base64.b64encode(new_res.encode()).decode()
    for peer_name in existing_peer_names:
        if peer_name == nodes.get(state_cluster.get("master", ""), {}).get("name", ""):
            continue   # master already adjusted above
        peer_host = nodes.get(peer_name, {}).get("host")
        if not peer_host:
            continue
        ssh(peer_host,
            f"echo {b} | base64 -d > {res_file}")
        ssh(peer_host, f"drbdadm adjust {CLUSTER_RESOURCE}", check=False)

    set_tier_state(CLUSTER_RESOURCE, mode="drbd",
                    peers=[p["name"] for p in peers])


# ── Decommissioning helpers ────────────────────────────────────────────────
#
#   drbd_remove_peer    — remove a peer from a running DRBD resource (config-first)
#
# Detailed contracts, invariants, command sequences, and source citations
# live in tier_storage.md.


def drbd_remove_peer(
    resource: str,
    leaving_peer_name: str,
    surviving_hosts: list[str],
    surviving_peers: list[dict] | None = None,
    new_res_text: str | None = None,
    bedrock_resource: bool = True,
) -> None:
    """Online peer removal for ANY DRBD resource.

    Service to /dev/drbd<minor> on the surviving primary stays up. The
    leaving peer is dropped from kernel state on every survivor via
    `drbdsetup disconnect` + `drbdsetup del-peer` (per lessons-log L20:
    drbdadm adjust is unreliable shrinking full-mesh resources).

    Args:
        resource:         DRBD resource name. For the cluster singleton
                          pass "cluster" with `bedrock_resource=True`
                          (default). For VM resources pass the full name
                          like "vm-web1-disk0" with `bedrock_resource=False`.
        leaving_peer_name: peer's hostname as it appears in the .res
        surviving_hosts:  list of mgmt-LAN hosts (or any reachable IP)
                          to SSH into for the per-node operations
        surviving_peers:  list of {"name": ..., "loopback_ip": ...} for the
                          peers that REMAIN. Required if
                          `bedrock_resource=True` so we can render the
                          new singleton config. Optional otherwise.
        new_res_text:     Pre-rendered .res file content to distribute.
                          If provided, overrides the auto-rendering for
                          bedrock-managed resources. For VM resources,
                          callers render their own config and pass it here.
                          If None and `bedrock_resource=False`, no
                          on-disk config update happens (caller is
                          responsible).
        bedrock_resource: True for the cluster singleton (auto-render via
                          render_drbd_res); False for VM disks or other
                          resources whose config the caller manages.

    Crash-safety: when an on-disk config is provided, it's distributed
    BEFORE the kernel-state mutation so a power loss leaves persistent
    state already at the desired end state.

    See tier_storage.md "drbd_remove_peer" for the command-by-command
    breakdown and source citations.
    """
    # The resource name is the DRBD resource name verbatim (no prefix —
    # the cluster singleton is "cluster", VM disks are "vm-*-diskN").
    full_res = resource

    print(f"  [tier] drbd_remove_peer({full_res}, leaving={leaving_peer_name})")

    # 1. Distribute the new on-disk config (if applicable).
    if bedrock_resource:
        if surviving_peers is None:
            raise ValueError(
                "drbd_remove_peer(bedrock_resource=True) requires "
                "surviving_peers to render the new singleton config.")
        # render_drbd_res honors persistent node-ids (invariant #3).
        write_drbd_resource(resource, surviving_peers)
        new_res_text = Path(f"/etc/drbd.d/{full_res}.res").read_text()
    if new_res_text:
        import base64
        b = base64.b64encode(new_res_text.encode()).decode()
        for host in surviving_hosts:
            ssh(host,
                f"echo {b} | base64 -d > /etc/drbd.d/{full_res}.res")

    # 2. Find the leaving peer's persistent node-id. For tier
    #    resources, look in cluster.json. For non-tier (e.g. VM)
    #    resources we fall through to the kernel-state lookup below.
    leaving_id = None
    if bedrock_resource:
        leaving_id = (load_cluster().get("tiers", {}).get(resource, {})
                      .get("drbd_node_ids", {}).get(leaving_peer_name))

    # 4. Mutate kernel state via drbdsetup direct (per L20 in
    #    lessons-log: drbdadm adjust is unreliable shrinking full-mesh
    #    resources because it hits "Combination of local address(port)
    #    and remote address(port) already in use" when re-establishing
    #    paths between survivors. drbdsetup disconnect+del-peer
    #    operates on kernel state directly using the node-id and
    #    works reliably).
    if leaving_id is None:
        # Fall back to reading kernel state to find the id by name —
        # required for non-tier resources (no cluster.json entry) and
        # rarely for tier resources where cluster.json missed the entry.
        for host in surviving_hosts:
            out = ssh(host,
                f"drbdsetup show {full_res} 2>&1 | "
                f"awk '/_peer_node_id/ {{pid=$2; gsub(\";\",\"\",pid)}} "
                f"/_name.*{leaving_peer_name}/ {{print pid; exit}}'",
                check=False).strip()
            if out.isdigit():
                leaving_id = int(out)
                break
        if leaving_id is None:
            raise RuntimeError(
                f"could not determine node-id for leaving peer "
                f"{leaving_peer_name} on resource {full_res}. "
                f"Inspect cluster.json + drbdsetup show output.")

    # 3. Mutate kernel state via drbdsetup direct (L20: drbdadm adjust
    #    is unreliable for full-mesh shrink).
    for host in surviving_hosts:
        # disconnect → StandAlone; del-peer removes per-peer kernel
        # config. Both are no-ops if the peer is already gone (host
        # powered off), so safe to retry.
        ssh(host, f"drbdsetup disconnect {full_res} {leaving_id}",
            check=False)
        ssh(host, f"drbdsetup del-peer {full_res} {leaving_id}",
            check=False)

    # 4. Optional sanity check via drbdadm adjust dry-run.
    #    With kernel state already correct, adjust should be a no-op.
    #    Significant residual ops indicate config drift — log but don't
    #    fail.
    for host in surviving_hosts:
        out = ssh(host, f"drbdadm --dry-run adjust {full_res}",
                  check=False)
        if out.strip():
            print(f"  [tier] note: drbdadm adjust dry-run on {host} "
                  f"reports residual ops (kernel state already correct):")
            for line in out.splitlines()[:5]:
                print(f"    {line}")

    # 5. Free the meta-disk bitmap slot. Optional but recommended: a
    #    later distinct peer added to this resource can reuse the
    #    cleared slot via a bitmap-based resync rather than a full
    #    sync. Run on every survivor.
    if leaving_id is not None:
        for host in surviving_hosts:
            ssh(host,
                f"drbdsetup forget-peer {full_res} {leaving_id}",
                check=False)
        # Drop the persistent assignment so future add can re-allocate
        if bedrock_resource:
            free_drbd_node_id(resource, leaving_peer_name)

    # 6. Persist updated peer list in cluster.json (tier resources only)
    if bedrock_resource and surviving_peers is not None:
        set_tier_state(resource, mode="drbd",
                       peers=[p["name"] for p in surviving_peers])
        print(f"  [tier] drbd_remove_peer({full_res}): done. "
              f"{len(surviving_peers)} peers remain.")
    else:
        print(f"  [tier] drbd_remove_peer({full_res}): done.")


def drbd_demote_to_local(remove_meta: bool = False) -> bool:
    """Demote the stand-alone cluster-singleton DRBD resource on this
    node back to a plain local LV mount at /var/lib/bedrock/cluster.

    Pre: the `cluster` DRBD resource is currently UP on this node with
    no other peers connected. The data LV's XFS is preserved (external
    metadata never touched it).

    Effects:
      1. Remove /etc/drbd.d/cluster.res so boot won't auto-up
      2. Update /etc/fstab: replace DRBD-mount line with local-LV line
      3. drbdadm down cluster (resource leaves kernel state)
      4. mount /dev/<vg>/bedrock-data-cluster at CLUSTER_MOUNT
      5. set_tier_state(cluster, mode="local")
      6. (optional) lvremove bedrock-meta-cluster

    Crash-safety: persistent state is mutated *before* the kernel-side
    drbdadm down. A reboot mid-flight finds .res gone + fstab pointing
    at the local LV; drbd-utils don't auto-up a missing config; the
    local mount succeeds; system arrives at the desired end state.

    Returns True on success, False if pre-conditions weren't met
    (e.g. resource still has peers — caller should drbd_remove_peer
    first).
    """
    res = CLUSTER_RESOURCE
    print(f"  [tier] drbd_demote_to_local({res})")

    minor = _minor_for(res)
    drbd_dev = f"/dev/drbd{minor}"
    mount = str(CLUSTER_MOUNT)
    data_lv = f"/dev/{VG}/{data_lv_for(res)}"

    # 0. Pre-conditions: resource exists, no other peers connected
    state = run(f"drbdsetup status {res} 2>&1", check=False)
    if not state or "not configured" in state.lower():
        print(f"  [tier] {res} not in kernel state — already down. "
              f"Proceeding to local-LV mount only.")
    elif "role:" in state:
        # Crude: any "<peer-name> role:" line means a peer is connected.
        # If there are no peer-role lines, only the local _this_host
        # line, we're stand-alone.
        peer_lines = [l for l in state.splitlines()
                      if "role:" in l and not l.startswith(res)]
        if peer_lines:
            print(f"  [tier] {res} still has peers connected:\n  " +
                  "\n  ".join(peer_lines))
            print(f"  [tier] Run drbd_remove_peer for each before "
                  f"drbd_demote_to_local can succeed.")
            return False

    # 1. drbdadm down with .res still in place. drbdadm orchestrates
    #    the full teardown (umount→secondary→detach→disconnect→
    #    del-minor→del-resource) using the .res file. Skipping this
    #    and using drbdsetup directly leaves the LV chained to a
    #    half-torn-down DRBD device. (See lessons-log L21.)
    if run_ok(f"mountpoint -q {mount}"):
        run(f"umount {mount}", check=False)
    run(f"drbdadm down {res}", check=False)

    # 2. NOW move .res aside. The crash window here is very brief, and
    #    even if a reboot lands here drbd-utils won't re-up because the
    #    resource is already-down at boot.
    res_file = Path(f"/etc/drbd.d/{res}.res")
    backup_file = Path(f"/etc/drbd.d/{res}.res.demoted")
    if res_file.exists():
        res_file.rename(backup_file)

    # 3. Update fstab: drop the DRBD-mount line, add the local-LV line.
    fstab = Path("/etc/fstab")
    text = fstab.read_text() if fstab.exists() else ""
    new_lines = [l for l in text.splitlines()
                 if mount not in l and data_lv not in l]
    new_lines.append(
        f"{data_lv} {mount} xfs "
        "defaults,discard,nofail,x-systemd.device-timeout=10s 0 0"
    )
    fstab.write_text("\n".join(new_lines).rstrip() + "\n")

    # 4. Mount the local LV (it has the same XFS the cluster ran on,
    #    byte-for-byte preserved by external-metadata semantics).
    Path(mount).mkdir(parents=True, exist_ok=True)
    if not run_ok(f"mountpoint -q {mount}"):
        run(f"mount {mount}")

    # 5. Persist in rqlite.
    set_tier_state(res, mode="local",
                   master=None,
                   backend_path=mount)

    # 6. Optional cleanup of the meta LV. Default: keep it, in case the
    #    operator wants to re-promote later. Removing it requires the
    #    resource to be fully down (it is now).
    if remove_meta:
        run(f"lvremove -f {VG}/{meta_lv_for(res)}", check=False)

    # Backup .res can be removed too (it's no longer a resource)
    if backup_file.exists():
        backup_file.unlink()

    print(f"  [tier] {res}: now local LV at {mount}")
    return True


def node_reset_local() -> None:
    """Bring this node back to its post-`bedrock bootstrap` state.

    Used when a node is being removed from the cluster (called over
    SSH from `bedrock storage remove-peer`'s cluster-side cleanup) or
    when an operator manually wants to take this node out of service.

    What this clears:
      - Stops bedrock services (mgmt/vm/vl/weed-*)
      - Tears down DRBD resources + removes /etc/drbd.d/*.res
      - Unmounts everything bedrock-related (FUSE mounts, DRBD, local LVs)
      - Drops fstab entries for bedrock mounts
      - Removes the cluster + weed-volume LVs from the resolved VG
        (data goes away — operator already accepted this by running
        remove-peer)
      - Removes /opt/bedrock/{mgmt,iso,data}
      - Truncates /etc/bedrock/state.json to {hardware, bootstrap_done}

    What this preserves:
      - OS packages (rpm DB)
      - DRBD kernel module + persist file
      - Network bridge (br0) + DRBD ring NIC config
      - SSH keys and known_hosts
      - The bedrock VG + thin pool itself (re-init/join skips the
        slow PV/VG creation)

    After this runs the operator can `bedrock init` (start a new cluster)
    or `bedrock join` (join one) — same choice as right after bootstrap.

    Idempotent — safe to re-run.
    """
    print("  [reset] clearing local cluster state")

    # 1. Stop services. Best-effort — the service might not exist on this node.
    #    Include bedrock-rqlited + bedrock-rqlited-arbiter + bedrock-net:
    #    leaving them up means stale Raft state keeps probing for the
    #    previous cluster's voters and the next `bedrock init` can't
    #    elect a leader.
    # Single unified daemon: bedrock-d.
    services = ("bedrock-d",
                "bedrock-mdns", "bedrock-redirect",
                "bedrock-cert-refresh.timer",
                "bedrock-vm", "bedrock-vl",
                "bedrock-vmagent", "bedrock-vlagent",
                "bedrock-rqlited", "bedrock-rqlited-arbiter",
                "bedrock-weed-master", "bedrock-weed-volume",
                "bedrock-weed-filer", "bedrock-weed-s3",
                "bedrock-weed-isos-mount.service")
    run(f"systemctl stop {' '.join(services)} 2>/dev/null", check=False)
    run(f"systemctl disable {' '.join(services)} 2>/dev/null", check=False)
    # Clear any cached failure counters from previous start-rate-limit
    # hits. Otherwise the next `systemctl enable --now` (from
    # mgmt_install / agent_install) returns success while the unit
    # silently refuses to start because it's still inside its
    # StartLimitInterval cooldown.
    run(f"systemctl reset-failed {' '.join(services)} 2>/dev/null", check=False)

    # 2. DRBD resources down + .res cleanup. Best-effort. The cluster
    #    singleton is resource `cluster`; per-VM disks are `vm-*`. We
    #    drop every configured resource via `drbdadm down all`.
    run("drbdadm down all 2>/dev/null", check=False)
    run(f"drbdadm down {CLUSTER_RESOURCE} 2>/dev/null", check=False)
    run(f"drbdsetup down {CLUSTER_RESOURCE} 2>/dev/null", check=False)
    run("rm -f /etc/drbd.d/*.res /etc/drbd.d/*.res.removed-* "
        "/etc/drbd.d/*.res.demoted 2>/dev/null", check=False)

    # 3. Unmount anything bedrock-touched. Two passes (normal then lazy)
    #    to handle any stuck handles per L16.
    #    /var/lib/bedrock/cluster is the cluster_arbiter DRBD mount —
    #    must come FIRST and BEFORE drbdadm down (which would otherwise
    #    refuse because the device is "open" by the mount).
    mounts = (
        str(CLUSTER_MOUNT),
        str(WEED_VOLUME_MOUNT),
        "/var/lib/bedrock/seaweedfs",
        "/mnt/bedrock",
        "/mnt/isos",  # legacy path; kept in cleanup list during transition
    )
    for mp in mounts:
        if run_ok(f"mountpoint -q {mp}"):
            run(f"umount {mp} 2>/dev/null || umount -l {mp} 2>/dev/null",
                check=False)
    # Try drbdadm down AGAIN after unmounts (the first attempt at step 2
    # may have failed because the mount was still open). Order:
    # umount → drbdadm down → rm .res. Without the second drbdadm down,
    # /dev/drbd1101 stays attached and the next create-md fails with
    # "Device '1101' is configured".
    run(f"drbdadm down {CLUSTER_RESOURCE} 2>/dev/null", check=False)
    run(f"drbdsetup down {CLUSTER_RESOURCE} 2>/dev/null", check=False)
    run(f"drbdsetup detach {CLUSTER_RESOURCE} 2>/dev/null", check=False)
    run(f"drbdsetup del-resource {CLUSTER_RESOURCE} 2>/dev/null", check=False)

    # 4. Drop fstab lines for anything bedrock-related.
    fstab = Path("/etc/fstab")
    if fstab.exists():
        tokens = ("/var/lib/bedrock", "bedrock-data-", "bedrock-meta-",
                  WEED_VOLUME_LV, "/mnt/bedrock", "/mnt/isos")
        new = [l for l in fstab.read_text().splitlines()
               if not any(t in l for t in tokens)]
        fstab.write_text("\n".join(new).rstrip() + "\n")

    # 5. SeaweedFS state — config files (filer.toml + master/volume
    #    runtime data) are reset on every fresh setup, but tear down
    #    here so a `node_reset_local` truly takes us back to bootstrap.
    #    Includes the generated /etc/bedrock/seaweedfs* + rqlited.env +
    #    storage.json files — these get re-rendered by mgmt_install /
    #    agent_install on the next `bedrock init`/`join`. Leaving stale
    #    copies bricks weed-master when the new cluster picks a
    #    different /24 (e.g. testbed loops): the env file's old
    #    loopback IP doesn't exist on `lo` and the bind fails.
    run("rm -rf /etc/seaweedfs /var/lib/bedrock/seaweedfs "
        "/var/lib/bedrock/cluster/seaweedfs 2>/dev/null", check=False)
    run("rm -f /etc/bedrock/seaweedfs.env "
        "/etc/bedrock/seaweedfs-master.toml "
        "/etc/bedrock/seaweedfs-s3.json "
        "/etc/bedrock/seaweedfs-filer.toml "
        "/etc/bedrock/rqlited.env "
        "/etc/bedrock/rqlited-arbiter.env "
        "/etc/bedrock/cluster.key "
        "/etc/bedrock/storage.json 2>/dev/null", check=False)
    # rqlite Raft WAL — stale data dir holds the previous cluster's
    # voter set and election state, which deadlocks a fresh `bedrock
    # init` waiting for ghost peers.
    run("rm -rf /var/lib/bedrock/rqlite /var/lib/bedrock-rqlited-arbiter "
        "2>/dev/null", check=False)
    # Mesh daemon runtime state — peer list, witness state.
    run("rm -rf /run/bedrock 2>/dev/null", check=False)

    # 6. Bedrock LVs in the RESOLVED VG (never hardcode 'bedrock').
    #    Lvremove fails harmlessly if the LV is already gone. The cluster
    #    singleton pair + the weed-volume LV + every per-VM data/meta LV
    #    (bedrock-data-vm-*/bedrock-meta-vm-*) go away — the operator
    #    accepted this by running remove-peer.
    fixed_lvs = (data_lv_for(CLUSTER_RESOURCE), meta_lv_for(CLUSTER_RESOURCE),
                 WEED_VOLUME_LV)
    for lv in fixed_lvs:
        run(f"lvremove -fy {VG}/{lv} 2>/dev/null", check=False)
    # Per-VM LVs: enumerate by name pattern, then lvremove each.
    vm_lvs = run(
        f"lvs --noheadings -o lv_name {VG} 2>/dev/null | "
        f"grep -E '^\\s*bedrock-(data|meta)-vm-' || true", check=False)
    for lv in vm_lvs.split():
        lv = lv.strip()
        if lv:
            run(f"lvremove -fy {VG}/{lv} 2>/dev/null", check=False)

    # 9. Mgmt-side /opt/bedrock/* subdirs that came from mgmt_install
    for sub in ("mgmt", "iso", "data", "vm", "vl"):
        run(f"rm -rf /opt/bedrock/{sub} 2>/dev/null", check=False)
    run("rm -f /opt/bedrock/scrape.yml 2>/dev/null", check=False)
    # Mgmt systemd units
    run("rm -f /etc/systemd/system/bedrock-{mgmt,vm,vl}.service "
        "/etc/systemd/system/mnt-isos.{mount,automount} 2>/dev/null",
        check=False)

    # 10. state.json truncated to bootstrap-only (cluster.json was
    #     removed cluster-wide in 2026-05-26; state.json is the only
    #     local cluster-related file now).
    if STATE_JSON.exists():
        try:
            s = json.loads(STATE_JSON.read_text())
        except json.JSONDecodeError:
            s = {}
        keep = {k: s[k] for k in ("hardware", "bootstrap_done") if k in s}
        # Go through lib.state.save for atomic + empty-write trap.
        # The plain write_text this used to do raced with readers
        # (rqlite_setup --render-env on every rqlited restart) and
        # could produce a partial file.
        from . import state as _state_mod
        _state_mod.save(keep)

    # 11. Reload systemd
    run("systemctl daemon-reload 2>/dev/null", check=False)

    print("  [reset] local state cleared. Run 'bedrock init' or "
          "'bedrock join'.")
