# installer/lib/packages.py

Installs the OS package set every Bedrock node needs to host VMs and run the
mgmt stack, on an AlmaLinux 10.1 base. It runs from the install path (e.g.
`bedrock bootstrap`) and registers ELRepo, lays down the DRBD kmod + utils, the
virtualization / LVM / Cockpit base, and the Python deps the mgmt FastAPI app
imports. The full set goes onto **every** node (initial master and joiners
alike) so any node can take over the mgmt role on failover without a runtime
package fetch. `install_base()` is the single public entry point.

## Functions / Classes

### `run(cmd, check=True) -> str`
Run a shell command and return its trimmed stdout.
- **In:** `cmd` — shell string (executed with `shell=True`); `check` — raise on
  non-zero exit when true.
- **Out:** stdout (stripped). Raises `RuntimeError` carrying the command and its
  stderr when `check` is true and the command exits non-zero. Side effect: the
  subprocess itself (`dnf`, `pip3`, `systemctl`, `modprobe`, `sed`, `echo`).

### `install_base() -> None`
Install the complete base package set on this node.
- **In:** none.
- **Out:** none. Side effects: registers the ELRepo release RPM; installs DRBD
  (`kmod-drbd9x`, `drbd9x-utils`), `BASE_PACKAGES`, and `MGMT_PYTHON_PACKAGES`;
  loads the `drbd` kernel module and writes `/etc/modules-load.d/drbd.conf`;
  enables and starts `cockpit.socket`; strips `root` from
  `/etc/cockpit/disallowed-users`; pip-installs the mgmt Python deps. Prints
  progress lines to stdout.

### Module constants
- `ELREPO_URL` — network URL for the ELRepo release RPM
  (`elrepo-release-10.el10`).
- `LOCAL_PAYLOAD_RPMS` — `/var/lib/bedrock-install/rpms`, where the bundled ISO
  payload stages pinned ELRepo + DRBD RPMs.
- `BASE_PACKAGES` — qemu-kvm, libvirt, libvirt-daemon-kvm, virt-install,
  virt-v2v, libguestfs-tools, libguestfs-winsupport, qemu-guest-agent, lvm2,
  xfsprogs, tuned, python3-pip, iputils, cockpit, cockpit-machines.
- `DRBD_PACKAGES` — `kmod-drbd9x`, `drbd9x-utils`.
- `MGMT_PYTHON_PACKAGES` — fastapi, uvicorn, paramiko, websockets, pydantic,
  python-multipart, msgpack (msgpack is the Echo wire format used by
  `witness.py`).

### Private helpers
- `_rpm_installed(pkg)` — true if `rpm -q pkg` exits zero.
- `_local_rpms_dir()` — returns `LOCAL_PAYLOAD_RPMS` if it exists and holds any
  `*.rpm`, else `None`.
- `_local_rpm(rpms_dir, pkg)` — first `pkg-*.rpm` match in the dir (sorted), or
  `None`.

## How it works

`install_base()` runs four stages in order. Each install is filtered by
`_rpm_installed()` so a re-run installs only what is missing — the function is
idempotent.

```
  install_base()
    │   local_rpms = _local_rpms_dir()   (bundled payload, or None)
    │
    ├─ 1. ELRepo release RPM   (skip if elrepo-release already installed)
    │        payload has it ──► dnf install <bundled .rpm>
    │        else            ──► dnf install ELREPO_URL
    │
    ├─ 2. DRBD (kmod-drbd9x, drbd9x-utils)
    │        drbd_remaining = DRBD pkgs not yet installed
    │        if drbd_remaining AND payload carries ALL of them:
    │             dnf install <bundled .rpm files> ; drbd_remaining = []
    │        else: leave drbd_remaining for stage 3
    │
    ├─ 3. dnf install  (missing BASE_PACKAGES) + drbd_remaining
    │        modprobe drbd                      (best-effort)
    │        echo drbd > /etc/modules-load.d/drbd.conf   (best-effort)
    │
    └─ 4. systemctl enable --now cockpit.socket          (best-effort)
         sed -i remove root from disallowed-users        (best-effort)
         pip3 install MGMT_PYTHON_PACKAGES               (best-effort)
```

**Payload-vs-network choice.** The bundled `/var/lib/bedrock-install/rpms` is
preferred for both ELRepo and DRBD; the network URL / repo is the fallback. For
DRBD the payload is used **only if it carries every still-missing DRBD RPM**
(`len(files) == len(drbd_remaining)`); a partial payload falls through entirely
to the dnf path so the DRBD set installs from one source, pulled from the ELRepo
repo registered in stage 1.

**Failure handling.** The load-bearing installs run with the default
`check=True`, so a failed ELRepo / DRBD / base install raises `RuntimeError` and
aborts. The trailing steps run `check=False` and tolerate failure: `modprobe
drbd`, the `modules-load.d` write, the cockpit enable, the `disallowed-users`
edit, and the `pip3 install` (whose output is piped through `tail -2`).

**What it deliberately does not do.** It does not enable or start `libvirtd`,
and it does not bring DRBD up as an actor. DRBD here is only the kernel-module
load plus the `modules-load.d` entry. libvirtd and DRBD resource activation come
up imperatively from bedrock-d's boot orchestrator after the node's quorum role
is established, so no VM or DRBD resource acts before quorum.

## Why

The full package set — including the mgmt FastAPI Python deps — lands on every
node so any node can take over the mgmt role on failover without a runtime
`pip install`. Bundled RPMs are preferred for ELRepo and DRBD because those are
the slowest leg of the bootstrap and the payload ships the exact tested
versions; the network fallback is harmless for the lightweight ELRepo metadata
RPM and a complete safety net when no payload is staged.
