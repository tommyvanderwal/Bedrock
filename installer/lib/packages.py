"""Package installation for Bedrock nodes.

See `packages.md` (next to this file) for the full operational spec —
invariants, where state lives, design rationale, sources.

Every Bedrock node — whether it's the initial mgmt master or a peer
that joined via `bedrock join` — gets the FULL package set installed
here. This includes the Python deps the mgmt FastAPI app needs
(paramiko, fastapi, uvicorn, websockets, pydantic, python-multipart),
because any node may take over the mgmt role on failover and must be
ready to start `bedrock-mgmt.service` immediately. (See lessons-log L17.)

OS base: **AlmaLinux 10.1**. The previous "NOT 10" caveat in
BEDROCK.md was tied to early 10.0 / kernel 6.12 DRBD-kmod gaps; ELRepo
now ships kmod-drbd9x-9.3.x built against the el10_1 kernel, which is
what we standardise on for v1.0.
"""

import subprocess
from pathlib import Path

ELREPO_URL = "https://www.elrepo.org/elrepo-release-10.el10.elrepo.noarch.rpm"

# install.sh stages the bundled ISO payload here; rpms/ holds the
# pinned ELRepo + DRBD packages so we don't depend on elrepo.org's
# slow mirrors for `bedrock bootstrap`. See iso-build/build-iso.sh
# step 2b for what gets dropped in.
LOCAL_PAYLOAD_RPMS = Path("/var/lib/bedrock-install/rpms")

BASE_PACKAGES = [
    "qemu-kvm",
    "libvirt",
    "libvirt-daemon-kvm",
    "virt-install",
    "virt-v2v",
    "libguestfs-tools",
    "libguestfs-winsupport",
    "qemu-guest-agent",
    "lvm2",
    "xfsprogs",
    "tuned",
    "python3-pip",
    "iputils",
    "cockpit",
    "cockpit-machines",
]

DRBD_PACKAGES = [
    "kmod-drbd9x",
    "drbd9x-utils",
]

# Python packages required by the mgmt FastAPI app (mgmt/app.py).
# Installed on EVERY node so any node can take over the mgmt role
# without runtime pip install. Pinning is intentionally loose; bedrock
# CLI evolves with whatever fastapi/pydantic versions are current.
MGMT_PYTHON_PACKAGES = [
    "fastapi",
    "uvicorn",
    "paramiko",
    "websockets",
    "pydantic",
    "python-multipart",
    # lib/witness.py uses msgpack for the Echo wire format
    "msgpack",
]


def run(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {r.stderr}")
    return r.stdout.strip()


def _rpm_installed(pkg: str) -> bool:
    r = subprocess.run(["rpm", "-q", pkg], capture_output=True)
    return r.returncode == 0


def _local_rpms_dir():
    """Return Path to the bundled rpms dir if it has any RPMs, else None."""
    if LOCAL_PAYLOAD_RPMS.is_dir() and any(LOCAL_PAYLOAD_RPMS.glob("*.rpm")):
        return LOCAL_PAYLOAD_RPMS
    return None


def _local_rpm(rpms_dir: Path, pkg: str):
    """Find the first matching <pkg>-*.rpm file in rpms_dir, or None."""
    matches = sorted(rpms_dir.glob(f"{pkg}-*.rpm"))
    return matches[0] if matches else None


def install_base():
    """Install base packages required on every Bedrock node."""
    local_rpms = _local_rpms_dir()

    # ELRepo registration (the .repo file + GPG key). The local copy is
    # preferred because elrepo.org's HTTP mirrors are unreliable and slow;
    # this is just the metadata RPM, not the kmod, so falling back to the
    # network is harmless.
    if not _rpm_installed("elrepo-release"):
        local = _local_rpm(local_rpms, "elrepo-release") if local_rpms else None
        src = str(local) if local else ELREPO_URL
        print(f"  Installing ELRepo from {'bundled payload' if local else 'elrepo.org'}...")
        run(f"dnf install -y -q {src}")

    # DRBD (kmod + utils) — install the pinned RPMs from the payload when
    # available. ELRepo's mirrors are the slowest leg of the bootstrap, and
    # we already ship the exact tested versions, so going to the network
    # for these is pure latency. If the payload is missing for some reason
    # (manual installer, old image), fall through to the dnf path below
    # which will pull them from the elrepo repo we just registered.
    drbd_remaining = [p for p in DRBD_PACKAGES if not _rpm_installed(p)]
    if drbd_remaining and local_rpms:
        files = [str(_local_rpm(local_rpms, p)) for p in drbd_remaining
                 if _local_rpm(local_rpms, p)]
        if len(files) == len(drbd_remaining):
            print(f"  Installing DRBD from bundled payload "
                  f"({len(files)} RPMs, ~{sum(p.stat().st_size for p in map(Path, files))//1024} KB)...")
            run(f"dnf install -y -q {' '.join(files)}")
            drbd_remaining = []

    # Everything else (BASE_PACKAGES + any DRBD that didn't come from the
    # payload) comes from the upstream repos.
    to_install = [p for p in BASE_PACKAGES if not _rpm_installed(p)] + drbd_remaining
    if to_install:
        print(f"  Installing {len(to_install)} packages from network repos...")
        run(f"dnf install -y -q {' '.join(to_install)}")

    # Load DRBD module
    run("modprobe drbd 2>/dev/null || true", check=False)
    run("echo drbd > /etc/modules-load.d/drbd.conf", check=False)

    # libvirtd is NOT enabled/started here. systemd auto-starts only
    # bedrock-d, bedrock-rqlited(-arbiter), weed-*, and the obs stack;
    # libvirtd (and DRBD as an auto-actor) come up imperatively from
    # bedrock-d's boot orchestrator AFTER role/quorum is established, so
    # no VM or DRBD resource acts before quorum. install.sh disables both
    # units; we must not re-enable libvirtd here. (BAD-2/3 boot ownership,
    # finding I-02.)

    # Enable cockpit for web console access on port 9090
    run("systemctl enable --now cockpit.socket >/dev/null 2>&1", check=False)
    # Allow root login to cockpit (default: blocked)
    run("sed -i '/^root$/d' /etc/cockpit/disallowed-users 2>/dev/null", check=False)

    # Install mgmt-app Python deps on EVERY node so any node can take
    # over the mgmt role on failover without a runtime pip install.
    # (Lessons-log L17.)
    print(f"  Installing mgmt-app Python deps "
          f"({', '.join(MGMT_PYTHON_PACKAGES)})...")
    run(f"pip3 install -q {' '.join(MGMT_PYTHON_PACKAGES)} "
        f"2>&1 | tail -2", check=False)

    print("  Base packages installed.")
