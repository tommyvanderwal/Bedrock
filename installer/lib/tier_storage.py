"""Bedrock storage tiers — scratch / bulk / critical.

Stable abstraction: /bedrock/{scratch,bulk,critical}, identical on every node.
Backend swaps under the symlink as the cluster grows.

  N=1: /bedrock/<tier> -> /var/lib/bedrock/local/<tier>      (local thin LV)
  N=2: scratch -> Garage (S3) via s3fs FUSE mount
       bulk    -> DRBD 2-way + XFS, NFS-exported from master, peer mounts NFS
       critical-> same as bulk (degenerate at 2 nodes; only differentiates at N>=3)
  N=3: critical promoted to 3-way DRBD; bulk stays 2-way; scratch Garage extends
  N=4: same shape; new node joins Garage + NFS clients only

The DRBD migration uses external metadata so the underlying LV's filesystem is
preserved byte-for-byte — no data copy required when promoting a local LV to a
DRBD-replicated LV (only a brief unmount/remount).

This module is callable from:
  - mgmt_install.install_full()  -> calls setup_n1()
  - agent_install.install()      -> calls setup_n1() then transitions
  - bedrock storage <subcommand> -> manual operator transitions
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

def render_drbd_res(resource: str, minor: int,
                    peers: list[dict]) -> str:
    """Render a DRBD resource file. peers = [{name, drbd_ip}, ...]."""
    on_blocks = []
    node_id = 0
    for p in peers:
        on_blocks.append(
            f'  on {p["name"]} {{\n'
            f'    node-id   {node_id};\n'
            f'    device    /dev/drbd{minor};\n'
            f'    disk      /dev/{VG}/tier-{resource};\n'
            f'    meta-disk /dev/{VG}/tier-{resource}-meta;\n'
            f'    address   {p["drbd_ip"]}:{7000 + minor};\n'
            f'  }}\n'
        )
        node_id += 1

    # Connection mesh between every pair
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
    """Write /etc/drbd.d/tier-<resource>.res based on peer list."""
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
                        endpoint_drbd_ip: str) -> None:
    """Mount Garage's 'scratch' bucket via s3fs at /var/lib/bedrock/mounts/scratch-s3fs
    and point /bedrock/scratch at it.
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
    line = (f"scratch {s3fs_mount} fuse.s3fs "
            f"_netdev,allow_other,umask=0022,sigv4,endpoint=garage,"
            f"use_path_request_style,"
            f"url=http://{endpoint_drbd_ip}:{GARAGE_S3_PORT},"
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
