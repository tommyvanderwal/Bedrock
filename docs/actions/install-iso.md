# Build & install from the Bedrock installer ISOs

Two ISO variants per release, both produced by the same build script
and kickstart. See `docs/install-and-iso.md` for the architecture
and naming convention.

| ISO | Size | Source | Internet at install? | Use when |
|---|---|---|---|---|
| `bedrock-installer-<version>.iso` | ~1 GB | AlmaLinux 10 boot.iso | Yes | Most installs. Default visible download. |
| `bedrock-installer-<version>-offline.iso` | ~5 GB | AlmaLinux 10 DVD | No | Airgap / MSP-ship-to-site. Operator-control deliverable. |

Same kickstart partitioning, same first-boot UX, same end state:
Bedrock installed, services prepared, waiting for the operator to
run `bedrock init` (new cluster) or `bedrock join` (joiner — mDNS-
discovers existing clusters on the LAN; pass `bedrock join <ip>`
explicitly if mDNS is blocked).

**Triggered by:**

- Build host: `installer/iso-build/build-iso.sh --version <version>`
- Real hardware: `dd if=bedrock-installer-<version>.iso of=/dev/sdX bs=4M`
  to a USB stick, boot from USB
- Testbed: `virt-install --cdrom bedrock-installer-<version>-offline.iso …`
  (testbed uses the offline variant to avoid S3 dependency)

**Source:**
- `installer/iso-build/build-iso.sh` — orchestrates both variants
- `installer/iso-build/bedrock-almalinux-10.ks` — single kickstart template,
  build script sed-substitutes placeholders per variant
- `installer/iso-build/payload/` — bundled into the offline ISO

## What goes into each ISO

### Net ISO (`bedrock-installer-<version>.iso`)

```
bedrock-installer-dev.iso  (≈1 GB)
├── EFI/, boot/, isolinux/, images/        ← AlmaLinux 10 boot.iso content
└── ks.cfg                                  ← Bedrock kickstart
                                              (fetches packages from
                                               Alma mirror + Bedrock from
                                               https://bedrock.fsn1.your-objectstorage.com/<version>)
```

First-boot service runs:
```
curl -fsSL ${BEDROCK_REPO}/install.sh -o /tmp/bedrock-install.sh
bash /tmp/bedrock-install.sh
```
where `BEDROCK_REPO` matches the version-baked filename — an ISO
named `…-v0.8.iso` fetches from `/v0.8/`, never from `/dev/`.

### Offline ISO (`bedrock-installer-<version>-offline.iso`)

```
bedrock-installer-dev-offline.iso  (≈5 GB)
├── EFI/, boot/, isolinux/, images/        ← AlmaLinux 10 DVD content
├── BaseOS/Packages/, AppStream/Packages/  ← stock OS RPMs (offline-usable)
├── ks.cfg                                  ← Bedrock kickstart (cdrom mode)
└── bedrock/                                ← full Bedrock payload
    ├── install.sh                          ← runs at first boot from /var/lib/bedrock-install
    ├── bedrock                             ← operator CLI
    ├── bedrock-d                           ← unified daemon
    ├── bedrock-{cert-refresh,mdns,redirect}
    ├── mgmt.tar.gz                         ← FastAPI + Svelte build
    ├── bedrock_d.tar.gz                    ← unified daemon code tree
    ├── kopia                               ← backup repo client
    ├── lib/*.py                            ← installer libraries
    ├── configs/*                           ← systemd units, etc.
    ├── rpms/                               ← ELRepo: kmod-drbd9x + utils
    ├── wheels/                             ← Python deps (httpx, fastapi, etc.)
    ├── virtio-win.iso                      ← Windows VM driver disk
    └── alpine.qcow2                        ← cattle-VM default boot image
```

First-boot service runs `/var/lib/bedrock-install/install.sh` with
`BEDROCK_REPO=file:///var/lib/bedrock-install`. Zero network calls.

## First-boot UX (both variants)

Single-disk happy path, blank disk: **zero prompts.** Kernel boots,
Anaconda runs from kickstart, partitions per the Bedrock layout
(`bedrock` VG, thinpool, root LV), installs packages, reboots into a
Bedrock-ready node.

Multi-disk box: the kickstart's `%pre` script lists disks and prompts
on the install console for which to use. Single question, no other UI.

Disk with existing partitions: the kickstart's `%pre` script halts
before any wipe and shows the partition layout with a single consent
prompt:

```
  Disk /dev/sda has existing partitions:

    sda     465G  disk
    ├─sda1  600M  part  /boot/efi
    ├─sda2    1G  part  /boot
    └─sda3  464G  part  (LVM PV — VG "almalinux")

  Installing Bedrock will erase all of them. There is no undo.

  Type 'yes' to wipe and proceed, anything else to cancel:
```

No colors louder than the default terminal; one screen; one prompt.

End state: bedrock CLI installed, daemons not started.
Login banner instructs the operator to run `bedrock init` (new
cluster) or `bedrock join <master>` (joiner). The ISO **does not**
run `bedrock init` automatically — joiners outnumber first-node
installs at any operating MSP, and the ISO has no way to know which
case the operator is in.

## Build the ISOs

```bash
cd installer/iso-build
./build-iso.sh                              # both variants, version=dev
./build-iso.sh --version v0.8               # both variants, version=v0.8
./build-iso.sh --version dev --variant net  # just the net ISO
./build-iso.sh --variant offline            # just the offline ISO
./build-iso.sh --testbed                    # bake in the dev-box SSH key
./build-iso.sh --skip-payload-refresh       # reuse cached payload/
```

First-time full build: ~5 min download (DVD ISO + boot.iso + ELRepo
RPMs + wheels + virtio-win + alpine) + ~2 min repack per variant.

Subsequent builds (`--skip-payload-refresh`): ~2 min per variant
once source ISOs are cached.

Outputs:
- `installer/iso-build/output/bedrock-installer-<version>.iso`
- `installer/iso-build/output/bedrock-installer-<version>-offline.iso`

Each printed with size + sha256 at end of run.

## Deploy

### Real hardware

```bash
sudo dd if=bedrock-installer-dev.iso of=/dev/sdX bs=4M status=progress
sync
```

Plug USB into target node, boot from USB, walk away. Single-disk
clean box = no prompts. Multi-disk = one prompt for disk selection.
Existing partitions = one prompt for wipe confirmation.

### Testbed

```bash
testbed/spawn.py up 4
```

`spawn.py` uses `bedrock-installer-dev-offline.iso` (offline variant,
no S3 dependency at install time).

## Publish to S3

```bash
# Build first
installer/iso-build/build-iso.sh --version dev

# Then push both ISOs + the install repo to S3 /dev/
testbed/publish-to-s3.sh --prefix dev --with-iso --allow-dirty
```

For releases, use `--prefix v0.8 --tag` (refuses `--allow-dirty`,
treats prefix as immutable unless `--allow-tag-overwrite`).

## Failure modes

| Symptom | Cause | Recovery |
|---|---|---|
| Anaconda "no kickstart found" | Bootloader edit failed during build | Re-run build-iso.sh; verify `/EFI/BOOT/grub.cfg` in the ISO has `inst.ks=…` |
| Install halts at disk-prompt asking for input | Multi-disk box or existing partitions detected — by design | Answer the prompt on the install console |
| Net ISO firstboot fails: `curl: (6) Could not resolve host` | No DHCP / no network at first boot | Either use the offline ISO, or attach the box to a network with DHCP before first boot |
| Net ISO firstboot fails: HTTP 404 on install.sh | Version prefix doesn't exist in S3 | Build matches a published prefix? Net ISO's `BEDROCK_REPO` is baked at build time; for unpublished dev work use the offline ISO |
| `dnf install -y kmod-drbd9x` fails post-install | Offline ISO: bundled RPMs not registered; Net ISO: ELRepo mirrors unreachable | Offline: `dnf install /var/lib/bedrock-install/rpms/*.rpm`. Net: check ELRepo reachability |
| Boot loops after install | Initramfs missing LVM modules | `dracut --add lvm -f` from a rescue console — rare on stock Alma |

## Update cadence

The ISOs need a rebuild whenever:
- AlmaLinux ships a new minor version (10.1 → 10.2) — Alma source ISO changes
- Anything in `installer/` or `bedrock_d/` or `mgmt/` changes (offline ISO bundles them)
- `Kopia`, `weed`, `rqlited`, or other pinned binary versions change
- ELRepo's `kmod-drbd9x` ships a new release

For `dev` builds, rebuild + republish daily-ish during active
development. For tagged releases (`v0.8`, `v1.0`), each release
gets a fresh ISO once and the prefix is treated as immutable.

## See also

- `docs/install-and-iso.md` — architecture, design principles, ISO
  naming convention.
- `installer/install.sh` — the post-install bootstrap that runs at
  first boot. Includes the `/home` reclaim recovery path for
  operators who run `install.sh` on already-installed Alma boxes
  (i.e. didn't use the ISO).
