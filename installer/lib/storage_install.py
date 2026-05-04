#!/usr/bin/env python3
"""
Bedrock storage stack installer.

Mirrors the manual procedure validated by docs/scenarios/storage-trial-2026-04-27.md
and made re-runnable end-to-end. Provisions:

  - RustFS (containerized, podman) for Bulk + Critical tiers (EC parity)
  - Garage (native musl binary) for Scratch tier (replication_factor=1)
  - s3backer 2.x (built from master) for VM data disks (FUSE block device)
  - s3fs-fuse for templates pool (libvirt directory pool source)
  - Per-peer mgmt-LAN fallback routes for the DRBD network

USAGE
  storage_install.py setup --nodes <n1>:<drbd1>,<n2>:<drbd2>,...  \\
                            [--ec-standard 2] [--ec-reduced 1] \\
                            [--ec-set-size <auto|N>]
  storage_install.py teardown --nodes <n1>:<drbd1>,...
  storage_install.py status   --nodes <n1>:<drbd1>,...

`--ec-set-size auto` (default) sets the set count = number of nodes (one
volume per node). For empirical 3-node EC:1 testing, override.

Re-running setup is idempotent — already-installed components are left alone.
Teardown is destructive (drops all storage state, leaves the OS intact).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import secrets
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path

# ── Pinned versions ────────────────────────────────────────────────────────

RUSTFS_IMAGE = "docker.io/rustfs/rustfs:1.0.0-alpha.99"
GARAGE_VERSION = "v2.3.0"
GARAGE_URL = (f"https://garagehq.deuxfleurs.fr/_releases/{GARAGE_VERSION}/"
              "x86_64-unknown-linux-musl/garage")
S3BACKER_REPO = "https://github.com/archiecobbs/s3backer.git"
S3BACKER_REF = "master"  # need master HEAD for --sharedDiskMode + --no-vhost

# ── Layout constants ───────────────────────────────────────────────────────

RUSTFS_S3_PORT = 9000
RUSTFS_CONSOLE_PORT = 9001
RUSTFS_CONTAINER_UID = 10001  # rustfs user inside the image
RUSTFS_DATA_DIR = "/var/lib/rustfs/data"
RUSTFS_LOG_DIR = "/var/log/rustfs"
RUSTFS_DATA_LV_GB = 40

GARAGE_S3_PORT = 3900
GARAGE_RPC_PORT = 3901
GARAGE_ADMIN_PORT = 3903
GARAGE_DATA_DIR = "/var/lib/garage/data"
GARAGE_META_DIR = "/var/lib/garage/meta"
GARAGE_DATA_LV_GB = 25
GARAGE_BLOCK_SIZE = 10 * 1024 * 1024  # 10 MiB

VG = "almalinux"  # default volume group on the AlmaLinux 9 sims
TEMPLATES_MOUNT = "/var/lib/libvirt/templates"


# ── Node spec + Ssh helper ─────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class Node:
    mgmt_ip: str   # e.g. 192.168.2.167
    drbd_ip: str   # e.g. 10.99.0.10


class Ssh:
    """Minimal ssh-as-root wrapper."""
    def __init__(self, host: str, quiet: bool = True):
        self.host = host
        self.quiet = quiet

    def run(self, cmd: str, check: bool = True, timeout: int = 600) -> str:
        full = ["ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=8",
                f"root@{self.host}", cmd]
        if not self.quiet:
            print(f"[{self.host}] $ {cmd}", file=sys.stderr)
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0:
            raise RuntimeError(
                f"ssh {self.host} cmd failed (rc={r.returncode}):\n"
                f"  cmd: {cmd}\n  stderr: {r.stderr.strip()}")
        return r.stdout

    def put(self, path: str, content: str, mode: int = 0o644,
            owner: str | None = None) -> None:
        import base64
        b = base64.b64encode(content.encode()).decode()
        self.run(f"echo {b} | base64 -d > {path}")
        self.run(f"chmod {oct(mode)[2:]} {path}")
        if owner:
            self.run(f"chown {owner} {path}")


# ── Provisioning a thin LV with discard mount ─────────────────────────────

def provision_thin_lv(s: Ssh, lv_name: str, size_gb: int, mount_point: str,
                       owner_uid: int = 0, owner_gid: int = 0) -> None:
    """Idempotent: skip if LV already exists."""
    if s.run(f"lvs {VG}/{lv_name} --noheadings 2>/dev/null", check=False).strip():
        return
    s.run(f"lvcreate -V {size_gb}G -T {VG}/thinpool -n {lv_name}")
    s.run(f"mkfs.ext4 -F -L {lv_name} -E lazy_itable_init=0,lazy_journal_init=0 "
          f"/dev/{VG}/{lv_name}")
    s.run(f"mkdir -p {mount_point}")
    # nofail: a corrupt thin-LV after power-loss must NOT drop the host into
    # emergency mode — let the rustfs/garage service fail loudly instead so
    # the node stays SSH-able for recovery.
    fstab = f"/dev/{VG}/{lv_name} {mount_point} ext4 defaults,nofail,discard 0 0"
    s.run(f"grep -q '{lv_name}' /etc/fstab || echo '{fstab}' >> /etc/fstab")
    s.run(f"mount {mount_point} 2>/dev/null || true")
    if owner_uid:
        s.run(f"chown -R {owner_uid}:{owner_gid} {mount_point}")


# ── RustFS ─────────────────────────────────────────────────────────────────

def render_rustfs_env(volumes: list[str], access_key: str, secret_key: str,
                      ec_set_size: int, ec_standard: int,
                      ec_reduced: int | None) -> str:
    lines = [
        f"RUSTFS_ACCESS_KEY={access_key}",
        f"RUSTFS_SECRET_KEY={secret_key}",
        f"RUSTFS_ADDRESS=:{RUSTFS_S3_PORT}",
        f"RUSTFS_CONSOLE_ADDRESS=:{RUSTFS_CONSOLE_PORT}",
        f"RUSTFS_VOLUMES={' '.join(volumes)}",
        f"RUSTFS_STORAGE_CLASS_STANDARD=EC:{ec_standard}",
        f"RUSTFS_ERASURE_SET_DRIVE_COUNT={ec_set_size}",
        f"RUSTFS_OBS_LOG_DIRECTORY={RUSTFS_LOG_DIR}",
    ]
    if ec_reduced is not None:
        lines.append(f"RUSTFS_STORAGE_CLASS_REDUCED_REDUNDANCY=EC:{ec_reduced}")
    return "\n".join(lines) + "\n"


def render_rustfs_systemd_unit() -> str:
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


def install_rustfs_node(s: Ssh, volumes: list[str], access_key: str,
                         secret_key: str, ec_set_size: int,
                         ec_standard: int, ec_reduced: int | None,
                         volumes_per_node: int = 1) -> None:
    """Install RustFS on a single node (config + systemd; not start)."""
    s.run("dnf install -y -q podman", timeout=180)
    s.run(f"podman pull {RUSTFS_IMAGE}", timeout=300)
    # Provision data LVs (one per drive in the set; default = 1/node)
    for i in range(volumes_per_node):
        suffix = f"-{i}" if volumes_per_node > 1 else ""
        provision_thin_lv(s, f"rustfs-data{suffix}", RUSTFS_DATA_LV_GB,
                           f"{RUSTFS_DATA_DIR}{suffix}")
    s.run(f"mkdir -p {RUSTFS_DATA_DIR} {RUSTFS_LOG_DIR}")
    s.run(f"chown -R {RUSTFS_CONTAINER_UID}:{RUSTFS_CONTAINER_UID} "
          f"{RUSTFS_DATA_DIR} {RUSTFS_LOG_DIR}")
    s.put("/etc/default/rustfs",
          render_rustfs_env(volumes, access_key, secret_key,
                             ec_set_size, ec_standard, ec_reduced),
          mode=0o600)
    s.put("/etc/systemd/system/rustfs.service",
          render_rustfs_systemd_unit())
    s.run("systemctl daemon-reload")
    s.run("systemctl enable rustfs.service", check=False)


def start_rustfs_cluster(nodes: list[Node]) -> None:
    """Start all nodes near-simultaneously (distributed bootstrap needs N)."""
    procs = []
    for n in nodes:
        procs.append(subprocess.Popen(
            ["ssh", f"root@{n.mgmt_ip}", "systemctl restart rustfs.service"]))
    for p in procs:
        p.wait()
    time.sleep(8)


# ── Garage ─────────────────────────────────────────────────────────────────

def render_garage_toml(rpc_secret: str, admin_token: str,
                       rpc_public_addr: str, replication_factor: int = 1) -> str:
    return textwrap.dedent(f"""\
        metadata_dir = "{GARAGE_META_DIR}"
        data_dir     = "{GARAGE_DATA_DIR}"
        db_engine    = "lmdb"

        replication_factor = {replication_factor}
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
        Description=Garage S3-compatible store
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


def install_garage_node(s: Ssh, drbd_ip: str, rpc_secret: str,
                         admin_token: str,
                         replication_factor: int = 1) -> None:
    s.run("id garage &>/dev/null || useradd -r -s /sbin/nologin "
          "-d /var/lib/garage garage")
    provision_thin_lv(s, "garage-data", GARAGE_DATA_LV_GB, GARAGE_DATA_DIR)
    s.run(f"mkdir -p {GARAGE_META_DIR}")
    s.run(f"chown -R garage:garage /var/lib/garage")
    if not s.run("test -x /usr/local/bin/garage && echo y || echo n").strip().endswith("y"):
        s.run(f"curl -sSL -o /usr/local/bin/garage {GARAGE_URL}", timeout=300)
        s.run("chmod +x /usr/local/bin/garage")
    s.put("/etc/garage.toml",
          render_garage_toml(rpc_secret, admin_token, drbd_ip,
                              replication_factor),
          mode=0o640, owner="root:garage")
    s.put("/etc/systemd/system/garage.service",
          render_garage_systemd_unit())
    s.run("systemctl daemon-reload")
    s.run("systemctl enable garage.service", check=False)


def garage_form_cluster(nodes: list[Node], replication_factor: int = 1,
                         capacity_gb: int = 20) -> None:
    """After daemons are up: collect node IDs, connect peers, apply layout."""
    ids: dict[str, str] = {}
    for n in nodes:
        s = Ssh(n.mgmt_ip)
        out = s.run("sudo -u garage /usr/local/bin/garage node id -q").strip()
        ids[n.drbd_ip] = out
    bootstrap = Ssh(nodes[0].mgmt_ip)
    for n in nodes[1:]:
        bootstrap.run(f"sudo -u garage /usr/local/bin/garage node connect "
                       f"'{ids[n.drbd_ip]}'", check=False)
    time.sleep(2)
    short_ids = {k: v.split("@")[0][:16] for k, v in ids.items()}
    for sid in short_ids.values():
        bootstrap.run(f"sudo -u garage /usr/local/bin/garage layout assign "
                       f"-z dc1 -c {capacity_gb}G {sid}")
    bootstrap.run("sudo -u garage /usr/local/bin/garage layout apply --version 1")


def garage_create_bucket(node: Node, name: str, key_name: str = "scratch-key"
                          ) -> dict:
    """Create bucket + key, grant key, return {access_key, secret_key}."""
    s = Ssh(node.mgmt_ip)
    s.run(f"sudo -u garage /usr/local/bin/garage bucket create {name}",
          check=False)
    s.run(f"sudo -u garage /usr/local/bin/garage key create {key_name}",
          check=False)
    out = s.run(f"sudo -u garage /usr/local/bin/garage key info {key_name} "
                 "--show-secret")
    ak, sk = None, None
    for line in out.splitlines():
        if "Key ID:" in line:
            ak = line.split(":", 1)[1].strip()
        if "Secret key:" in line:
            sk = line.split(":", 1)[1].strip()
    s.run(f"sudo -u garage /usr/local/bin/garage bucket allow "
          f"--read --write --owner {name} --key {key_name}", check=False)
    return {"access_key": ak, "secret_key": sk}


# ── s3backer build (per node) ──────────────────────────────────────────────

def install_s3backer(s: Ssh) -> None:
    """Build s3backer master from source and install. Idempotent."""
    if s.run("test -x /usr/bin/s3backer && echo y || echo n",
             check=False).strip().endswith("y"):
        return
    s.run("dnf config-manager --set-enabled crb", check=False)
    s.run("dnf install -y -q gcc make autoconf automake libtool pkgconfig "
          "git fuse3-devel libcurl-devel expat-devel libxml2-devel zlib-devel",
          timeout=300)
    s.run("rm -rf /tmp/s3backer-build && mkdir -p /tmp/s3backer-build")
    s.run(f"cd /tmp/s3backer-build && git clone -q --depth=1 "
          f"-b {S3BACKER_REF} {S3BACKER_REPO} src", timeout=300)
    s.run("cd /tmp/s3backer-build/src && mkdir -p m4 && "
          "autoreconf -iv >/dev/null && ./configure >/dev/null && "
          "make -j4 >/dev/null && make install", timeout=600)


# ── s3fs-fuse for templates pool ───────────────────────────────────────────

def install_s3fs_templates(s: Ssh, drbd_ip: str, access_key: str,
                            secret_key: str) -> None:
    """Mount Garage's templates bucket at the libvirt templates path."""
    s.run("dnf install -y -q s3fs-fuse", timeout=180)
    s.put("/etc/passwd-s3fs", f"{access_key}:{secret_key}\n", mode=0o600)
    s.run(f"mkdir -p {TEMPLATES_MOUNT}")
    fstab_line = (f"templates {TEMPLATES_MOUNT} fuse.s3fs "
                   f"_netdev,nofail,allow_other,umask=0022,sigv4,endpoint=garage,"
                   f"use_path_request_style,url=http://{drbd_ip}:{GARAGE_S3_PORT},"
                   "passwd_file=/etc/passwd-s3fs 0 0")
    s.run(f"grep -q ' {TEMPLATES_MOUNT} ' /etc/fstab || "
          f"echo '{fstab_line}' >> /etc/fstab")
    s.run(f"mount {TEMPLATES_MOUNT} 2>/dev/null || true")


# ── DRBD-mgmt fallback routes ──────────────────────────────────────────────

def install_routing_fallback(s: Ssh, peers: list[Node],
                              self_drbd_ip: str) -> None:
    s.run("sysctl -wq net.ipv4.ip_forward=1")
    s.run("sysctl -wq net.ipv4.conf.all.rp_filter=2 "
          "net.ipv4.conf.default.rp_filter=2")
    for p in peers:
        if p.drbd_ip == self_drbd_ip:
            continue
        s.run(f"ip route del {p.drbd_ip}/32 2>/dev/null; "
              f"ip route add {p.drbd_ip}/32 via {p.mgmt_ip} metric 200",
              check=False)


# ── Orchestration ──────────────────────────────────────────────────────────

def cmd_setup(args) -> None:
    nodes = parse_nodes(args.nodes)
    n = len(nodes)
    if n < 1:
        sys.exit("need at least one node")

    set_size = n if args.ec_set_size in (None, "auto") else int(args.ec_set_size)

    rustfs_ak = "rustfsadmin"
    rustfs_sk = "rustfssecret123"
    rpc_secret = secrets.token_hex(32)
    admin_token = secrets.token_hex(32)

    print(f"[+] {n} nodes, EC set size = {set_size}, "
          f"STANDARD=EC:{args.ec_standard}, "
          f"REDUCED_REDUNDANCY={'EC:'+str(args.ec_reduced) if args.ec_reduced is not None else 'disabled'}")

    rustfs_volumes = [f"http://{x.drbd_ip}:{RUSTFS_S3_PORT}/data" for x in nodes]

    print("[*] installing per-node binaries + configs in parallel")
    for x in nodes:
        s = Ssh(x.mgmt_ip)
        install_rustfs_node(s, rustfs_volumes, rustfs_ak, rustfs_sk,
                             set_size, args.ec_standard, args.ec_reduced)
        install_garage_node(s, x.drbd_ip, rpc_secret, admin_token,
                             replication_factor=args.garage_rf)
        install_s3backer(s)
        install_routing_fallback(s, nodes, x.drbd_ip)

    print("[*] starting RustFS cluster (all nodes simultaneously)")
    start_rustfs_cluster(nodes)

    print("[*] starting Garage daemons")
    for x in nodes:
        Ssh(x.mgmt_ip).run("systemctl restart garage.service", check=False)
    time.sleep(4)

    print("[*] forming Garage cluster + applying layout")
    garage_form_cluster(nodes, replication_factor=args.garage_rf,
                         capacity_gb=GARAGE_DATA_LV_GB - 5)

    print("[*] creating Garage scratch + templates buckets")
    scratch = garage_create_bucket(nodes[0], "scratch")
    templates_creds = garage_create_bucket(nodes[0], "templates",
                                             key_name="scratch-key")

    # If both buckets share the same key, templates_creds == scratch
    print(f"[+] Garage scratch key: {scratch['access_key']}")

    print("[*] mounting s3fs templates pool on every node")
    for x in nodes:
        install_s3fs_templates(Ssh(x.mgmt_ip), x.drbd_ip,
                                 scratch["access_key"], scratch["secret_key"])

    print("[*] creating RustFS critical+bulk buckets")
    bootstrap = nodes[0]
    # Create buckets via curl + sigv4 directly so we don't depend on aws-cli
    # being installed/discoverable on the dev box.
    for bucket in ("critical", "bulk"):
        # PUT with empty body to /<bucket>/ creates the bucket
        # Use s3api equivalent via aws-cli inheriting full env (so PATH is found)
        import os
        env = os.environ.copy()
        env.update({
            "AWS_ACCESS_KEY_ID": rustfs_ak,
            "AWS_SECRET_ACCESS_KEY": rustfs_sk,
            "AWS_DEFAULT_REGION": "us-east-1",
        })
        subprocess.run(
            ["aws", "--endpoint-url",
             f"http://{bootstrap.mgmt_ip}:{RUSTFS_S3_PORT}",
             "s3api", "create-bucket", "--bucket", bucket],
            env=env, capture_output=True, check=False)

    state = {
        "rustfs": {
            "endpoint_mgmt": f"http://{bootstrap.mgmt_ip}:{RUSTFS_S3_PORT}",
            "access_key": rustfs_ak, "secret_key": rustfs_sk,
            "ec_standard": args.ec_standard,
            "ec_reduced": args.ec_reduced,
            "ec_set_size": set_size,
        },
        "garage": {
            "endpoint_mgmt": f"http://{bootstrap.mgmt_ip}:{GARAGE_S3_PORT}",
            "access_key": scratch["access_key"],
            "secret_key": scratch["secret_key"],
            "replication_factor": args.garage_rf,
        },
        "nodes": [dataclasses.asdict(x) for x in nodes],
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    p = Path(__file__).resolve().parent.parent / "state" / "current-cluster.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(state, indent=2))
    p.chmod(0o600)
    print(f"[+] state saved to {p}")


def cmd_teardown(args) -> None:
    nodes = parse_nodes(args.nodes)
    print(f"[!] tearing down storage on {len(nodes)} nodes")
    for x in nodes:
        s = Ssh(x.mgmt_ip)
        # Stop services
        s.run("systemctl stop rustfs.service garage.service", check=False)
        # Unmount
        s.run("fusermount3 -u /var/lib/libvirt/templates 2>/dev/null", check=False)
        for mnt in ("/mnt/s3disk1", RUSTFS_DATA_DIR, GARAGE_DATA_DIR):
            s.run(f"umount {mnt} 2>/dev/null", check=False)
        # Remove fstab entries
        s.run(r"sed -i '/rustfs-data\|garage-data\|s3backer\|templates fuse.s3fs/d' /etc/fstab")
        # Remove LVs
        s.run("lvremove -f almalinux/rustfs-data almalinux/garage-data 2>/dev/null",
              check=False)
        # Disable units
        s.run("systemctl disable rustfs.service garage.service", check=False)
        s.run("rm -f /etc/systemd/system/rustfs.service "
              "/etc/systemd/system/garage.service /etc/default/rustfs "
              "/etc/garage.toml /etc/passwd-s3fs")
        s.run("systemctl daemon-reload")
        # Drop fallback routes (best-effort)
        for p in nodes:
            if p.drbd_ip == x.drbd_ip:
                continue
            s.run(f"ip route del {p.drbd_ip}/32 2>/dev/null", check=False)
    print("[+] teardown complete (OS + thin pool intact)")


def cmd_status(args) -> None:
    nodes = parse_nodes(args.nodes)
    for x in nodes:
        s = Ssh(x.mgmt_ip)
        rustfs = s.run("systemctl is-active rustfs.service",
                        check=False).strip()
        garage = s.run("systemctl is-active garage.service",
                        check=False).strip()
        print(f"{x.mgmt_ip} ({x.drbd_ip}): rustfs={rustfs} garage={garage}")


def parse_nodes(spec: str) -> list[Node]:
    out: list[Node] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            sys.exit(f"node spec needs mgmt:drbd, got: {chunk}")
        mgmt, drbd = chunk.split(":", 1)
        out.append(Node(mgmt_ip=mgmt, drbd_ip=drbd))
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pa = sub.add_parser("setup")
    pa.add_argument("--nodes", required=True,
                    help="comma list of mgmt:drbd pairs")
    pa.add_argument("--ec-standard", type=int, default=2,
                    help="parity for STANDARD storage class (default 2)")
    pa.add_argument("--ec-reduced", type=int, default=1,
                    help="parity for REDUCED_REDUNDANCY (default 1, "
                         "set to -1 to disable)")
    pa.add_argument("--ec-set-size", default="auto",
                    help='"auto" = node count, or specific N')
    pa.add_argument("--garage-rf", type=int, default=1,
                    help="Garage replication_factor (1=scratch, 2=bulk-like)")
    pa.set_defaults(func=cmd_setup)

    pb = sub.add_parser("teardown")
    pb.add_argument("--nodes", required=True)
    pb.set_defaults(func=cmd_teardown)

    pc = sub.add_parser("status")
    pc.add_argument("--nodes", required=True)
    pc.set_defaults(func=cmd_status)

    args = p.parse_args()
    if hasattr(args, "ec_reduced") and args.ec_reduced == -1:
        args.ec_reduced = None
    args.func(args)


if __name__ == "__main__":
    main()
