"""
Storage tier install helpers.

Mirrors the manual steps validated by docs/scenarios/storage-trial-2026-04-27.md.
Splits Bedrock's storage stack into:

  - RustFS  (containerized, podman) → buckets `critical` (EC:2 = STANDARD)
                                       and `bulk` (EC:1 = REDUCED_REDUNDANCY)
  - Garage  (native musl binary)    → bucket `scratch` (replication_factor=1)
  - s3backer per VM data disk       → /mnt/s3disk-<n>/file (FUSE block device)
  - s3fs-fuse for templates         → /var/lib/libvirt/templates dir pool

Cluster-size gating (advertised by mgmt to the dashboard):
  1 node : Cattle / Scratch
  2 nodes: + Pet / Bulk
  3 nodes: + Pet+ (DRBD 3-way)
  4 nodes: + Critical-EC (RustFS EC:2)

The 3-node Critical case is mathematically RS-valid (1 data + 2 parity)
but undocumented in RustFS alpha.99 — keep gated at 4 until GA.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import textwrap
from pathlib import Path
from typing import Iterable, Sequence

# ── Constants ──────────────────────────────────────────────────────────────

RUSTFS_IMAGE = "docker.io/rustfs/rustfs:latest"
RUSTFS_S3_PORT = 9000
RUSTFS_CONSOLE_PORT = 9001
RUSTFS_CONTAINER_UID = 10001  # the `rustfs` user inside the image
RUSTFS_DATA_DIR = "/var/lib/rustfs/data"
RUSTFS_LOG_DIR = "/var/log/rustfs"

GARAGE_VERSION = "v2.3.0"
GARAGE_URL = (
    f"https://garagehq.deuxfleurs.fr/_releases/{GARAGE_VERSION}/"
    "x86_64-unknown-linux-musl/garage"
)
GARAGE_S3_PORT = 3900
GARAGE_RPC_PORT = 3901
GARAGE_ADMIN_PORT = 3903
GARAGE_DATA_DIR = "/var/lib/garage/data"
GARAGE_META_DIR = "/var/lib/garage/meta"
GARAGE_BLOCK_SIZE = 10 * 1024 * 1024  # 10 MiB — better fanout for big files

S3BACKER_REPO = "https://github.com/archiecobbs/s3backer.git"
S3BACKER_REF = "master"  # need master HEAD for --sharedDiskMode + --no-vhost

# Ports unioned across the storage stack
PUBLIC_TCP_PORTS = (
    RUSTFS_S3_PORT, RUSTFS_CONSOLE_PORT,
    GARAGE_S3_PORT, GARAGE_RPC_PORT, GARAGE_ADMIN_PORT,
)


# ── Provisioning ───────────────────────────────────────────────────────────

def provision_thin_lv(
    ssh, vg: str, lv_name: str, size_gb: int, mount_point: str,
    owner_uid: int = 0, owner_gid: int = 0,
) -> None:
    """
    Create a thin LV, ext4-format with discard, mount with `discard` so the
    TRIM chain (ext4 → thin LV → thin pool → backing) works end-to-end.
    """
    ssh(f"lvcreate -V {size_gb}G -T {vg}/thinpool -n {lv_name}")
    dev = f"/dev/{vg}/{lv_name}"
    ssh(f"mkfs.ext4 -F -L {lv_name} -E lazy_itable_init=0,lazy_journal_init=0 {dev}")
    ssh(f"mkdir -p {mount_point}")
    fstab_line = f"{dev} {mount_point} ext4 defaults,discard 0 0"
    ssh(f"grep -q '{lv_name}' /etc/fstab || echo '{fstab_line}' >> /etc/fstab")
    ssh(f"mount {mount_point}")
    if owner_uid:
        ssh(f"chown -R {owner_uid}:{owner_gid} {mount_point}")


# ── RustFS ─────────────────────────────────────────────────────────────────

def render_rustfs_env(volumes_urls: Sequence[str], access_key: str,
                      secret_key: str, ec_set_size: int = 4) -> str:
    """
    Render `/etc/default/rustfs`. Volumes go HERE (not on the CLI) — passing
    them as CLI args makes the container entrypoint append `/data`, breaking
    `RUSTFS_ERASURE_SET_DRIVE_COUNT`.
    """
    return textwrap.dedent(f"""\
        RUSTFS_ACCESS_KEY={access_key}
        RUSTFS_SECRET_KEY={secret_key}
        RUSTFS_ADDRESS=:{RUSTFS_S3_PORT}
        RUSTFS_CONSOLE_ADDRESS=:{RUSTFS_CONSOLE_PORT}
        RUSTFS_VOLUMES={' '.join(volumes_urls)}
        RUSTFS_STORAGE_CLASS_STANDARD=EC:2
        RUSTFS_STORAGE_CLASS_REDUCED_REDUNDANCY=EC:1
        RUSTFS_ERASURE_SET_DRIVE_COUNT={ec_set_size}
        RUSTFS_OBS_LOG_DIRECTORY={RUSTFS_LOG_DIR}
    """)


def render_rustfs_systemd_unit() -> str:
    """
    podman-managed RustFS service. Bind-mounts the data dir AND the log dir
    so the entrypoint's `mkdir -p $RUSTFS_OBS_LOG_DIRECTORY` succeeds without
    polluting the data namespace.
    """
    return textwrap.dedent(f"""\
        [Unit]
        Description=RustFS distributed object store (podman)
        After=network-online.target
        Wants=network-online.target
        RequiresMountsFor={RUSTFS_DATA_DIR}

        [Service]
        Type=simple
        ExecStartPre=-/usr/bin/podman rm -f rustfs
        ExecStart=/usr/bin/podman run --rm --name rustfs --network host \\
            --env-file /etc/default/rustfs \\
            -v {RUSTFS_DATA_DIR}:/data:Z \\
            -v {RUSTFS_LOG_DIR}:{RUSTFS_LOG_DIR}:Z \\
            {RUSTFS_IMAGE}
        ExecStop=/usr/bin/podman stop -t 10 rustfs
        Restart=on-failure
        RestartSec=10s
        LimitNOFILE=1048576

        [Install]
        WantedBy=multi-user.target
    """)


def install_rustfs(ssh, peer_ips: Sequence[str], access_key: str,
                   secret_key: str) -> None:
    """One-shot: install podman + image, write env + unit, enable + start."""
    ssh("dnf install -y -q podman")
    ssh(f"podman pull {RUSTFS_IMAGE}")
    ssh(f"chown -R {RUSTFS_CONTAINER_UID}:{RUSTFS_CONTAINER_UID} "
        f"{RUSTFS_DATA_DIR} {RUSTFS_LOG_DIR}")
    volumes = [f"http://{ip}:{RUSTFS_S3_PORT}/data" for ip in peer_ips]
    ssh.put("/etc/default/rustfs", render_rustfs_env(volumes, access_key,
                                                      secret_key,
                                                      len(peer_ips)),
            mode=0o600)
    ssh.put("/etc/systemd/system/rustfs.service",
            render_rustfs_systemd_unit())
    ssh("systemctl daemon-reload && systemctl enable rustfs.service")


# ── Garage ─────────────────────────────────────────────────────────────────

def render_garage_toml(rpc_secret: str, admin_token: str,
                       rpc_public_addr: str) -> str:
    return textwrap.dedent(f"""\
        metadata_dir = "{GARAGE_META_DIR}"
        data_dir     = "{GARAGE_DATA_DIR}"
        db_engine    = "lmdb"

        replication_factor = 1
        rpc_secret      = "{rpc_secret}"
        rpc_bind_addr   = "[::]:{GARAGE_RPC_PORT}"
        rpc_public_addr = "{rpc_public_addr}:{GARAGE_RPC_PORT}"

        block_size = {GARAGE_BLOCK_SIZE}

        [s3_api]
        api_bind_addr = "[::]:{GARAGE_S3_PORT}"
        s3_region     = "garage"
        root_domain   = ".s3.scratch.local"

        [admin]
        api_bind_addr = "[::]:{GARAGE_ADMIN_PORT}"
        admin_token   = "{admin_token}"
    """)


def render_garage_systemd_unit() -> str:
    return textwrap.dedent(f"""\
        [Unit]
        Description=Garage S3-compatible store (scratch tier)
        After=network-online.target
        Wants=network-online.target
        RequiresMountsFor={GARAGE_DATA_DIR}

        [Service]
        User=garage
        Group=garage
        Environment=RUST_LOG=garage=info RUST_BACKTRACE=1
        ExecStart=/usr/local/bin/garage server
        Restart=on-failure
        RestartSec=5
        LimitNOFILE=65536

        [Install]
        WantedBy=multi-user.target
    """)


def install_garage(ssh, rpc_secret: str, admin_token: str,
                   public_ip: str) -> None:
    ssh(f"curl -sSL -o /usr/local/bin/garage {GARAGE_URL}")
    ssh("chmod +x /usr/local/bin/garage")
    ssh("id garage &>/dev/null || useradd -r -s /sbin/nologin -d "
        "/var/lib/garage garage")
    ssh.put("/etc/garage.toml", render_garage_toml(rpc_secret, admin_token,
                                                    public_ip),
            mode=0o640, owner="root:garage")
    ssh.put("/etc/systemd/system/garage.service", render_garage_systemd_unit())
    ssh("chown -R garage:garage /var/lib/garage")
    ssh("systemctl daemon-reload && systemctl enable garage.service")


# ── s3backer (build from master, distribute) ──────────────────────────────

def build_s3backer_master(ssh) -> None:
    """
    Build s3backer from master HEAD. Need master, not the v1.4.5 EPEL or
    GitHub release, for `--sharedDiskMode` and `--no-vhost`.
    """
    ssh("dnf config-manager --set-enabled crb")
    ssh("dnf install -y -q gcc make autoconf automake libtool pkgconfig "
        "git fuse3-devel libcurl-devel expat-devel libxml2-devel zlib-devel")
    ssh("rm -rf /tmp/s3backer-build && mkdir -p /tmp/s3backer-build")
    ssh(f"cd /tmp/s3backer-build && git clone -q --depth=1 "
        f"-b {S3BACKER_REF} {S3BACKER_REPO} src && cd src && "
        "mkdir -p m4 && autoreconf -iv >/dev/null && "
        "./configure >/dev/null && make -j4 >/dev/null && make install")


def s3backer_mount_cmd(bucket: str, mount_point: str, base_url: str,
                       creds_file: str, size_gb: int,
                       block_size_mib: int = 1) -> list[str]:
    """
    Returns the argv for an s3backer mount with the validated, no-cache,
    no-force flags. Caller can wrap with systemd-run or a service unit.
    """
    return [
        "/usr/bin/s3backer",
        f"--baseURL={base_url}",
        f"--accessFile={creds_file}",
        "--no-vhost", "--sharedDiskMode",
        f"--size={size_gb}G", f"--blockSize={block_size_mib}m",
        "--fileMode=0666",  # so qemu can open the FUSE-backed file
        bucket, mount_point,
    ]


# ── Cluster-size gating ────────────────────────────────────────────────────

def available_tiers(node_count: int) -> dict[str, list[str]]:
    """
    Map cluster size to the list of tiers we'll advertise.
    Used by mgmt to gate the tier dropdown in the dashboard.
    """
    vm_disk = ["cattle"]
    fileshare = ["scratch"]
    if node_count >= 2:
        vm_disk.append("pet")
        fileshare.append("bulk")
    if node_count >= 3:
        vm_disk.append("pet+")  # DRBD 3-way is fine at 3 nodes
        # NOTE: critical-EC at 3 nodes (RustFS EC:2 = 1d+2p) is RS-valid
        # but not a documented/tested RustFS config. Wait for GA.
    if node_count >= 4:
        fileshare.append("critical")
    return {"vm_disk": vm_disk, "fileshare": fileshare}


# ── Multi-network resilience ───────────────────────────────────────────────

def install_drbd_mgmt_fallback_routes(ssh, peers: Iterable[tuple[str, str]],
                                       drbd_iface: str = "eth1") -> None:
    """
    Per-peer host route via the mgmt LAN at metric 200, used only when the
    drbd-network connected route is gone (link down). Does NOT recover an
    in-flight TCP connection — depends on the application's reconnect logic
    (RustFS does retry, so it works in practice).

    `peers` = iterable of (peer_drbd_ip, peer_mgmt_ip).
    """
    ssh("sysctl -wq net.ipv4.ip_forward=1")
    ssh("sysctl -wq net.ipv4.conf.all.rp_filter=2 "
        "net.ipv4.conf.default.rp_filter=2")
    for drbd_ip, mgmt_ip in peers:
        ssh(f"ip route del {drbd_ip}/32 2>/dev/null; "
            f"ip route add {drbd_ip}/32 via {mgmt_ip} metric 200")


# ── Helpers ────────────────────────────────────────────────────────────────

def gen_secret(nbytes: int = 32) -> str:
    return secrets.token_hex(nbytes)
