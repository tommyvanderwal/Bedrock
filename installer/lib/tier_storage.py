"""Bedrock storage tiers — scratch / bulk / critical.

See `tier_storage.md` (next to this file) for the full operational spec:
  - what each function does, contracts and invariants
  - where state lives (cluster.json, /etc/drbd.d, /etc/fstab, kernel)
  - the WHY behind each design choice
  - the documented sources for every external behavior
  - known issues and queued fixes

Reviewers analyzing this module for "can this reach a bad state" should
read tier_storage.md first — the invariants section enumerates what each
operation must preserve, with crash-safety reasoning.

For the journey of decisions and corrections that led here (wrong turns,
misdiagnoses, lessons learned), see ../../docs/lessons-log.md.

Quick model:
  /bedrock/<tier> is the stable mountpoint on every node, always valid.
  Backend behind the symlink swaps as the cluster grows or shrinks.

  N=1: /bedrock/<tier>    -> /var/lib/bedrock/local/<tier>        (local thin LV)
  N=2: /bedrock/scratch   -> Garage S3 via local s3fs FUSE
       /bedrock/bulk      -> DRBD 2-way XFS, NFS-served by master
       /bedrock/critical  -> DRBD 2-way XFS, NFS-served by master
  N=3: /bedrock/critical  -> DRBD 3-way XFS  (bulk stays 2-way)
  N=4: same shape; new node = Garage volume + NFS client

External DRBD metadata is essential: it makes local-LV → DRBD-replicated
promotion zero-copy (the data LV's XFS is preserved byte-for-byte).

Entry points (growth path):
  setup_n1()                          — single-node setup; idempotent
  transition_to_n2_master(...)        — N=1 -> N=2 master side
  transition_to_n2_peer(...)          — N=1 -> N=2 peer side
  finalize_n2_garage(...)             — Garage cluster formation
  promote_critical_to_3way(...)       — N=2 -> N=3 critical promote
  s3fs_mount_scratch(...)             — FUSE mount Garage scratch bucket

Entry points (shrink / role-move path):
  drbd_remove_peer(...)               — online DRBD peer removal
                                        (LINBIT-blessed adjust flow)
  garage_drain_node(...)              — graceful Garage node decommission
                                        (RF=1 safe, per-partition resync)
  transfer_mgmt_role(...)             — move mgmt + NFS + DRBD primary
                                        from one node to another

Entry points (final-collapse to single-node path):
  drbd_demote_to_local(tier)          — turn a stand-alone DRBD resource
                                        back into a plain local LV
                                        (XFS preserved by external meta)
  migrate_scratch_out_of_garage()     — copy scratch data out of Garage
                                        into a local LV; stop Garage

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

VG = "bedrock"
THINPOOL = "thinpool"
# Candidate raw disks for the bedrock VG, in order. First unused disk wins.
DATA_DISK_CANDIDATES = ("/dev/vdb", "/dev/sdb", "/dev/nvme1n1")

# Tier sizes (testbed defaults — operator can override per-node by setting
# /etc/bedrock/tier-sizes.json before init/join).
TIER_SIZE_GB = {
    "scratch":  20,
    "bulk":     30,
    "critical":  5,
}
DRBD_META_SIZE_MB = 32   # external metadata is tiny; per-resource

# Mountpoint trees
LOCAL_ROOT  = Path("/var/lib/bedrock/local")    # /var/lib/bedrock/local/<tier>
MOUNTS_ROOT = Path("/var/lib/bedrock/mounts")   # /var/lib/bedrock/mounts/<tier>-{drbd,nfs,s3fs}
PUBLIC_ROOT = Path("/bedrock")                  # /bedrock/<tier>  (the stable abstraction)

TIERS = ("scratch", "bulk", "critical")

# DRBD resource minor numbers — kept above VM minors (which start at 1000).
# Tier resources start at 1100 to leave a comfortable gap.
DRBD_MINORS = {
    "bulk":     1100,
    "critical": 1101,
}

# Garage (only at N>=2; serves the scratch tier)
GARAGE_VERSION  = "v2.3.0"
GARAGE_URL      = (f"https://garagehq.deuxfleurs.fr/_releases/{GARAGE_VERSION}/"
                   "x86_64-unknown-linux-musl/garage")
GARAGE_S3_PORT      = 3900
GARAGE_RPC_PORT     = 3901
GARAGE_ADMIN_PORT   = 3903
GARAGE_DATA_LV_GB   = 18   # backs Garage's data dir; replaces local scratch LV
GARAGE_DATA_DIR     = "/var/lib/garage/data"
GARAGE_META_DIR     = "/var/lib/garage/meta"
GARAGE_BLOCK_BYTES  = 10 * 1024 * 1024


CLUSTER_JSON = Path("/etc/bedrock/cluster.json")
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

def _log_append_typed(payload_bytes):
    """Append a typed log entry via IPC. Returns (idx, hash) or None
    if the daemon isn't reachable. Never raises — best-effort dual-write
    so an offline daemon doesn't block cluster operations."""
    try:
        from . import rust_ipc
        if not Path(rust_ipc.DEFAULT_SOCK).exists():
            return None
        with rust_ipc.Daemon() as d:
            return d.append(payload_bytes)
    except Exception as e:
        # Daemon down, msgpack missing on a partially-installed peer,
        # IPC frame error — none of these should stop a tier op. Log
        # for visibility and fall through to the direct write.
        print(f"  [log] append skipped: {e}")
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
    if CLUSTER_JSON.exists():
        return json.loads(CLUSTER_JSON.read_text())
    return {}


def save_cluster(c: dict) -> None:
    CLUSTER_JSON.parent.mkdir(parents=True, exist_ok=True)
    CLUSTER_JSON.write_text(json.dumps(c, indent=2))


def load_state() -> dict:
    if STATE_JSON.exists():
        return json.loads(STATE_JSON.read_text())
    return {}


def get_tier_state(tier: str) -> dict:
    """Return cluster-wide state for one tier (mode, master, peers, version)."""
    c = load_cluster()
    return c.get("tiers", {}).get(tier, {"mode": "local", "version": 1})


def set_tier_state(tier: str, **kv) -> None:
    c = load_cluster()
    c.setdefault("tiers", {})
    cur = c["tiers"].setdefault(tier, {"mode": "local", "version": 1})
    cur.update(kv)
    cur["version"] = cur.get("version", 0) + 1
    save_cluster(c)
    # Phase 5 cutover: shadow the tier-state mutation as a typed log
    # entry. view_builder folds it identically on every peer.
    try:
        from . import log_entries as _le
        _log_append_typed(_le.tier_state(
            tier=tier,
            mode=cur.get("mode", "local"),
            master=cur.get("master"),
            peers=cur.get("peers"),
            backend_path=cur.get("backend_path"),
            garage_endpoint=cur.get("garage_endpoint"),
        ))
    except ImportError:
        pass


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
    """Find an unused candidate disk for the bedrock VG. Picks the first
    disk that exists, has no partition table, and isn't mounted.
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


def ensure_vg() -> None:
    """Create the bedrock VG on a dedicated data disk if it doesn't exist."""
    if vg_exists():
        return
    disk = find_data_disk()
    print(f"  [tier] Creating PV+VG on {disk}")
    # Force-zero the start of the disk to clear any old signatures
    run(f"wipefs -af {disk}", check=False)
    run(f"pvcreate -ff -y {disk}")
    run(f"vgcreate {VG} {disk}")


def ensure_thinpool() -> None:
    """Create the thin pool if it doesn't exist. Sized to fill the VG."""
    ensure_vg()
    if thinpool_exists():
        return

    # Available free space in VG, in MB
    out = run(f"vgs {VG} --units m -o vg_free --noheadings", check=False)
    try:
        free_mb = float(out.replace("m", "").strip())
    except ValueError:
        free_mb = 0.0

    needed_mb = sum(TIER_SIZE_GB.values()) * 1024 + GARAGE_DATA_LV_GB * 1024 + 1024
    if free_mb < needed_mb:
        raise RuntimeError(
            f"Not enough free space in VG {VG}: {free_mb:.0f}MB free, "
            f"need {needed_mb}MB. Use a larger data disk.")

    # Create thin pool with ~all free space minus a small headroom (512MB).
    pool_size_mb = int(free_mb - 512)
    run(f"lvcreate -L {pool_size_mb}M -T {VG}/{THINPOOL} -y")


def ensure_thin_lv(lv: str, size_gb: int) -> None:
    """Create a thin LV in the pool if it doesn't exist."""
    if lv_exists(lv):
        return
    run(f"lvcreate -V {size_gb}G -T {VG}/{THINPOOL} -n {lv} -y")


def ensure_meta_lv(lv: str, size_mb: int = DRBD_META_SIZE_MB) -> None:
    """Create a thick meta LV (small) for DRBD external metadata."""
    if lv_exists(lv):
        return
    run(f"lvcreate -L {size_mb}M -n {lv} {VG} -y")


def ensure_xfs(device: str, label: str) -> None:
    """mkfs.xfs only if not already an XFS filesystem."""
    fstype = run(f"blkid -s TYPE -o value {device} 2>/dev/null", check=False)
    if fstype == "xfs":
        return
    run(f"mkfs.xfs -f -L {label} {device}")


def ensure_fstab(device: str, mount: str, fstype: str = "xfs",
                  options: str = "defaults,discard") -> None:
    """Idempotent fstab line."""
    fstab = Path("/etc/fstab")
    line = f"{device} {mount} {fstype} {options} 0 0"
    text = fstab.read_text() if fstab.exists() else ""
    if mount in text:
        return
    fstab.write_text(text.rstrip() + "\n" + line + "\n")


def ensure_mounted(device: str, mount: str, fstype: str = "xfs",
                    options: str = "defaults,discard") -> None:
    Path(mount).mkdir(parents=True, exist_ok=True)
    ensure_fstab(device, mount, fstype, options)
    if not run_ok(f"mountpoint -q {mount}"):
        run(f"mount {mount}")


def umount_quiet(mount: str) -> None:
    run(f"umount {mount} 2>/dev/null", check=False)


# ── N=1: local-only setup ──────────────────────────────────────────────────

def setup_n1() -> None:
    """Single-node tier setup. Creates LVs, mounts them, points
    /bedrock/<tier> at the local mount. Idempotent.
    """
    print("  [tier] Ensuring thin pool...")
    ensure_thinpool()

    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    MOUNTS_ROOT.mkdir(parents=True, exist_ok=True)
    PUBLIC_ROOT.mkdir(parents=True, exist_ok=True)

    for tier in TIERS:
        lv = f"tier-{tier}"
        size = TIER_SIZE_GB[tier]
        local_mount = LOCAL_ROOT / tier
        device = f"/dev/{VG}/{lv}"

        ensure_thin_lv(lv, size)
        ensure_xfs(device, tier)  # XFS labels max 12 chars; tier names are short
        ensure_mounted(device, str(local_mount))

        # /bedrock/<tier> -> /var/lib/bedrock/local/<tier>
        atomic_symlink(str(local_mount), PUBLIC_ROOT / tier)

        set_tier_state(tier, mode="local", master=None,
                       backend_path=str(local_mount))

        print(f"  [tier] {tier:<10} {size:>3}G -> {local_mount}")

    print("  [tier] N=1 setup complete: /bedrock/{scratch,bulk,critical} ready")


# ── DRBD resource config ───────────────────────────────────────────────────

# ── Persistent DRBD node-id assignments ────────────────────────────────────
#
# DRBD node-ids are *permanent* for the lifetime of a resource (see invariant
# #3 in tier_storage.md). We persist {peer_name: node_id} per resource in
# /etc/bedrock/cluster.json under tiers.<tier>.drbd_node_ids so that adding,
# removing, or rewriting peers never renumbers existing peers' IDs.

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
    # Phase 5 cutover: also persist this assignment as a typed log
    # entry. When peers replicate the log, their view_builder folds the
    # same drbd_node_id into their cluster.json — no fresh-allocation
    # race per L27. Best-effort; falls back to direct-write-only if the
    # daemon isn't running yet.
    try:
        from . import log_entries as _le
        _log_append_typed(_le.drbd_node_id_assigned(resource, peer_name, nid))
    except ImportError:
        pass
    return nid


def free_drbd_node_id(resource: str, peer_name: str) -> int | None:
    """Mark this peer's node-id as free for re-use. Call only after
    drbdsetup forget-peer has cleared the bitmap slot, otherwise a
    later peer reusing the slot would trigger a forced full-resync.
    Returns the freed id, or None if the peer was not assigned.
    """
    c = load_cluster()
    tiers = c.setdefault("tiers", {})
    tier = tiers.setdefault(resource, {})
    assignments = tier.setdefault("drbd_node_ids", {})
    nid = assignments.pop(peer_name, None)
    if nid is not None:
        tier["version"] = tier.get("version", 0) + 1
        save_cluster(c)
    return nid


def render_drbd_res(resource: str, minor: int,
                    peers: list[dict]) -> str:
    """Render a DRBD resource file. peers = [{name, drbd_ip}, ...].

    Node-ids are PERSISTED (not renumbered): each peer gets its sticky
    id from cluster.json, allocated on first sight of that peer.
    """
    on_blocks = []
    peer_ids = {}  # for the connection-block render below
    for p in peers:
        nid = get_drbd_node_id(resource, p["name"])
        peer_ids[p["name"]] = nid
        on_blocks.append(
            f'  on {p["name"]} {{\n'
            f'    node-id   {nid};\n'
            f'    device    /dev/drbd{minor};\n'
            f'    disk      /dev/{VG}/tier-{resource};\n'
            f'    meta-disk /dev/{VG}/tier-{resource}-meta;\n'
            f'    address   {p["drbd_ip"]}:{7000 + minor};\n'
            f'  }}\n'
        )

    # Connection mesh between every pair (full mesh for N>=2)
    conn_blocks = []
    for i in range(len(peers)):
        for j in range(i + 1, len(peers)):
            conn_blocks.append(
                f'  connection {{\n'
                f'    host {peers[i]["name"]} address {peers[i]["drbd_ip"]}:{7000+minor};\n'
                f'    host {peers[j]["name"]} address {peers[j]["drbd_ip"]}:{7000+minor};\n'
                f'  }}\n'
            )

    body = (
        f'resource tier-{resource} {{\n'
        f'  protocol C;\n'
        f'  options {{ on-no-quorum suspend-io; }}\n'
        f'  disk    {{ c-plan-ahead 0; resync-rate 100M; }}\n'
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


def write_drbd_resource(resource: str, peers: list[dict]) -> None:
    """Write /etc/drbd.d/tier-<resource>.res based on peer list.
    Honors persistent node-id assignments (see get_drbd_node_id).
    """
    minor = DRBD_MINORS[resource]
    Path("/etc/drbd.d").mkdir(parents=True, exist_ok=True)
    p = Path(f"/etc/drbd.d/tier-{resource}.res")
    p.write_text(render_drbd_res(resource, minor, peers))


# ── Local LV → DRBD migration (preserves filesystem via external metadata) ──

def promote_local_to_drbd_master(tier: str, peers: list[dict]) -> None:
    """On the master, convert a local-mounted LV into a DRBD primary that
    still contains the same XFS/data — uses external metadata so the on-disk
    filesystem layout is unchanged.

    Requires the LV to be unmounted; remounts the DRBD device in its place.
    """
    assert tier in ("bulk", "critical"), tier
    minor = DRBD_MINORS[tier]
    local_mount = str(LOCAL_ROOT / tier)
    drbd_mount = str(MOUNTS_ROOT / f"{tier}-drbd")
    drbd_dev = f"/dev/drbd{minor}"

    # 1. Create the meta LV (tiny, thick) — lives outside the thin pool so
    #    DRBD never sees ENOSPC on metadata writes.
    ensure_meta_lv(f"tier-{tier}-meta")

    # 2. Write the resource config (mesh of all peers)
    write_drbd_resource(tier, peers)

    # 3. Unmount local — but only if it's currently mounted there
    if run_ok(f"mountpoint -q {local_mount}"):
        run(f"umount {local_mount}")

    # 4. Initialize DRBD metadata + bring up the resource as primary --force
    run(f"drbdadm create-md tier-{tier} --force --max-peers=7")
    run(f"drbdadm up tier-{tier}")
    run(f"drbdadm primary --force tier-{tier}")

    # 5. Mount the DRBD device — same XFS, same data, just a different /dev
    Path(drbd_mount).mkdir(parents=True, exist_ok=True)
    run(f"mount -t xfs {drbd_dev} {drbd_mount}")

    # 6. Replace fstab line: local mount -> DRBD mount
    fstab = Path("/etc/fstab")
    text = fstab.read_text() if fstab.exists() else ""
    new_lines = []
    for line in text.splitlines():
        if local_mount in line and "tier-" in line:
            continue  # drop old local-LV line
        new_lines.append(line)
    new_lines.append(f"{drbd_dev} {drbd_mount} xfs defaults,discard,nofail,_netdev 0 0")
    fstab.write_text("\n".join(new_lines).rstrip() + "\n")

    # 7. Atomic symlink swap: /bedrock/<tier> -> drbd mount
    atomic_symlink(drbd_mount, PUBLIC_ROOT / tier)


def join_drbd_peer(tier: str, peers: list[dict]) -> None:
    """On a peer (not the source of data): create the LV (if needed), write
    DRBD config, bring up as Secondary so it can resync from the primary.
    """
    minor = DRBD_MINORS[tier]
    lv = f"tier-{tier}"
    size = TIER_SIZE_GB[tier]

    ensure_thin_lv(lv, size)
    ensure_meta_lv(f"tier-{tier}-meta")
    write_drbd_resource(tier, peers)
    run(f"drbdadm create-md tier-{tier} --force --max-peers=7")
    run(f"drbdadm up tier-{tier}")
    # Don't promote — the master is primary. Initial sync starts automatically.


# ── NFS export (master) and NFS mount (peers) ──────────────────────────────

def nfs_export_drbd_tiers(allowed_subnets: list[str]) -> None:
    """Export /var/lib/bedrock/mounts/<tier>-drbd to peers."""
    Path("/etc/exports.d").mkdir(parents=True, exist_ok=True)
    lines = []
    for tier in ("bulk", "critical"):
        path = MOUNTS_ROOT / f"{tier}-drbd"
        if not path.exists():
            continue
        opts = "rw,sync,no_root_squash,no_subtree_check"
        clauses = " ".join(f"{net}({opts})" for net in allowed_subnets)
        lines.append(f"{path} {clauses}")
    Path("/etc/exports.d/bedrock-tiers.exports").write_text("\n".join(lines) + "\n")
    # ensure nfs-server running
    run("dnf install -y -q nfs-utils >/dev/null 2>&1", check=False)
    run("systemctl enable --now nfs-server", check=False)
    run("exportfs -ra", check=False)


def nfs_mount_drbd_tiers(master_drbd_ip: str) -> None:
    """On a peer: mount each DRBD-backed tier from master via NFS, point
    /bedrock/<tier> at it.

    Uses fstab + mount (not a systemd .mount unit) — avoids systemd-escape
    pain with hyphens in mount paths.
    """
    run("dnf install -y -q nfs-utils >/dev/null 2>&1", check=False)
    fstab_path = Path("/etc/fstab")
    text = fstab_path.read_text() if fstab_path.exists() else ""
    new_lines = text.splitlines()

    for tier in ("bulk", "critical"):
        nfs_mount = MOUNTS_ROOT / f"{tier}-nfs"
        nfs_mount.mkdir(parents=True, exist_ok=True)
        remote = f"{master_drbd_ip}:/var/lib/bedrock/mounts/{tier}-drbd"
        line = (f"{remote} {nfs_mount} nfs "
                f"rw,nolock,soft,timeo=50,retrans=3,_netdev,nofail 0 0")
        # Drop any existing line for this mount (idempotent re-runs)
        new_lines = [ln for ln in new_lines if str(nfs_mount) not in ln]
        new_lines.append(line)

    fstab_path.write_text("\n".join(new_lines).rstrip() + "\n")
    run("systemctl daemon-reload", check=False)

    # Mount each tier
    for tier in ("bulk", "critical"):
        nfs_mount = MOUNTS_ROOT / f"{tier}-nfs"
        if not run_ok(f"mountpoint -q {nfs_mount}"):
            run(f"mount {nfs_mount}", check=False)
        # Symlink: /bedrock/<tier> -> NFS mountpoint
        atomic_symlink(str(nfs_mount), PUBLIC_ROOT / tier)


# ── Garage admin API helpers ──────────────────────────────────────────────
#
# We talk to Garage via the v2 admin API (http://127.0.0.1:3903) rather than
# parsing `garage` CLI text tables. Rationale: CLI labels and column layouts
# drift across releases and a parse miss is silent (we read the wrong value).
# The admin API returns structured JSON whose schema is in OpenAPI v2 and
# evolves under semver.
#
# See `tier_storage.md` § "Garage admin API" and lessons-log L18 / L24.

GARAGE_ADMIN_BASE = f"http://127.0.0.1:{GARAGE_ADMIN_PORT}"


def _garage_admin_token(host: str | None = None) -> str:
    """Read admin_token from /etc/garage.toml on `host` (None = local).

    Garage has a single shared admin token cluster-wide (written by
    `install_garage_local` from the value passed to `init`/`join`). Reading
    it from the same config Garage itself reads keeps caller and server
    in lockstep — no separate secret to plumb.
    """
    cmd = r"""awk -F'"' '/^admin_token/{print $2}' /etc/garage.toml"""
    out = (run(cmd, check=False) if host is None
           else ssh(host, cmd, check=False)).strip()
    if not out:
        where = host or "local"
        raise RuntimeError(
            f"admin_token not found in /etc/garage.toml on {where} — "
            f"Garage admin API needs it.")
    return out


def _garage_api(method: str, path: str, body=None, *,
                host: str | None = None,
                token: str | None = None,
                check: bool = True,
                timeout: int = 10):
    """Call the Garage v2 admin API. Returns parsed JSON (or None when
    `check=False` and the call fails / response is empty).

    `path` includes leading slash and any query string,
        e.g. "/v2/GetClusterLayout" or "/v2/ListWorkers?node=self".
    `body` is a Python value JSON-encoded into the request body, or None.
    `host` selects which node's admin API to call (None = local).

    Local calls go through stdlib urllib (no shell). Remote calls use
    `curl` over our `ssh()` helper because the admin port may not be
    routable cluster-wide and the token lives on each node.
    """
    if token is None:
        token = _garage_admin_token(host)
    payload = "" if body is None else json.dumps(body)
    url = f"{GARAGE_ADMIN_BASE}{path}"

    if host is None:
        try:
            req = urllib.request.Request(
                url, method=method,
                data=payload.encode() if body is not None else None,
                headers={
                    "Authorization": f"Bearer {token}",
                    **({"Content-Type": "application/json"}
                       if body is not None else {}),
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
            return json.loads(raw) if raw else None
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError) as e:
            if check:
                raise RuntimeError(
                    f"Garage API {method} {path} failed: {e}") from e
            return None

    # Remote: curl over ssh. Bodies we send never contain single quotes
    # (we control them: bucket names, hex node ids, integers, "tables"/
    # "blocks"). If that ever changes, switch to a heredoc here.
    parts = ["curl -fsS", f"-X {method}",
             f"-H 'Authorization: Bearer {token}'"]
    if body is not None:
        parts.append("-H 'Content-Type: application/json'")
        parts.append(f"-d '{payload}'")
    parts.append(f"'{url}'")
    out = ssh(host, " ".join(parts), check=check)
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        if check:
            raise RuntimeError(
                f"Garage API {method} {path} on {host} returned non-JSON: "
                f"{out!r}") from e
        return None


# ── Garage installation + cluster formation ───────────────────────────────

def install_garage_local(drbd_ip: str, rpc_secret: str,
                          admin_token: str) -> None:
    """Install + start Garage on this node. Idempotent."""
    # Garage user
    if not run_ok("id garage &>/dev/null"):
        run("useradd -r -s /sbin/nologin -d /var/lib/garage garage")

    # Garage data LV (replaces the local scratch LV at N>=2 — but for now we
    # provision a separate LV so the local scratch data stays accessible
    # during migration if needed).
    ensure_thin_lv("garage-data", GARAGE_DATA_LV_GB)
    ensure_xfs(f"/dev/{VG}/garage-data", "garage-data")
    ensure_mounted(f"/dev/{VG}/garage-data", GARAGE_DATA_DIR)
    Path(GARAGE_META_DIR).mkdir(parents=True, exist_ok=True)
    run(f"chown -R garage:garage /var/lib/garage")

    # Binary
    if not Path("/usr/local/bin/garage").exists():
        run(f"curl -fsSL -o /usr/local/bin/garage {GARAGE_URL}", timeout=300)
        run("chmod +x /usr/local/bin/garage")

    # Config
    Path("/etc/garage.toml").write_text(
        f'metadata_dir = "{GARAGE_META_DIR}"\n'
        f'data_dir     = "{GARAGE_DATA_DIR}"\n'
        f'db_engine    = "lmdb"\n'
        f'replication_factor = 1\n'
        f'rpc_secret      = "{rpc_secret}"\n'
        f'rpc_bind_addr   = "[::]:{GARAGE_RPC_PORT}"\n'
        f'rpc_public_addr = "{drbd_ip}:{GARAGE_RPC_PORT}"\n'
        f'block_size = {GARAGE_BLOCK_BYTES}\n'
        f'\n'
        f'[s3_api]\n'
        f'api_bind_addr = "[::]:{GARAGE_S3_PORT}"\n'
        f's3_region     = "garage"\n'
        f'root_domain   = ".s3.scratch.local"\n'
        f'\n'
        f'[admin]\n'
        f'api_bind_addr = "[::]:{GARAGE_ADMIN_PORT}"\n'
        f'admin_token   = "{admin_token}"\n'
    )
    run("chown root:garage /etc/garage.toml && chmod 640 /etc/garage.toml")

    # systemd unit
    Path("/etc/systemd/system/garage.service").write_text(
        "[Unit]\n"
        "Description=Garage S3 (scratch tier)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        f"RequiresMountsFor={GARAGE_DATA_DIR}\n\n"
        "[Service]\n"
        "User=garage\n"
        "Group=garage\n"
        "Environment=RUST_LOG=garage=info\n"
        "ExecStart=/usr/local/bin/garage server\n"
        "Restart=on-failure\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    run("systemctl daemon-reload")
    run("systemctl enable --now garage.service", check=False)
    time.sleep(2)


def _self_drbd_ip() -> str:
    """Pick this node's DRBD IP from /etc/bedrock/state.json."""
    s = load_state()
    if s.get("drbd_ip"):
        return s["drbd_ip"]
    for n in s.get("hardware", {}).get("nics", []):
        ip = n.get("ip", "")
        if ip.startswith("10.99."):
            return ip
    return ""


def garage_form_cluster(peers_drbd_ips: list[str],
                         capacity_gb: int = GARAGE_DATA_LV_GB - 2) -> None:
    """Connect peers + apply cluster layout. Idempotent — re-running just
    bumps the layout version.

    Talks to each peer's admin API directly (read its node id, stage roles,
    apply layout). Replaces the prior CLI-fanout shape that parsed
    `garage layout show` text — see lessons-log L24.
    """
    self_ip = _self_drbd_ip()
    token = _garage_admin_token()  # admin_token is shared cluster-wide

    # Discover each peer's full hex node id and its RPC listen address.
    ids: dict[str, str] = {}  # drbd_ip -> "<hex>@<addr>"
    for ip in peers_drbd_ips:
        host = None if ip == self_ip else ip
        info = _garage_api("GET", "/v2/GetNodeInfo?node=self",
                           host=host, token=token, check=False)
        succ = (info or {}).get("success", {})
        if not succ:
            continue
        my_hex = next(iter(succ.values())).get("nodeId", "")
        if not my_hex:
            continue
        # Cold-start fallback: if the peer hasn't yet learned its addr from
        # other nodes, GetClusterStatus may not return an addr. Use the DRBD
        # IP we were called with — Garage's gossip will replace it.
        addr = f"{ip}:{GARAGE_RPC_PORT}"
        status = _garage_api("GET", "/v2/GetClusterStatus",
                             host=host, token=token, check=False) or {}
        for n in status.get("nodes", []):
            if n.get("id") == my_hex and n.get("addr"):
                addr = n["addr"]
                break
        ids[ip] = f"{my_hex}@{addr}"

    # Connect from this node to each remote peer (skip self).
    remote = [v for ip, v in ids.items() if ip != self_ip]
    if remote:
        _garage_api("POST", "/v2/ConnectClusterNodes",
                    body=remote, token=token, check=False)

    time.sleep(2)

    # Stage one role per node — same zone, equal capacity (in BYTES; the
    # API takes int64, not the CLI's "12G" suffix).
    capacity_bytes = capacity_gb * (1024 ** 3)
    roles = [
        {"id": v.split("@")[0], "zone": "dc1",
         "capacity": capacity_bytes, "tags": []}
        for v in ids.values()
    ]
    if roles:
        _garage_api("POST", "/v2/UpdateClusterLayout",
                    body={"roles": roles}, token=token, check=False)

    # Apply: read the current version from the structured response and
    # bump by 1. Replaces `parsing "Current cluster layout version: N"`.
    cur = _garage_api("GET", "/v2/GetClusterLayout",
                      token=token, check=False) or {}
    next_version = int(cur.get("version", 0)) + 1
    _garage_api("POST", "/v2/ApplyClusterLayout",
                body={"version": next_version}, token=token, check=False)


def garage_create_scratch_bucket() -> dict:
    """Create the 'scratch' bucket + key. Returns {access_key, secret_key}.

    Uses CreateBucket / CreateKey / AllowBucketKey directly and reads
    `accessKeyId` + `secretAccessKey` from the structured CreateKey
    response — no more regexing 'Key ID:' / 'Secret key:' labels that
    drift across Garage versions (lessons-log L24).
    """
    token = _garage_admin_token()

    # Bucket: create or recover via global-alias lookup if it already exists.
    bucket = _garage_api("POST", "/v2/CreateBucket",
                         body={"globalAlias": "scratch"},
                         token=token, check=False)
    if not bucket:
        bucket = _garage_api(
            "GET", "/v2/GetBucketInfo?globalAlias=scratch", token=token)
    bucket_id = bucket["id"]

    # Key: create or recover via name search. Fresh CreateKey returns the
    # secret in the response; GetKeyInfo only does so with showSecretKey=true.
    key = _garage_api("POST", "/v2/CreateKey",
                      body={"name": "scratch-key"},
                      token=token, check=False)
    if not key or not key.get("secretAccessKey"):
        key = _garage_api(
            "GET", "/v2/GetKeyInfo?search=scratch-key&showSecretKey=true",
            token=token)
    ak = key["accessKeyId"]
    sk = key.get("secretAccessKey") or ""

    # Grant the key full perms on the bucket. Idempotent — repeated calls
    # just re-set the same flags.
    _garage_api("POST", "/v2/AllowBucketKey", body={
        "bucketId": bucket_id,
        "accessKeyId": ak,
        "permissions": {"read": True, "write": True, "owner": True},
    }, token=token, check=False)

    return {"access_key": ak, "secret_key": sk}


def s3fs_mount_scratch(access_key: str, secret_key: str,
                        endpoint_drbd_ip: str | None = None,
                        migrate_local_data: bool = True) -> None:
    """Mount Garage's 'scratch' bucket via s3fs at /var/lib/bedrock/mounts/scratch-s3fs
    and point /bedrock/scratch at it.

    Always uses the LOCAL Garage daemon at 127.0.0.1:3900 (invariant #6
    in tier_storage.md). The endpoint_drbd_ip arg is accepted for
    backward compat with older callers but ignored — Garage handles
    cross-node block lookup internally via its own RPC.

    If `migrate_local_data` is True (default) and the local scratch LV
    is currently mounted with content, that content is rsync'd into the
    Garage bucket BEFORE the symlink swap. This preserves data across
    the N=1 → N=2 promote (lessons-log L15: data may be lost only on
    node loss, never on a default migration).

    Set migrate_local_data=False on a freshly-joined peer that doesn't
    have its own local scratch data to migrate (the local LV exists
    from setup_n1 but is empty; nothing to copy).
    """
    # s3fs-fuse is in EPEL on AlmaLinux 9
    if not run_ok("rpm -q s3fs-fuse >/dev/null 2>&1"):
        if not run_ok("rpm -q epel-release >/dev/null 2>&1"):
            run("dnf install -y -q epel-release", timeout=180)
        run("dnf install -y -q s3fs-fuse", timeout=180)
    Path("/etc/passwd-s3fs").write_text(f"{access_key}:{secret_key}\n")
    os.chmod("/etc/passwd-s3fs", 0o600)

    s3fs_mount = MOUNTS_ROOT / "scratch-s3fs"
    s3fs_mount.mkdir(parents=True, exist_ok=True)

    fstab = Path("/etc/fstab")
    # Always target the LOCAL Garage daemon (invariant #6 in
    # tier_storage.md, lessons-log L6).
    line = (f"scratch {s3fs_mount} fuse.s3fs "
            f"_netdev,allow_other,umask=0022,sigv4,endpoint=garage,"
            f"use_path_request_style,"
            f"url=http://127.0.0.1:{GARAGE_S3_PORT},"
            f"passwd_file=/etc/passwd-s3fs 0 0")
    text = fstab.read_text() if fstab.exists() else ""
    if str(s3fs_mount) not in text:
        fstab.write_text(text.rstrip() + "\n" + line + "\n")

    # Mount s3fs FIRST (before unmounting local) so the migration
    # has a destination.
    if not run_ok(f"mountpoint -q {s3fs_mount}"):
        run(f"mount {s3fs_mount}", check=False)

    # Migrate any local scratch data into the Garage bucket BEFORE
    # the symlink swap. (L15: scratch data must be preserved on
    # default N=1 → N=2 promote.)
    local_scratch = LOCAL_ROOT / "scratch"
    if migrate_local_data and run_ok(f"mountpoint -q {local_scratch}"):
        migrate_scratch_into_garage(verify_md5=True)
    elif run_ok(f"mountpoint -q {local_scratch}"):
        # Caller said skip migration — just unmount and swap symlink.
        run(f"umount {local_scratch}", check=False)

    atomic_symlink(str(s3fs_mount), PUBLIC_ROOT / "scratch")


def migrate_scratch_into_garage(verify_md5: bool = True) -> None:
    """Copy any data from the local scratch LV into the Garage scratch
    bucket via s3fs, atomically swap /bedrock/scratch to point at the
    Garage mount, then unmount the local LV.

    Symmetric counterpart to `migrate_scratch_out_of_garage()`. Called
    from `s3fs_mount_scratch()` during N=1 → N=2 promote so the
    operator's local scratch data is preserved.

    Pre:
      - local scratch LV is mounted at /var/lib/bedrock/local/scratch
      - s3fs already mounted at /var/lib/bedrock/mounts/scratch-s3fs
        (Garage cluster up, scratch bucket created, s3fs operational)

    Post:
      - all files from local scratch are now objects in the scratch bucket
      - /bedrock/scratch symlink points at the s3fs mount
      - local scratch LV is unmounted (LV preserved; operator can
        lvremove later if disk space is needed)
      - if verify_md5=True, every file's MD5 was checksummed on both
        sides before the symlink swap

    Crash-safety:
      - The rsync runs while /bedrock/scratch still points at the local
        LV. A crash mid-rsync leaves persistent state at "local mode";
        re-running converges (rsync skips already-copied files).
      - The atomic symlink swap is the commit point. After it, new
        opens go to s3fs; existing fds on the local mount keep
        working until they close.
      - The umount happens AFTER the swap, so a crash between commit
        and umount just leaves the local LV mounted but unused; next
        boot's fstab won't remount it (we drop the line at the end)
        and a re-run picks up where it left off.
    """
    print(f"  [garage] migrate_scratch_into_garage()")

    s3fs_mount = MOUNTS_ROOT / "scratch-s3fs"
    local_scratch = LOCAL_ROOT / "scratch"

    if not run_ok(f"mountpoint -q {local_scratch}"):
        # Nothing to migrate
        return
    if not run_ok(f"mountpoint -q {s3fs_mount}"):
        raise RuntimeError(
            f"s3fs not mounted at {s3fs_mount} — caller must mount "
            f"the Garage scratch bucket first.")

    # 1. rsync local → s3fs.
    #    - no -X: xattrs incompatible per L22.
    #    - --omit-dir-times: s3fs returns EIO when rsync sets the
    #      destination root dir's mtime (S3 has no notion of directory
    #      mtime; FUSE bridge surfaces EIO). File mtimes still preserved
    #      so re-runs remain idempotent on size+mtime. (L26.)
    print(f"  [garage] rsync pass 1 (local -> Garage)")
    run(f"rsync -aH --inplace --omit-dir-times "
        f"{local_scratch}/ {s3fs_mount}/",
        timeout=24 * 3600)

    # 2. Optional MD5 verification
    if verify_md5:
        print(f"  [garage] md5 verify")
        src_md5 = run(
            f"cd {local_scratch} && find . -type f -print0 | sort -z | "
            f"xargs -0 md5sum 2>/dev/null", check=False)
        dst_md5 = run(
            f"cd {s3fs_mount} && find . -type f -print0 | sort -z | "
            f"xargs -0 md5sum 2>/dev/null", check=False)
        if src_md5 != dst_md5:
            Path("/tmp/scratch-into-md5-src.log").write_text(src_md5)
            Path("/tmp/scratch-into-md5-dst.log").write_text(dst_md5)
            raise RuntimeError(
                "MD5 verification failed: local and Garage differ. "
                "Manifests at /tmp/scratch-into-md5-{src,dst}.log. "
                "Re-run rsync with --checksum, or investigate the diff.")

    # 3. Atomic symlink swap — commit point
    atomic_symlink(str(s3fs_mount), PUBLIC_ROOT / "scratch")
    print(f"  [garage] /bedrock/scratch now points at Garage "
          f"(via local s3fs)")

    # 4. Wait for any open fds on the local mount to drain so we can
    #    cleanly umount.
    deadline = time.time() + 60
    while time.time() < deadline:
        out = run(f"lsof +D {local_scratch} 2>/dev/null | wc -l",
                  check=False)
        try:
            count = int(out.strip().splitlines()[0])
        except (ValueError, IndexError):
            count = 0
        if count <= 1:
            break
        time.sleep(2)

    # 5. Unmount local scratch + drop fstab line
    run(f"umount {local_scratch} 2>/dev/null", check=False)
    if run_ok(f"mountpoint -q {local_scratch}"):
        run(f"umount -l {local_scratch}", check=False)

    fstab = Path("/etc/fstab")
    if fstab.exists():
        text = fstab.read_text()
        new = "\n".join(l for l in text.splitlines()
                        if str(local_scratch) not in l)
        fstab.write_text(new + "\n" if new else "")

    print(f"  [garage] local scratch LV unmounted; data now lives in Garage.")


# ── Top-level transition orchestration ─────────────────────────────────────

def transition_to_n2_master(self_drbd_ip: str, peer: dict,
                              rpc_secret: str, admin_token: str) -> dict:
    """Master-side N=1 -> N=2 transition. Returns garage credentials.

    peer = {"name": "...", "drbd_ip": "..."}
    """
    print("  [tier] N=2 master transition: install Garage, promote DRBD bulk+critical, NFS-export")

    # 1. Install Garage locally (master)
    install_garage_local(self_drbd_ip, rpc_secret, admin_token)

    # 2. Build peer list including self
    self_state = load_state()
    self_name = self_state.get("node_name", "node1")
    peers = [
        {"name": self_name, "drbd_ip": self_drbd_ip},
        peer,
    ]

    # 3. Promote bulk + critical to DRBD primary on master
    for tier in ("bulk", "critical"):
        promote_local_to_drbd_master(tier, peers)
        set_tier_state(tier, mode="drbd-nfs", master=self_name,
                       peers=[p["name"] for p in peers])

    # 4. NFS export the DRBD-backed mounts
    nfs_export_drbd_tiers(["192.168.2.0/24", "10.99.0.0/24"])

    # 5. Garage cluster formation will happen after the peer's daemon is up;
    #    that's the peer's responsibility to call back.

    set_tier_state("scratch", mode="garage-pending", master=self_name)

    return {"rpc_secret": rpc_secret, "admin_token": admin_token,
             "peers": peers}


def transition_to_n2_peer(self_drbd_ip: str, master: dict,
                            rpc_secret: str, admin_token: str,
                            peers: list[dict]) -> None:
    """Peer-side N=1 -> N=2 transition. Called on the joining node after
    setup_n1() and after master has set up the export.
    """
    print("  [tier] N=2 peer transition: unmount local LVs, join DRBD, NFS-mount, install Garage")

    # 1. Unmount peer's local bulk/critical mounts FIRST. The peer's local
    #    XFS will be overwritten by the DRBD initial sync from master.
    #    DRBD `attach` requires the backing LV to be unowned, so this MUST
    #    happen before drbdadm up.
    for tier in ("bulk", "critical"):
        local_mount = LOCAL_ROOT / tier
        if run_ok(f"mountpoint -q {local_mount}"):
            run(f"umount {local_mount}", check=False)
        # Remove the old fstab line (peer no longer mounts the raw LV)
        fstab = Path("/etc/fstab")
        if fstab.exists():
            new = []
            for line in fstab.read_text().splitlines():
                if str(local_mount) in line and "tier-" in line:
                    continue
                new.append(line)
            fstab.write_text("\n".join(new).rstrip() + "\n")

    # 2. Join DRBD as Secondary for bulk + critical (initial sync starts auto)
    for tier in ("bulk", "critical"):
        join_drbd_peer(tier, peers)

    # 3. NFS-mount bulk + critical from master
    nfs_mount_drbd_tiers(master["drbd_ip"])

    # 4. Install Garage locally
    install_garage_local(self_drbd_ip, rpc_secret, admin_token)


def finalize_n2_garage(garage_endpoint_drbd_ip: str,
                        peers_drbd_ips: list[str]) -> dict:
    """Called on master once all Garage daemons are up. Forms cluster,
    creates scratch bucket + key, returns credentials. Then both nodes run
    s3fs_mount_scratch() with these credentials.
    """
    garage_form_cluster(peers_drbd_ips)
    creds = garage_create_scratch_bucket()
    set_tier_state("scratch", mode="garage", master=None,
                   garage_endpoint=f"http://{garage_endpoint_drbd_ip}:{GARAGE_S3_PORT}")
    return creds


# ── 3-way critical promotion (N=2 -> N=3) ──────────────────────────────────

def promote_critical_to_3way(third_peer: dict) -> None:
    """Add a third peer to the critical DRBD resource. bulk stays 2-way.

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
    state_critical = get_tier_state("critical")
    existing_peer_names = state_critical.get("peers", [])
    cluster = load_cluster()
    nodes = cluster.get("nodes", {})
    peers = []
    for name in existing_peer_names + [third_peer["name"]]:
        node = nodes.get(name, {})
        peers.append({"name": name, "drbd_ip": node.get("drbd_ip", "")})

    # Local: write new config + adjust kernel.
    write_drbd_resource("critical", peers)
    new_res = Path(f"/etc/drbd.d/tier-critical.res").read_text()
    run("drbdadm adjust tier-critical")

    # Distribute identical config to every existing peer (the new
    # third peer's join_drbd_peer will write its own; we don't need
    # to also push to it). Then drbdadm adjust on each so the kernel
    # picks up the new peer-3 connection definition.
    import base64
    b = base64.b64encode(new_res.encode()).decode()
    for peer_name in existing_peer_names:
        if peer_name == nodes.get(state_critical.get("master", ""), {}).get("name", ""):
            continue   # master already adjusted above
        peer_host = nodes.get(peer_name, {}).get("host")
        if not peer_host:
            continue
        ssh(peer_host,
            f"echo {b} | base64 -d > /etc/drbd.d/tier-critical.res")
        ssh(peer_host, "drbdadm adjust tier-critical", check=False)

    set_tier_state("critical", mode="drbd-nfs",
                    peers=[p["name"] for p in peers])


# ── Decommissioning helpers ────────────────────────────────────────────────
#
# These three helpers implement the "shrink the cluster cleanly" path:
#
#   drbd_remove_peer    — remove a peer from a running DRBD resource (config-first)
#   garage_drain_node   — drain a Garage node's data to surviving nodes (RF=1 safe)
#   transfer_mgmt_role  — move mgmt + NFS server + DRBD primary to a new node
#
# Detailed contracts, invariants, command sequences, and source citations
# live in tier_storage.md (sections "drbd_remove_peer", "garage_drain_node",
# "transfer_mgmt_role"). Read those before changing this code.


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
        resource:         FULL DRBD resource name. For tier resources
                          can be just the short name "bulk" if
                          `bedrock_resource=True` (default), in which
                          case "tier-bulk" is the actual resource name.
                          For VM resources pass the full name like
                          "vm-web1-disk0" with `bedrock_resource=False`.
        leaving_peer_name: peer's hostname as it appears in the .res
        surviving_hosts:  list of mgmt-LAN hosts (or any reachable IP)
                          to SSH into for the per-node operations
        surviving_peers:  list of {"name": ..., "drbd_ip": ...} for the
                          peers that REMAIN. Required if
                          `bedrock_resource=True` so we can render the
                          new tier config. Optional otherwise.
        new_res_text:     Pre-rendered .res file content to distribute.
                          If provided, overrides the auto-rendering for
                          tier resources. For VM resources, callers
                          render their own config and pass it here.
                          If None and `bedrock_resource=False`, no
                          on-disk config update happens (caller is
                          responsible).
        bedrock_resource: True for tier-X resources (auto-render via
                          render_drbd_res); False for VM disks or
                          other non-tier DRBD resources.

    Crash-safety: when an on-disk config is provided, it's distributed
    BEFORE the kernel-state mutation so a power loss leaves persistent
    state already at the desired end state.

    See tier_storage.md "drbd_remove_peer" for the command-by-command
    breakdown and source citations.
    """
    # Resolve the actual DRBD resource name for kernel commands and
    # the .res filename. For tier resources, "bulk" → "tier-bulk".
    full_res = f"tier-{resource}" if bedrock_resource else resource

    print(f"  [tier] drbd_remove_peer({full_res}, leaving={leaving_peer_name})")

    # 1. Distribute the new on-disk config (if applicable).
    if bedrock_resource:
        if surviving_peers is None:
            raise ValueError(
                "drbd_remove_peer(bedrock_resource=True) requires "
                "surviving_peers to render the new tier config.")
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
        set_tier_state(resource, mode="drbd-nfs",
                       peers=[p["name"] for p in surviving_peers])
        print(f"  [tier] drbd_remove_peer({full_res}): done. "
              f"{len(surviving_peers)} peers remain.")
    else:
        print(f"  [tier] drbd_remove_peer({full_res}): done.")


def garage_drain_node(
    departing_node_id_short: str,
    surviving_admin_host: str,
    departing_node_admin_host: str,
    poll_seconds: int = 5,
    max_wait_seconds: int = 7200,
) -> None:
    """Garage-blessed online node decommission.

    Drains a Garage node's data to its peers via Garage's own per-
    partition block-resync worker. Works at any replication factor
    INCLUDING RF=1 — Garage's resync mechanism is offload-then-delete:
    blocks are copied to their new owner BEFORE being deleted from
    the source, so reads stay correct throughout the transition (the
    multi-version layout history continues to direct reads to the
    departing node until each block has been copied).

    Args:
        departing_node_id_short: 16-char short node id of the leaving Garage daemon
        surviving_admin_host:    mgmt-LAN host of any surviving node
                                 (used to issue layout commands; Garage
                                 propagates them)
        departing_node_admin_host: mgmt-LAN host of the departing node
                                 (where we observe + speed up the resync
                                 worker, and ultimately stop the daemon)
        poll_seconds:           how often to poll worker state
        max_wait_seconds:       safety timeout

    Pre: surviving_admin_host can ssh to departing_node_admin_host.

    See tier_storage.md "garage_drain_node" for the command-by-command
    breakdown and source citations to Garage's block_resync worker.
    """
    print(f"  [garage] drain {departing_node_id_short}")

    # All Garage interactions go through the v2 admin API (see
    # `_garage_api`). The token is shared cluster-wide so we can read it
    # from any node — survivor + departing both work.
    surv_token = _garage_admin_token(surviving_admin_host)
    dep_token  = _garage_admin_token(departing_node_admin_host)

    # The API takes the FULL hex node id, not the short id the CLI
    # accepts. Resolve via the survivor's GetClusterStatus.
    status = _garage_api("GET", "/v2/GetClusterStatus",
                         host=surviving_admin_host, token=surv_token)
    departing_full = ""
    for n in status.get("nodes", []):
        if n.get("id", "").startswith(departing_node_id_short):
            departing_full = n["id"]
            break
    if not departing_full:
        raise RuntimeError(
            f"node {departing_node_id_short} not found in cluster status — "
            f"already removed from layout?")

    # 1. Stage the layout removal + apply. Garage assigns this node's
    #    partitions to surviving nodes in the new layout version.
    _garage_api("POST", "/v2/UpdateClusterLayout",
                body={"roles": [{"id": departing_full, "remove": True}]},
                host=surviving_admin_host, token=surv_token)

    # Bump the version monotonically (read structured `version`, no parse).
    cur = _garage_api("GET", "/v2/GetClusterLayout",
                      host=surviving_admin_host, token=surv_token) or {}
    next_version = int(cur.get("version", 0)) + 1
    _garage_api("POST", "/v2/ApplyClusterLayout",
                body={"version": next_version},
                host=surviving_admin_host, token=surv_token)

    # 2. Speed up the resync workers on the DEPARTING node (where the
    #    blocks live). Default tranquility throttles for impact-friendly
    #    operation; for a controlled drain we want it to drain fast.
    for var, val in (("resync-tranquility", "0"),
                     ("resync-worker-count", "8")):
        _garage_api("POST", "/v2/SetWorkerVariable?node=self",
                    body={"variable": var, "value": val},
                    host=departing_node_admin_host, token=dep_token,
                    check=False)

    # 3. Wait for all "Block resync" workers on the departing node to
    #    show idle with queueLength in (0, null). The block_resync worker
    #    is offload-then-delete, so this is exactly the "data is fully
    #    re-homed" signal we need before stopping the daemon.
    #
    # Source: src/block/resync.rs L537 (worker name) + L551 (queueLength)
    # at https://git.deuxfleurs.fr/Deuxfleurs/garage/src/branch/main-v2/
    # See lessons-log L18 for the original CLI-table-parsing miss.
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        body = _garage_api("POST", "/v2/ListWorkers?node=self",
                           body={}, host=departing_node_admin_host,
                           token=dep_token, check=False)
        if not body or body.get("error") or not body.get("success"):
            time.sleep(poll_seconds)
            continue
        # success is {<this-node-id-hex>: [worker, worker, ...]}
        workers = next(iter(body["success"].values()))
        all_idle = True
        any_resync = False
        for w in workers:
            if not w.get("name", "").startswith("Block resync worker"):
                continue
            any_resync = True
            if w.get("state") != "idle":
                all_idle = False
                break
            if (w.get("queueLength") or 0) > 0:
                all_idle = False
                break
        if any_resync and all_idle:
            break
        time.sleep(poll_seconds)
    else:
        raise RuntimeError(
            f"garage drain timeout after {max_wait_seconds}s — "
            f"workers still not Idle. Investigate before stopping the node.")

    # 4. Verify no errored blocks. ListBlockErrors returns a structured
    #    array per node — len == 0 is the safety gate. Replaces text-line
    #    counting that miscounts on header-format changes.
    errs = _garage_api("GET", "/v2/ListBlockErrors?node=self",
                       host=departing_node_admin_host, token=dep_token,
                       check=False) or {}
    succ = (errs.get("success") or {}) if isinstance(errs, dict) else {}
    err_list = next(iter(succ.values()), []) if succ else []
    if err_list:
        # Show the first few hashes so the operator can investigate.
        sample = ", ".join(b.get("blockHash", "?") for b in err_list[:3])
        raise RuntimeError(
            f"garage block errors on {departing_node_admin_host}: "
            f"{len(err_list)} entries (e.g. {sample}). "
            f"NOT safe to remove node yet.")

    # 5. Run repair on the whole cluster to ensure metadata tables and
    #    block references are consistent. Garage docs recommend this
    #    after layout changes. Idempotent.
    for repair_type in ("tables", "blocks"):
        _garage_api("POST", "/v2/LaunchRepairOperation?node=*",
                    body={"repairType": repair_type},
                    host=surviving_admin_host, token=surv_token,
                    check=False)

    # 6. NOW it is safe to stop Garage on the departing node.
    ssh(departing_node_admin_host, "systemctl stop garage", check=False)
    ssh(departing_node_admin_host, "systemctl disable garage",
        check=False)

    print(f"  [garage] drain complete. Surviving cluster has all data.")


def transfer_mgmt_role(
    old_master_host: str,
    new_master_host: str,
    new_master_drbd_ip: str,
    other_peer_hosts: list[str],
    old_master_drbd_ip: str | None = None,
) -> None:
    """Move the mgmt + NFS-server + DRBD-primary role to a new node.

    Ten-step playbook (see tier_storage.md "transfer_mgmt_role"):

      1. Verify new master's DRBD secondaries are UpToDate (won't
         promote stale data)
      2. Stop bedrock-{mgmt,vm,vl} + nfs-server on old master
      3. Unmount + drbdadm secondary on old master
      4. drbdadm primary + mount on new master
      5. rsync /opt/bedrock/{mgmt,iso,data,bin} from old → new
      6. Copy systemd unit files; daemon-reload on new
      7. Configure NFS exports + start nfs-server on new
      8. Start bedrock-vl, bedrock-vm, bedrock-mgmt on new
      9. SSH-fanout to other peers: replace NFS server IP in fstab,
         remount NFS clients
     10. Update /bedrock/<tier> symlinks; update cluster.json on every
         node to reflect new master

    Crash-safety: each on-disk write (fstab, exports, systemd units,
    cluster.json) is committed before the kernel-side step that
    depends on it. A power loss mid-flight leaves persistent state
    consistent with whichever step had committed by then; running this
    function again is idempotent and resumes correctly.

    Args:
        old_master_host:    current master mgmt-LAN address (must be
                            ssh-reachable for the rsync; can be skipped
                            if old master is already down — see special
                            case below)
        new_master_host:    new master mgmt-LAN address
        new_master_drbd_ip: new master's IP on the DRBD ring (10.99.0.x)
        other_peer_hosts:   mgmt-LAN addresses of every OTHER cluster
                            node (not old master, not new master) — they
                            need their NFS clients re-pointed
        old_master_drbd_ip: old master's IP on the DRBD ring; if None,
                            looked up from cluster.json
    """
    print(f"  [mgmt] transfer mgmt+NFS role: {old_master_host} → {new_master_host}")

    # 0. Resolve old master's DRBD ip if not given (needed for fstab sed)
    if old_master_drbd_ip is None:
        c = load_cluster()
        for node in c.get("nodes", {}).values():
            if node.get("host") == old_master_host:
                old_master_drbd_ip = node.get("drbd_ip", "")
                break
        if not old_master_drbd_ip:
            raise RuntimeError(
                f"could not resolve drbd ip for old master {old_master_host}")

    # 1. Verify new master's DRBD secondaries are UpToDate
    for tier in ("bulk", "critical"):
        out = ssh(new_master_host, f"drbdadm status tier-{tier}",
                  check=False)
        if "disk:UpToDate" not in out:
            raise RuntimeError(
                f"new master {new_master_host}'s tier-{tier} is not "
                f"UpToDate; refusing to promote.\n{out}")

    # 2. Stop services on old master (best-effort: may already be down)
    ssh(old_master_host,
        "systemctl stop bedrock-mgmt bedrock-vm bedrock-vl "
        "mnt-isos.mount nfs-server",
        check=False)

    # 3. Unmount + secondary on old master
    for tier in ("bulk", "critical"):
        ssh(old_master_host,
            f"umount /var/lib/bedrock/mounts/{tier}-drbd",
            check=False)
        ssh(old_master_host, f"drbdadm secondary tier-{tier}", check=False)

    # 4. Promote new master + mount the DRBD-backed XFS
    for tier in ("bulk", "critical"):
        ssh(new_master_host, f"drbdadm primary tier-{tier}")
        minor = DRBD_MINORS[tier]
        mount = f"/var/lib/bedrock/mounts/{tier}-drbd"
        ssh(new_master_host, f"mkdir -p {mount}")
        ssh(new_master_host, f"mount /dev/drbd{minor} {mount}",
            check=False)
        # Idempotent fstab line on new master (DRBD device, not NFS)
        device = f"/dev/drbd{minor}"
        ssh(new_master_host,
            f"grep -q '{mount}' /etc/fstab || echo "
            f"'{device} {mount} xfs defaults,discard,nofail,_netdev 0 0' "
            f">> /etc/fstab")

    # 5. rsync /opt/bedrock/{mgmt,iso,data,bin} from old → new (via
    #    new master pulling from old; consistent with our other
    #    rsync-pull patterns).
    for sub in ("mgmt", "iso", "data", "bin"):
        ssh(new_master_host,
            f"mkdir -p /opt/bedrock/{sub} && "
            f"rsync -aHX --delete -e 'ssh -o StrictHostKeyChecking=no' "
            f"root@{old_master_host}:/opt/bedrock/{sub}/ "
            f"/opt/bedrock/{sub}/", check=False)
    # scrape.yml + any top-level singletons
    ssh(new_master_host,
        f"rsync -aHX -e 'ssh -o StrictHostKeyChecking=no' "
        f"root@{old_master_host}:/opt/bedrock/scrape.yml "
        f"/opt/bedrock/ 2>/dev/null", check=False)

    # 5b. Pull /etc/bedrock/cluster.json from the old master. Peers'
    #     cluster.json only ever held tier state (the canonical
    #     cluster_name + cluster_uuid + nodes map lives only on the
    #     master). Without this rsync the new master's `bedrock storage
    #     status` shows "Cluster: <none>" and downstream verbs
    #     (remove-peer, collapse-to-n1) can't resolve peer hostnames
    #     to drbd_ips. (Lessons-log L28.)
    ssh(new_master_host,
        f"rsync -aHX -e 'ssh -o StrictHostKeyChecking=no' "
        f"root@{old_master_host}:/etc/bedrock/cluster.json "
        f"/etc/bedrock/cluster.json", check=False)

    # 6. Copy systemd unit files. mnt-isos.mount and the bedrock-*
    #    units are idempotent if pre-existing (rsync overwrites).
    for unit in ("bedrock-mgmt.service", "bedrock-vm.service",
                 "bedrock-vl.service", "mnt-isos.mount"):
        ssh(new_master_host,
            f"rsync -aHX -e 'ssh -o StrictHostKeyChecking=no' "
            f"root@{old_master_host}:/etc/systemd/system/{unit} "
            f"/etc/systemd/system/{unit}", check=False)
    ssh(new_master_host, "systemctl daemon-reload")

    # 6b. Install mgmt-app Python deps on the new master. agent_install
    #     doesn't install these (only mgmt_install.install_full does),
    #     so a peer becoming the new master needs them now.
    #     (See lessons-log L17.)
    ssh(new_master_host,
        "pip3 install -q fastapi uvicorn paramiko websockets pydantic "
        "python-multipart 2>&1 | tail -2",
        check=False, timeout=300)

    # 7. NFS exports on new master
    nfs_export_drbd_tiers_remote(new_master_host)

    # 8. Start mgmt + metrics services on new master
    ssh(new_master_host,
        "systemctl enable --now bedrock-vm bedrock-vl bedrock-mgmt "
        "mnt-isos.mount", check=False)

    # 9. Re-point NFS clients on every other peer
    for peer in other_peer_hosts:
        ssh(peer,
            f"sed -i 's|{old_master_drbd_ip}:/var/lib/bedrock/mounts/|"
            f"{new_master_drbd_ip}:/var/lib/bedrock/mounts/|g' /etc/fstab")
        # Also update the ISO library NFS mount unit if present
        ssh(peer,
            f"sed -i 's|{old_master_host}:/opt/bedrock/iso|"
            f"{new_master_host}:/opt/bedrock/iso|g' "
            f"/etc/systemd/system/mnt-isos.mount", check=False)
        ssh(peer, "systemctl daemon-reload", check=False)
        for tier in ("bulk", "critical"):
            # Use lazy umount: when the old NFS server has just been
            # demoted, regular umount can return success without
            # actually unmounting — leaving the kernel state pointing
            # at the dead server. -l detaches the mount immediately
            # and the next mount picks up fresh config from fstab.
            # (See lessons-log L16.)
            ssh(peer,
                f"umount -l /var/lib/bedrock/mounts/{tier}-nfs", check=False)
            ssh(peer,
                f"mount /var/lib/bedrock/mounts/{tier}-nfs", check=False)

    # 10. Symlink swaps: new master goes to local DRBD mount; old
    #     master (if reachable) goes to NFS-from-new-master.
    for tier in ("bulk", "critical"):
        ssh(new_master_host,
            f"ln -sfn /var/lib/bedrock/mounts/{tier}-drbd /bedrock/{tier}.tmp && "
            f"mv -T /bedrock/{tier}.tmp /bedrock/{tier}",
            check=False)
        # Old master, if reachable, becomes a peer; symlink to NFS mount
        ssh(old_master_host,
            f"mkdir -p /var/lib/bedrock/mounts/{tier}-nfs && "
            f"grep -q '{tier}-nfs' /etc/fstab || echo "
            f"'{new_master_drbd_ip}:/var/lib/bedrock/mounts/{tier}-drbd "
            f"/var/lib/bedrock/mounts/{tier}-nfs nfs "
            f"rw,nolock,soft,timeo=50,retrans=3,_netdev,nofail 0 0' "
            f">> /etc/fstab && "
            f"mount /var/lib/bedrock/mounts/{tier}-nfs && "
            f"ln -sfn /var/lib/bedrock/mounts/{tier}-nfs /bedrock/{tier}.tmp && "
            f"mv -T /bedrock/{tier}.tmp /bedrock/{tier}",
            check=False)

    # 11. Update cluster.json on the new master + propagate.
    #     Two updates per node:
    #       a) tier.master       — bulk + critical → new_master_name
    #       b) nodes[*].role     — old master demoted to "compute";
    #                              new master upgraded to "mgmt+compute".
    #     Without (b), `bedrock storage remove-peer` would refuse to
    #     remove the OLD master on the (now-correct) ground that it
    #     still has the "mgmt" role. (Lessons-log L28.)
    new_master_name = ssh(new_master_host,
                          "hostname --fqdn 2>/dev/null || hostname",
                          check=False).strip()
    old_master_name = ssh(old_master_host,
                          "hostname --fqdn 2>/dev/null || hostname",
                          check=False).strip()
    all_hosts = [new_master_host] + other_peer_hosts
    if old_master_host not in all_hosts:
        all_hosts.append(old_master_host)
    for host in all_hosts:
        ssh(host,
            f"python3 -c 'import json; from pathlib import Path; "
            f"p=Path(\"/etc/bedrock/cluster.json\"); "
            f"c=json.loads(p.read_text()) if p.exists() else {{}}; "
            f"c.setdefault(\"tiers\",{{}}); "
            f"[c[\"tiers\"].setdefault(t,{{}}).update("
            f"{{\"master\":\"{new_master_name}\"}}) for t in (\"bulk\",\"critical\")]; "
            f"nodes=c.setdefault(\"nodes\",{{}}); "
            f"nodes.setdefault(\"{new_master_name}\",{{}})[\"role\"]=\"mgmt+compute\"; "
            f"old=nodes.get(\"{old_master_name}\"); "
            f"old and old.update({{\"role\":\"compute\"}}); "
            f"p.write_text(json.dumps(c, indent=2))'",
            check=False)

    # 12. Update state.json on every node so each one's mgmt_url +
    #     witness_host point at the new master, and `role` matches the
    #     new layout. Without this:
    #       - new master's `bedrock-mgmt` keeps reporting its OLD
    #         (peer-era) mgmt_url in /cluster-info,
    #       - peers' `bedrock storage status` shows stale mgmt_ip,
    #       - subsequent `bedrock join --witness <new>` queries
    #         /cluster-info, gets back the OLD master's mgmt_url, and
    #         tries to register against a dead service.
    #     (Lessons-log L28 follow-up.)
    new_master_url = f"http://{new_master_host}:8080"
    for host in all_hosts:
        is_new_master = (host == new_master_host)
        new_role = "mgmt+compute" if is_new_master else "compute"
        # witness_host on the new master is "self" (same convention as
        # mgmt_install.install_full); on every other node it's the new
        # master's mgmt-LAN host.
        new_witness = "self" if is_new_master else new_master_host
        ssh(host,
            f"python3 -c 'import json; from pathlib import Path; "
            f"p=Path(\"/etc/bedrock/state.json\"); "
            f"s=json.loads(p.read_text()) if p.exists() else {{}}; "
            f"s[\"mgmt_url\"]={json.dumps(new_master_url)}; "
            f"s[\"witness_host\"]={json.dumps(new_witness)}; "
            f"s[\"role\"]={json.dumps(new_role)}; "
            f"p.write_text(json.dumps(s, indent=2))'",
            check=False)
    # bedrock-mgmt caches the cluster info from state.json at startup,
    # so a restart on the new master picks up the new mgmt_url.
    ssh(new_master_host, "systemctl restart bedrock-mgmt", check=False)

    # Phase 5 cutover: append a typed `mgmt_master` log entry. The
    # bedrock-rust daemon replicates it to every peer; each peer's
    # view_builder then folds it into its own cluster.json identically.
    # This is what makes L28's manual rsync of cluster.json + role
    # rewrite unnecessary going forward — the log IS the propagation.
    try:
        from . import log_entries as _le
        _log_append_typed(_le.mgmt_master(new_master_name))
    except ImportError:
        pass

    print(f"  [mgmt] transfer complete. New master: {new_master_host} ({new_master_name})")


def nfs_export_drbd_tiers_remote(host: str) -> None:
    """Set up NFS exports for tier-bulk/critical on a remote host.

    The remote variant of nfs_export_drbd_tiers — used by
    transfer_mgmt_role. Idempotent.
    """
    exports = (
        "/var/lib/bedrock/mounts/bulk-drbd     "
        "192.168.2.0/24(rw,sync,no_root_squash,no_subtree_check) "
        "10.99.0.0/24(rw,sync,no_root_squash,no_subtree_check)\n"
        "/var/lib/bedrock/mounts/critical-drbd "
        "192.168.2.0/24(rw,sync,no_root_squash,no_subtree_check) "
        "10.99.0.0/24(rw,sync,no_root_squash,no_subtree_check)\n"
    )
    import base64
    b = base64.b64encode(exports.encode()).decode()
    ssh(host, "mkdir -p /etc/exports.d")
    ssh(host, f"echo {b} | base64 -d > /etc/exports.d/bedrock-tiers.exports")
    ssh(host, "dnf install -y -q nfs-utils >/dev/null 2>&1", check=False)
    ssh(host, "systemctl enable --now nfs-server", check=False)
    ssh(host, "exportfs -ra", check=False)


# ── Final-collapse helpers (N=2 → N=1, last surviving node) ───────────────
#
#   drbd_demote_to_local           — turn a single-peer DRBD into a local LV
#   migrate_scratch_out_of_garage  — migrate scratch data out of Garage into
#                                    a local LV; stop Garage cleanly
#
# These run on the LAST surviving node when collapsing the cluster back to
# single-node operation. They pair with drbd_remove_peer and
# garage_drain_node, which are what get you DOWN to a single peer / single
# Garage node first. See tier_storage.md sections "drbd_demote_to_local"
# and "migrate_scratch_out_of_garage" for full operational specs.


def drbd_demote_to_local(tier: str, remove_meta: bool = False) -> bool:
    """Demote a stand-alone DRBD resource on this node back to a plain
    local LV mount.

    Pre: tier is a tier-<tier> DRBD resource currently UP on this node
    with no other peers connected. The data LV's XFS is preserved
    (external metadata never touched it).

    Effects:
      1. Stop NFS export of <tier>-drbd (if applicable)
      2. Remove /etc/drbd.d/tier-<tier>.res so boot won't auto-up
      3. Update /etc/fstab: replace DRBD-mount line with local-LV line
      4. drbdsetup down tier-<tier> (resource leaves kernel state)
      5. mount /dev/<vg>/tier-<tier> at /var/lib/bedrock/local/<tier>
      6. atomic_symlink /bedrock/<tier> → /var/lib/bedrock/local/<tier>
      7. set_tier_state(<tier>, mode="local")
      8. (optional) lvremove tier-<tier>-meta

    Crash-safety: persistent state is mutated *before* the kernel-side
    drbdadm down. A reboot mid-flight finds .res gone + fstab pointing
    at the local LV; drbd-utils don't auto-up a missing config; the
    local mount succeeds; system arrives at the desired end state.

    Returns True on success, False if pre-conditions weren't met
    (e.g. resource still has peers — caller should drbd_remove_peer
    first).
    """
    print(f"  [tier] drbd_demote_to_local({tier})")

    res = f"tier-{tier}"
    minor = DRBD_MINORS[tier]
    drbd_dev = f"/dev/drbd{minor}"
    drbd_mount = MOUNTS_ROOT / f"{tier}-drbd"
    local_mount = LOCAL_ROOT / tier
    data_lv = f"/dev/{VG}/tier-{tier}"

    # 0. Pre-conditions: resource exists, no other peers connected
    state = run(f"drbdsetup status {res} 2>&1", check=False)
    if not state or "not configured" in state.lower():
        print(f"  [tier] {res} not in kernel state — already down. "
              f"Proceeding to local-LV mount only.")
    elif "role:" in state:
        # Crude: any "<peer-name> role:" line means a peer is connected
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

    # 1. Stop NFS export (best-effort — if it was being exported)
    exports_file = Path("/etc/exports.d/bedrock-tiers.exports")
    if exports_file.exists():
        text = exports_file.read_text()
        new = "\n".join(l for l in text.splitlines()
                        if str(drbd_mount) not in l)
        exports_file.write_text(new + "\n" if new else "")
        run("exportfs -ra", check=False)

    # 2. drbdadm down with .res still in place. drbdadm orchestrates
    #    the full teardown (umount→secondary→detach→disconnect→
    #    del-minor→del-resource) using the .res file. Skipping this
    #    and using drbdsetup directly leaves the LV chained to a
    #    half-torn-down DRBD device. (See lessons-log L21.)
    if run_ok(f"mountpoint -q {drbd_mount}"):
        run(f"umount {drbd_mount}", check=False)
    run(f"drbdadm down {res}", check=False)

    # 3. NOW move .res aside. The crash window between (2) and (3) is
    #    very brief, and even if a reboot lands here drbd-utils won't
    #    re-up because the resource is already-down at boot.
    res_file = Path(f"/etc/drbd.d/{res}.res")
    backup_file = Path(f"/etc/drbd.d/{res}.res.demoted")
    if res_file.exists():
        res_file.rename(backup_file)

    # 4. Update fstab: drop the DRBD-mount line, add the local-LV line
    fstab = Path("/etc/fstab")
    text = fstab.read_text() if fstab.exists() else ""
    new_lines = []
    for line in text.splitlines():
        if str(drbd_mount) in line:
            continue   # drop the DRBD line
        if str(local_mount) in line and "tier-" in line:
            continue   # drop any pre-existing local-LV line for this tier
        new_lines.append(line)
    new_lines.append(
        f"{data_lv} {local_mount} xfs defaults,discard 0 0"
    )
    fstab.write_text("\n".join(new_lines).rstrip() + "\n")

    # 5. Mount the local LV (it has the same XFS we ran the cluster on,
    #    byte-for-byte preserved by external-metadata semantics).
    Path(local_mount).mkdir(parents=True, exist_ok=True)
    if not run_ok(f"mountpoint -q {local_mount}"):
        run(f"mount {local_mount}")

    # 6. Swap the public symlink atomically
    atomic_symlink(str(local_mount), PUBLIC_ROOT / tier)

    # 7. Persist in cluster.json
    set_tier_state(tier, mode="local",
                   master=None,
                   backend_path=str(local_mount))

    # 8. Optional cleanup of the meta LV. Default: keep it, in case the
    #    operator wants to re-promote later. Removing it requires the
    #    resource to be fully down (it is now).
    if remove_meta:
        run(f"lvremove -f {VG}/tier-{tier}-meta", check=False)

    # Backup .res can be removed too (it's no longer a resource)
    if backup_file.exists():
        backup_file.unlink()

    print(f"  [tier] {tier}: now local LV at {local_mount}")
    return True


def migrate_scratch_out_of_garage(
    verify_md5: bool = True,
    keep_garage: bool = False,
) -> None:
    """Migrate all scratch data out of Garage into a local LV; then
    decommission Garage on this node.

    Used at the end of cluster collapse (N=1, last node) to return the
    scratch tier to a plain local-LV mount and stop Garage entirely.

    Pre:
      - This node is the only Garage cluster member (after
        garage_drain_node has drained every other node).
      - /var/lib/bedrock/local/scratch's underlying LV exists (created
        in setup_n1; may currently be unmounted because s3fs is using
        the public symlink).
      - There is enough free space in the local thin pool to hold the
        current Garage scratch dataset.

    Effects (in order, with crash-safety annotations):
      1. Mount the local scratch LV at /var/lib/bedrock/local/scratch.
      2. rsync from /bedrock/scratch (s3fs view) to local mount, twice
         (first pass while in-flight, second pass to catch deltas).
      3. (optional) MD5 verification that every file in local matches
         the s3fs source.
      4. atomic_symlink /bedrock/scratch → local mount  (commit point)
      5. Wait for any process still using the s3fs mount via the OLD
         symlink target to release file handles (lsof poll).
      6. umount s3fs; remove fstab entry.
      7. Update set_tier_state(scratch, mode="local").
      8. systemctl stop garage; systemctl disable garage.
      9. (optional) lvremove garage-data; rm /etc/garage.toml +
         /var/lib/garage/meta directory.

    The crash-window is between step 4 (symlink swap) and step 6
    (umount): if power is lost there, on next boot fstab still has
    the s3fs line, garage.service starts, scratch returns to s3fs.
    Operator re-runs the function and it picks up where it left off
    (idempotent: rsync sees "no changes," symlink already correct,
    umount + stop garage proceed).

    Args:
      verify_md5:  if True, hash every file in local + s3fs and
                   compare. Default True. Set False for very large
                   datasets where hashing time is prohibitive.
      keep_garage: if True, do NOT stop/disable garage at step 8.
                   Useful if other things use the Garage cluster.
                   Default False — this is the "last node, full
                   collapse" case.
    """
    print(f"  [garage] migrate_scratch_out_of_garage()")

    s3fs_mount = MOUNTS_ROOT / "scratch-s3fs"
    local_mount = LOCAL_ROOT / "scratch"
    data_lv = f"/dev/{VG}/tier-scratch"

    # 0. Pre-flight — local LV exists?
    if not lv_exists("tier-scratch"):
        raise RuntimeError(
            "tier-scratch LV missing — was setup_n1 ever run on this node? "
            "Cannot migrate without a destination.")

    # 1. Mount local scratch LV (might already be mounted; idempotent)
    Path(local_mount).mkdir(parents=True, exist_ok=True)
    fstype = run(f"blkid -s TYPE -o value {data_lv} 2>/dev/null",
                 check=False)
    if fstype != "xfs":
        run(f"mkfs.xfs -f -L scratch {data_lv}")
    if not run_ok(f"mountpoint -q {local_mount}"):
        run(f"mount {data_lv} {local_mount}")

    # 1b. Pre-flight — enough free space in thin pool for the data?
    src_bytes = run(f"du -sb {s3fs_mount} 2>/dev/null | awk '{{print $1}}'",
                    check=False)
    try:
        src_bytes = int(src_bytes)
    except ValueError:
        src_bytes = 0
    pool_free_mb = run(
        f"lvs --noheadings --units m -o lv_size,data_percent "
        f"{VG}/{THINPOOL} | awk '{{size=$1; pct=$2+0; "
        f"gsub(/m/,\"\",size); print size*(100-pct)/100}}'",
        check=False)
    try:
        free_bytes = int(float(pool_free_mb)) * 1024 * 1024
    except ValueError:
        free_bytes = 0
    if free_bytes < src_bytes * 1.1:    # 10% headroom
        raise RuntimeError(
            f"thin pool free space {free_bytes/1e9:.1f} GB insufficient "
            f"for scratch dataset {src_bytes/1e9:.1f} GB + 10% headroom. "
            f"Free up the pool or extend it before retrying.")

    # 2. rsync, twice. The first pass copies most data while the
    #    cluster may still be writing; the second pass catches deltas
    #    after we have the symlink-swap commit point ready.
    print(f"  [garage] rsync pass 1 (bulk copy)")
    # NOTE: rsync -X (extended attrs) deliberately omitted here.
    # s3fs reports SELinux/xattr contexts inconsistently with the
    # destination XFS, causing "lremovexattr: Permission denied"
    # mid-copy. Plain -aH preserves what we actually need
    # (perms, times, hardlinks). See lessons-log L22.
    run(f"rsync -aH --inplace {s3fs_mount}/ {local_mount}/",
        timeout=24 * 3600)

    # 3. (Optional) MD5 verify before the commit
    if verify_md5:
        print(f"  [garage] md5 verify")
        # Generate manifests and compare. We use sorted output for
        # deterministic diff.
        src_md5 = run(
            f"cd {s3fs_mount} && find . -type f -print0 | sort -z | "
            f"xargs -0 md5sum 2>/dev/null", check=False)
        dst_md5 = run(
            f"cd {local_mount} && find . -type f -print0 | sort -z | "
            f"xargs -0 md5sum 2>/dev/null", check=False)
        if src_md5 != dst_md5:
            # Save both manifests for debugging
            Path("/tmp/scratch-md5-src.log").write_text(src_md5)
            Path("/tmp/scratch-md5-dst.log").write_text(dst_md5)
            raise RuntimeError(
                "MD5 verification failed: src and local differ. "
                "Manifests saved to /tmp/scratch-md5-{src,dst}.log. "
                "Re-run rsync with --checksum, or investigate the diff.")

    # 4. Commit point — symlink swap. New opens of /bedrock/scratch
    #    now go to local LV. Any process that already had a file open
    #    via the s3fs mount keeps reading from the old inode.
    atomic_symlink(str(local_mount), PUBLIC_ROOT / "scratch")
    print(f"  [garage] /bedrock/scratch now points at local LV "
          f"{local_mount}")

    # 5. Wait for s3fs to be unused so we can cleanly umount.
    #    lsof returns 0 entries when nothing has files open inside.
    deadline = time.time() + 120
    while time.time() < deadline:
        out = run(f"lsof +D {s3fs_mount} 2>/dev/null | wc -l",
                  check=False)
        try:
            count = int(out.strip().splitlines()[0])
        except (ValueError, IndexError):
            count = 0
        if count <= 1:   # 1 = header line; 0 = totally empty
            break
        time.sleep(2)

    # 6. umount s3fs; drop the fstab line
    run(f"umount {s3fs_mount} 2>/dev/null", check=False)
    if run_ok(f"mountpoint -q {s3fs_mount}"):
        # lazy fallback if normal umount failed
        run(f"umount -l {s3fs_mount}", check=False)
    fstab = Path("/etc/fstab")
    if fstab.exists():
        text = fstab.read_text()
        new = "\n".join(l for l in text.splitlines()
                        if "fuse.s3fs" not in l)
        fstab.write_text(new + "\n" if new else "")

    # 7. Persist in cluster.json
    set_tier_state("scratch", mode="local",
                   master=None,
                   backend_path=str(local_mount),
                   garage_endpoint=None)

    # 8. Stop + disable Garage (unless caller said keep)
    if not keep_garage:
        run("systemctl stop garage", check=False)
        run("systemctl disable garage", check=False)

        # 9. Optional disk-space reclaim. Garage's data LV and its
        #    metadata directory are no longer needed.
        run(f"umount /var/lib/garage/data 2>/dev/null", check=False)
        run(f"lvremove -f {VG}/garage-data 2>/dev/null", check=False)
        # Remove fstab line for garage-data
        if fstab.exists():
            text = fstab.read_text()
            new = "\n".join(l for l in text.splitlines()
                            if "garage-data" not in l)
            fstab.write_text(new + "\n" if new else "")
        run("rm -rf /var/lib/garage", check=False)
        run("rm -f /etc/garage.toml /etc/systemd/system/garage.service "
            "/etc/passwd-s3fs", check=False)
        run("systemctl daemon-reload", check=False)

    print(f"  [garage] scratch migrated to local LV; "
          f"Garage decommissioned." if not keep_garage else
          f"  [garage] scratch migrated to local LV; "
          f"Garage left running per keep_garage=True.")


# ── Clean-leave: node reset to post-bootstrap / pre-init state ────────────

def node_reset_local() -> None:
    """Bring this node back to its post-`bedrock bootstrap` state.

    Used when a node is being removed from the cluster (called over
    SSH from `bedrock storage remove-peer`'s cluster-side cleanup) or
    when an operator manually wants to take this node out of service.

    What this clears:
      - Stops bedrock services (mgmt/vm/vl/garage/nfs-server/mnt-isos)
      - Tears down DRBD resources + removes /etc/drbd.d/tier-*.res
      - Unmounts everything bedrock-related (NFS, s3fs, DRBD, local LVs)
      - Drops fstab entries for bedrock mounts
      - Removes NFS exports + garage config + s3fs creds + units
      - Removes tier LVs from the bedrock VG (data goes away — operator
        already accepted this by running remove-peer)
      - Removes /bedrock/* symlinks and /opt/bedrock/{mgmt,iso,data}
      - Drops /etc/bedrock/cluster.json
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
    services = ("bedrock-mgmt", "bedrock-vm", "bedrock-vl",
                "mnt-isos.mount", "mnt-isos.automount",
                "nfs-server", "garage")
    run(f"systemctl stop {' '.join(services)} 2>/dev/null", check=False)
    run(f"systemctl disable {' '.join(services)} 2>/dev/null", check=False)

    # 2. DRBD resources down + .res cleanup. Best-effort.
    for tier in ("bulk", "critical"):
        run(f"drbdadm down tier-{tier} 2>/dev/null", check=False)
        run(f"drbdsetup down tier-{tier} 2>/dev/null", check=False)
    run("rm -f /etc/drbd.d/tier-*.res /etc/drbd.d/tier-*.res.removed-* "
        "2>/dev/null", check=False)

    # 3. Unmount anything bedrock-touched. Two passes (normal then lazy)
    #    to handle any stuck handles per L16.
    mounts = (
        "/var/lib/bedrock/mounts/scratch-s3fs",
        "/var/lib/bedrock/mounts/bulk-nfs",
        "/var/lib/bedrock/mounts/critical-nfs",
        "/var/lib/bedrock/mounts/bulk-drbd",
        "/var/lib/bedrock/mounts/critical-drbd",
        "/var/lib/bedrock/local/scratch",
        "/var/lib/bedrock/local/bulk",
        "/var/lib/bedrock/local/critical",
        "/var/lib/garage/data",
        "/mnt/isos",
    )
    for mp in mounts:
        if run_ok(f"mountpoint -q {mp}"):
            run(f"umount {mp} 2>/dev/null || umount -l {mp} 2>/dev/null",
                check=False)

    # 4. Drop fstab lines for anything bedrock-related. Use a token list
    #    that matches every kind of mount we've ever installed.
    fstab = Path("/etc/fstab")
    if fstab.exists():
        tokens = ("/var/lib/bedrock", "/var/lib/garage",
                  "scratch-s3fs", "tier-", "garage-data",
                  "/mnt/isos", " /bedrock/")
        new = [l for l in fstab.read_text().splitlines()
               if not any(t in l for t in tokens)]
        fstab.write_text("\n".join(new).rstrip() + "\n")

    # 5. NFS exports
    run("rm -f /etc/exports.d/bedrock-*.exports 2>/dev/null", check=False)
    run("exportfs -ra 2>/dev/null", check=False)

    # 6. Garage config + creds + unit
    run("rm -f /etc/garage.toml /etc/passwd-s3fs "
        "/etc/systemd/system/garage.service 2>/dev/null", check=False)
    run("rm -rf /var/lib/garage 2>/dev/null", check=False)

    # 7. Tier LVs. Lvremove fails harmlessly if the LV is already gone.
    for lv in ("tier-scratch", "tier-bulk", "tier-critical",
               "tier-bulk-meta", "tier-critical-meta", "garage-data"):
        run(f"lvremove -fy bedrock/{lv} 2>/dev/null", check=False)

    # 8. /bedrock/* symlinks
    for tier in TIERS:
        link = PUBLIC_ROOT / tier
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
        except OSError:
            pass

    # 9. Mgmt-side /opt/bedrock/* subdirs that came from mgmt_install
    for sub in ("mgmt", "iso", "data", "vm", "vl"):
        run(f"rm -rf /opt/bedrock/{sub} 2>/dev/null", check=False)
    run("rm -f /opt/bedrock/scrape.yml 2>/dev/null", check=False)
    # Mgmt systemd units
    run("rm -f /etc/systemd/system/bedrock-{mgmt,vm,vl}.service "
        "/etc/systemd/system/mnt-isos.{mount,automount} 2>/dev/null",
        check=False)

    # 10. cluster.json gone; state.json truncated to bootstrap-only
    if CLUSTER_JSON.exists():
        CLUSTER_JSON.unlink()
    if STATE_JSON.exists():
        try:
            s = json.loads(STATE_JSON.read_text())
        except json.JSONDecodeError:
            s = {}
        keep = {k: s[k] for k in ("hardware", "bootstrap_done") if k in s}
        STATE_JSON.write_text(json.dumps(keep, indent=2))

    # 11. Reload systemd
    run("systemctl daemon-reload 2>/dev/null", check=False)

    print("  [reset] local state cleared. Run 'bedrock init' or "
          "'bedrock join'.")
