# Build & install from the bedrock-install ISO

A single bootable ISO that turns a fresh box (real hardware or testbed
VM) into a bedrock-ready node with **zero network access required**
during install. AlmaLinux 10.1 + the bedrock storage layout + every
RPM, wheel, and binary bedrock needs — all baked into one file.

**Triggered by:**

- Build host: `installer/iso-build/build-iso.sh`
- Real hardware: `dd if=bedrock-install-almalinux-10.iso of=/dev/sdX bs=4M`
  to a USB stick, boot from USB
- Testbed: `virt-install --cdrom bedrock-install-almalinux-10.iso …`

**Source:**
`installer/iso-build/build-iso.sh`,
`installer/iso-build/bedrock-almalinux-10.ks`,
`installer/lib/packages.py` (offline branch).

## What goes into the ISO

```
bedrock-install-almalinux-10.iso  (≈9.5 GB with full bundle)
├── EFI/, boot/, isolinux/, images/      ← AlmaLinux 10 DVD contents
├── BaseOS/Packages/, AppStream/Packages/  ← stock OS RPMs
├── ks.cfg                                ← bedrock kickstart, auto-loaded
└── bedrock/                              ← bedrock payload
    ├── install.sh                        ← runs at first boot
    ├── bedrock                           ← operator CLI
    ├── bedrock-rust                      ← cluster-protocol daemon
    ├── bedrock-fence-watchdog            ← independent fence reaper
    ├── mgmt.tar.gz                       ← FastAPI + Svelte build
    ├── kopia                             ← backup repo client
    ├── lib/*.py                          ← installer libraries
    ├── configs/*                         ← systemd units, etc.
    ├── rpms/                             ← ELRepo: kmod-drbd9x + utils
    ├── wheels/                           ← Python: fastapi, uvicorn,
    │                                       paramiko, websockets,
    │                                       pydantic, multipart, msgpack
    ├── virtio-win.iso                    ← Windows VM driver disk
    └── alpine.qcow2                      ← cattle-VM default boot image
```

The grub menu's `Install AlmaLinux 10.1` entry is rewritten by the
build script to include `inst.ks=cdrom:/dev/sr0:/ks.cfg`, so anaconda
runs unattended. `inst.stage2=hd:LABEL=Bedrock-Install-10` matches the
new volume label, so the installer finds its own stage2 image
without falling back to network.

## What the install does

```
  T=0    Operator inserts USB stick / virt-install --cdrom
         │
         │ Firmware (BIOS or UEFI) boots the El Torito image
         │
  T+5s   GRUB menu renders. 1-second timeout — auto-picks
         "Install AlmaLinux 10.1" with our kickstart args.
         │
  T+30s  Anaconda kernel + initrd loaded, stage2 mounts the
         /run/install/repo from the ISO.
         │
  T+60s  Kickstart parsed:
         │   - clearpart --all
         │   - 500 MB /boot/efi (vfat)
         │   - 1 GB /boot (xfs)
         │   - rest = LVM PV → bedrock VG
         │   - thinpool fills the VG
         │   - 16 GB thin root LV (xfs) for /
         │   - no swap
         │
  T+~3m  AlmaLinux 10 base packages installed from
         the bundled DVD repo (no network).
         │
  T+~5m  %post script runs in chroot:
         │   - copies /run/install/repo/bedrock → /var/lib/bedrock-install/
         │   - writes /etc/systemd/system/bedrock-firstboot.service
         │   - enables it
         │   - writes /etc/motd with next-step guidance
         │
  T+~6m  Anaconda reboots the VM (reboot --eject — VM ejects the ISO
         so it boots from the just-installed disk).
         │
  T+~7m  AlmaLinux 10 boots from disk. systemd reaches multi-user.target.
         │
  T+~8m  bedrock-firstboot.service runs:
         │   - BEDROCK_REPO=file:///var/lib/bedrock-install
         │   - bash /var/lib/bedrock-install/install.sh
         │   - install.sh fetches every asset via curl file:///…
         │   - packages.py prefers bundled RPMs + wheels over network
         │   - bedrock CLI + bedrock-rust unit installed
         │   - mgmt.tar.gz extracted to /opt/bedrock/mgmt
         │   - touches /var/lib/bedrock-install/.bootstrap-done
         │   - disables itself
         │
  T+~9m  Operator login prompt + motd:
         │   ╔════════════════════════════╗
         │   ║  Bedrock node — fresh install    ║
         │   ║  Next step:                      ║
         │   ║    bedrock init                  ║
         │   ║    bedrock join HOST             ║
         │   ╚══════════════════════════════════╝
         │
         │ At this point: no network was needed for any of the above.
         │ Network is needed from `bedrock init` onward (peers talk).
```

## Build the ISO

```bash
cd installer/iso-build
./build-iso.sh                 # full build, 8.5 GB DVD base + payload
./build-iso.sh --quick         # boot.iso base + skip large bundles
                               # (≈1 GB output, needs network at install)
./build-iso.sh --skip-payload-refresh   # reuse cached payload/
```

First-time full build: ~5 min download (DVD ISO + ELRepo RPMs +
wheels + virtio-win + alpine) + ~2 min repack = ~7 min wall-clock.

Subsequent builds (`--skip-payload-refresh`): ~2 min repack only.

Output: `installer/iso-build/output/bedrock-install-almalinux-10.iso`
plus a SHA256 line printed at end of run.

## Deploy the ISO

### Real hardware

```bash
# After build, on the dev box:
sudo dd if=bedrock-install-almalinux-10.iso of=/dev/sdX bs=4M status=progress
sync
```

Eject USB, plug into target node, boot from USB (one-time BIOS/UEFI
boot menu — usually F12/F11/Esc on most boards). Walk away ~8 min.

### Testbed

```bash
# After build:
testbed/spawn.py up 4
```

`spawn.py` v1.0+ uses the bedrock-install ISO instead of the cloud
image, so testbed installs go through the exact same path as
production. (Earlier versions used cloud-image + post-install carve;
that path stays in `tier_storage.carve_pv_from_boot_disk_tail` as a
greenfield-from-cloud-image fallback.)

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `dracut-initqueue` timeout, "Could not find LABEL=Bedrock-Install-10" | dd to USB didn't complete or USB is corrupt | Re-write USB; verify with `sha256sum`. |
| Anaconda complains "no kickstart found" | Bootloader edit failed during build | Re-run `build-iso.sh`; check `/EFI/BOOT/grub.cfg` in the output ISO contains `inst.ks=…`. |
| Install hangs at "Storage configuration" | Kickstart partitioning failed (disk too small) | Need ≥20 GB disk for /boot + thinpool minimum. |
| Boot loops after install | Initramfs missing LVM modules | `dracut --add lvm -f` from a rescue console; sometimes happens with custom AlmaLinux respins. |
| `bedrock-firstboot.service` failed | Look at `journalctl -u bedrock-firstboot` — usually a missing payload file (build pipeline issue) | Re-run build-iso.sh. |
| `dnf install -y kmod-drbd9x` fails post-install | Bundled RPMs not registered as a local repo | Manual: `dnf install /var/lib/bedrock-install/rpms/*.rpm` |

## Update cadence

The ISO needs a refresh whenever:
- AlmaLinux ships a new minor version (10.1 → 10.2 → …) — pulls fresh DVD
- bedrock-rust binary is rebuilt — pulls from `installer/binaries/`
- mgmt.tar.gz is repacked — pulls from `installer/`
- Kopia release we pin to changes — bump `KOPIA_VERSION` in `build-iso.sh`
- ELRepo's kmod-drbd9x version changes — bump in `build-iso.sh`

A simple CI step (`./build-iso.sh && upload-to-cdn.sh`) gives a fresh
ISO per bedrock release.
