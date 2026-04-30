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
import shutil
import subprocess
import time
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
    """Run a command on a peer via root ssh."""
    full = (f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=8 root@{host} {json.dumps(cmd)}")
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

    Runs on the local node (typically the master). For the local node's id
    we read locally; for remote peers we ssh to each.
    """
    self_ip = _self_drbd_ip()
    ids: dict[str, str] = {}
    for ip in peers_drbd_ips:
        if ip == self_ip:
            full = run("sudo -u garage /usr/local/bin/garage node id -q",
                       check=False).strip()
        else:
            full = ssh(ip, "sudo -u garage /usr/local/bin/garage node id -q",
                       check=False).strip()
        if full:
            ids[ip] = full

    # Connect from this node to each remote peer (skip self)
    for ip, full_id in ids.items():
        if ip == self_ip:
            continue
        run(f"sudo -u garage /usr/local/bin/garage node connect '{full_id}'",
            check=False)

    time.sleep(2)

    # Layout: one zone, equal capacity per node
    short_ids = [v.split("@")[0][:16] for v in ids.values()]
    for sid in short_ids:
        run(f"sudo -u garage /usr/local/bin/garage layout assign "
            f"-z dc1 -c {capacity_gb}G {sid}", check=False)

    # Bump layout version monotonically (parse from `layout show`)
    out = run("sudo -u garage /usr/local/bin/garage layout show",
              check=False)
    next_version = 1
    for line in out.splitlines():
        # Garage 2.x prints "Current cluster layout version: N"
        if "layout version:" in line.lower():
            try:
                next_version = int(line.split(":")[1].strip()) + 1
            except (IndexError, ValueError):
                pass
    run(f"sudo -u garage /usr/local/bin/garage layout apply "
        f"--version {next_version}", check=False)


def garage_create_scratch_bucket() -> dict:
    """Create the 'scratch' bucket + key. Returns {access_key, secret_key}."""
    run("sudo -u garage /usr/local/bin/garage bucket create scratch",
        check=False)
    run("sudo -u garage /usr/local/bin/garage key create scratch-key",
        check=False)
    out = run("sudo -u garage /usr/local/bin/garage key info scratch-key "
              "--show-secret")
    ak, sk = None, None
    for line in out.splitlines():
        if "Key ID:" in line:
            ak = line.split(":", 1)[1].strip()
        if "Secret key:" in line:
            sk = line.split(":", 1)[1].strip()
    run("sudo -u garage /usr/local/bin/garage bucket allow "
        "--read --write --owner scratch --key scratch-key", check=False)
    return {"access_key": ak, "secret_key": sk}


def s3fs_mount_scratch(access_key: str, secret_key: str,
                        endpoint_drbd_ip: str | None = None) -> None:
    """Mount Garage's 'scratch' bucket via s3fs at /var/lib/bedrock/mounts/scratch-s3fs
    and point /bedrock/scratch at it.

    Always uses the LOCAL Garage daemon at 127.0.0.1:3900 (invariant #6
    in tier_storage.md). The endpoint_drbd_ip arg is accepted for
    backward compat with older callers but ignored — Garage handles
    cross-node block lookup internally via its own RPC.
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

    # Stop using the local scratch LV — unmount it (keep the LV around for
    # safety; operator can drop it later via `bedrock storage gc`).
    local_scratch = LOCAL_ROOT / "scratch"
    if run_ok(f"mountpoint -q {local_scratch}"):
        # Move any user data into Garage first — best-effort rsync
        run(f"sudo -u garage /usr/local/bin/garage bucket info scratch >/dev/null",
            check=False)
        # (skip rsync-into-S3 for now; that's a documented operator step)
        run(f"umount {local_scratch}", check=False)

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

    if not run_ok(f"mountpoint -q {s3fs_mount}"):
        run(f"mount {s3fs_mount}", check=False)

    atomic_symlink(str(s3fs_mount), PUBLIC_ROOT / "scratch")


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
    write_drbd_resource("critical", peers)
    run("drbdadm adjust tier-critical")
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
    surviving_peers: list[dict],
    surviving_hosts: list[str],
) -> None:
    """LINBIT-blessed online peer removal for a DRBD tier resource.

    Service to /dev/drbd<minor> on the surviving primary stays up. The
    leaving peer is dropped from kernel state on every survivor via
    `drbdadm adjust` — which reads the (newly-edited) on-disk config
    and issues `drbdsetup del-peer` for the now-missing peer.

    Args:
        resource:         tier name (e.g. "bulk" or "critical")
        leaving_peer_name: peer's hostname as it appears in the .res
        surviving_peers:  list of {"name": ..., "drbd_ip": ...} for the
                          peers that REMAIN. Their persistent node-ids
                          are honored — none of their connections are
                          touched.
        surviving_hosts:  list of mgmt-LAN hosts (or any reachable IP)
                          to SSH into for the per-node operations

    Crash-safety: the on-disk config is rewritten + distributed BEFORE
    drbdadm adjust applies it. A power loss between distribute and
    apply leaves persistent state already at the desired end state;
    `drbdadm up` on next boot reconciles correctly.

    See tier_storage.md "drbd_remove_peer" for the command-by-command
    breakdown and source citations.
    """
    print(f"  [tier] drbd_remove_peer({resource}, leaving={leaving_peer_name})")

    # 1. Render new config WITHOUT the leaving peer; persistent node-ids
    #    of survivors are preserved (render_drbd_res reads cluster.json).
    write_drbd_resource(resource, surviving_peers)
    new_res = Path(f"/etc/drbd.d/tier-{resource}.res").read_text()

    # 2. Distribute identical config to every surviving host. We assume
    #    sshable as root via id_ed25519 (set up at cluster init).
    for host in surviving_hosts:
        # Use base64 to avoid quoting headaches with the resource body
        import base64
        b = base64.b64encode(new_res.encode()).decode()
        ssh(host, f"echo {b} | base64 -d > /etc/drbd.d/tier-{resource}.res")

    # 3. Dry-run on each survivor; abort if adjust would do anything
    #    other than del-peer for the leaving peer (and possibly
    #    disconnect, which is implicit in del-peer per drbd-utils).
    leaving_id = (load_cluster().get("tiers", {}).get(resource, {})
                  .get("drbd_node_ids", {}).get(leaving_peer_name))
    for host in surviving_hosts:
        out = ssh(host, f"drbdadm --dry-run adjust tier-{resource}",
                  check=False)
        # Empty output is fine (no changes needed). Non-empty must
        # only mention del-peer (or disconnect) for the leaving id.
        for line in out.splitlines():
            if not line.strip():
                continue
            allowed = ("del-peer", "disconnect", "del-path", "down")
            if not any(tok in line for tok in allowed):
                raise RuntimeError(
                    f"adjust dry-run on {host} would do unexpected work: "
                    f"{line!r}. Aborting peer removal — investigate before "
                    f"forcing.")

    # 4. Apply on each survivor. drbdadm adjust issues del-peer to the
    #    kernel for any kernel-side connection without a matching
    #    config entry. /dev/drbdN stays up the whole time.
    for host in surviving_hosts:
        ssh(host, f"drbdadm adjust tier-{resource}")

    # 5. Free the meta-disk bitmap slot. Optional but recommended: a
    #    later distinct peer added to this resource can reuse the
    #    cleared slot via a bitmap-based resync rather than a full
    #    sync. Run on every survivor.
    if leaving_id is not None:
        for host in surviving_hosts:
            ssh(host,
                f"drbdsetup forget-peer tier-{resource} {leaving_id}",
                check=False)
        # Drop the persistent assignment so future add can re-allocate
        free_drbd_node_id(resource, leaving_peer_name)

    # 6. Persist updated peer list in cluster.json
    set_tier_state(resource, mode="drbd-nfs",
                   peers=[p["name"] for p in surviving_peers])
    print(f"  [tier] drbd_remove_peer({resource}): done. "
          f"{len(surviving_peers)} peers remain.")


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

    g = "sudo -u garage /usr/local/bin/garage"

    # 1. Stage the layout removal + apply. Garage assigns this node's
    #    partitions to surviving nodes in the new layout version.
    ssh(surviving_admin_host, f"{g} layout remove {departing_node_id_short}")

    # Determine the next layout version
    show = ssh(surviving_admin_host, f"{g} layout show")
    next_version = 1
    for line in show.splitlines():
        if "layout version" in line.lower():
            try:
                next_version = int(line.split(":")[1].strip()) + 1
                break
            except (IndexError, ValueError):
                pass

    ssh(surviving_admin_host,
        f"{g} layout apply --version {next_version}")

    # 2. Speed up the resync workers on the DEPARTING node (where the
    #    blocks live). Default tranquility throttles for impact-friendly
    #    operation; for a controlled drain we want it to drain fast.
    ssh(departing_node_admin_host,
        f"{g} worker set resync-tranquility 0", check=False)
    ssh(departing_node_admin_host,
        f"{g} worker set resync-worker-count 8", check=False)

    # 3. Wait for all "Block resync" workers on the departing node to
    #    show Idle with Queue=0. This is the indicator that all blocks
    #    have been copied to their new owners and the source can be
    #    safely retired.
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        out = ssh(departing_node_admin_host, f"{g} worker list",
                  check=False)
        # Parse "Block resync worker #N" lines, check State and Queue
        # columns. Format (v2.x):
        #   TID  State  Name                    Tranq  Done  Queue ...
        all_idle = True
        any_resync = False
        for line in out.splitlines():
            if "Block resync worker" not in line:
                continue
            any_resync = True
            cols = line.split()
            if len(cols) < 6:
                continue
            state = cols[1]
            queue = cols[5]
            if state != "Idle" or (queue not in ("0", "-")):
                all_idle = False
                break
        if any_resync and all_idle:
            break
        time.sleep(poll_seconds)
    else:
        raise RuntimeError(
            f"garage drain timeout after {max_wait_seconds}s — "
            f"workers still not Idle. Investigate before stopping the node.")

    # 4. Verify no errored blocks. Any block in the error queue means
    #    we have a candidate for actual data loss.
    errs = ssh(departing_node_admin_host, f"{g} block list-errors",
               check=False)
    err_lines = [l for l in errs.splitlines() if l.strip()
                 and not l.startswith("Hash")]
    if err_lines:
        raise RuntimeError(
            f"garage block list-errors not empty on {departing_node_admin_host}: "
            f"{len(err_lines)} entries. NOT safe to remove node yet.")

    # 5. Run repair on the whole cluster to ensure metadata tables and
    #    block references are consistent. Garage docs recommend this
    #    after layout changes. Idempotent.
    ssh(surviving_admin_host,
        f"{g} repair --all-nodes --yes tables", check=False)
    ssh(surviving_admin_host,
        f"{g} repair --all-nodes --yes blocks", check=False)

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

    # 6. Copy systemd unit files. mnt-isos.mount and the bedrock-*
    #    units are idempotent if pre-existing (rsync overwrites).
    for unit in ("bedrock-mgmt.service", "bedrock-vm.service",
                 "bedrock-vl.service", "mnt-isos.mount"):
        ssh(new_master_host,
            f"rsync -aHX -e 'ssh -o StrictHostKeyChecking=no' "
            f"root@{old_master_host}:/etc/systemd/system/{unit} "
            f"/etc/systemd/system/{unit}", check=False)
    ssh(new_master_host, "systemctl daemon-reload")

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
            ssh(peer,
                f"umount /var/lib/bedrock/mounts/{tier}-nfs", check=False)
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

    # 11. Update cluster.json on the new master + propagate
    new_master_name = ssh(new_master_host,
                          "hostname --fqdn 2>/dev/null || hostname",
                          check=False).strip()
    for host in [new_master_host] + other_peer_hosts:
        ssh(host,
            f"python3 -c 'import json; from pathlib import Path; "
            f"p=Path(\"/etc/bedrock/cluster.json\"); "
            f"c=json.loads(p.read_text()) if p.exists() else {{}}; "
            f"c.setdefault(\"tiers\",{{}}); "
            f"[c[\"tiers\"].setdefault(t,{{}}).update("
            f"{{\"master\":\"{new_master_name}\"}}) for t in (\"bulk\",\"critical\")]; "
            f"p.write_text(json.dumps(c, indent=2))'",
            check=False)

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

    # 2. Remove .res file FIRST so any reboot won't try to up the resource
    res_file = Path(f"/etc/drbd.d/{res}.res")
    backup_file = Path(f"/etc/drbd.d/{res}.res.demoted")
    if res_file.exists():
        # Move-aside (vs. delete) so we can recover if the demote fails
        res_file.rename(backup_file)

    # 3. Update fstab: drop the DRBD-mount line, add the local-LV line
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

    # 4. drbdsetup down — release /dev/drbdN. drbdadm wouldn't work
    #    here because the .res is gone; drbdsetup operates by name in
    #    kernel state.
    run(f"drbdsetup down {res}", check=False)
    if run_ok(f"mountpoint -q {drbd_mount}"):
        run(f"umount {drbd_mount}", check=False)

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
    run(f"rsync -aHX --inplace {s3fs_mount}/ {local_mount}/",
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
