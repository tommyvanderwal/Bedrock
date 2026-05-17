#!/usr/bin/env bash
# Build the bedrock-install-almalinux-10.iso — a single bootable ISO
# that:
#   1. Installs AlmaLinux 10.1 unattended via the bundled kickstart
#   2. Lays out the disk per docs/01-storage-stack.md (single VG, thin
#      pool, root as a thin LV, no swap)
#   3. Stages the bedrock payload (binaries, RPMs, Python wheels,
#      mgmt.tar.gz, virtio-win.iso, alpine.qcow2) into
#      /var/lib/bedrock-install on the target system
#   4. Arms a one-shot first-boot service that runs install.sh
#      against the local payload — bedrock bootstrap completes
#      without ever reaching the network.
#
# Output: ISO that boots on testbed VMs (virt-install --cdrom) AND
# real hardware (dd to USB stick, boot from USB). Same install
# experience either way.
#
# Build host requirements:
#   - xorriso  (for ISO repack)
#   - curl, gunzip, sha256sum, find, sed
#   - dnf or rpm  (only if we need to fetch ELRepo RPMs without a
#                  pre-populated cache; otherwise just curl)
#   - ~25 GB free disk for source ISO + extracted contents + output
#
# Use:
#   ./build-iso.sh                          # full build
#   ./build-iso.sh --skip-payload-refresh   # reuse cached payload
#   ./build-iso.sh --quick                  # boot.iso + skip large
#                                           # bundles (virtio-win etc.)
#                                           # for fast dev iteration

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$INSTALLER/.." && pwd)"

BUILD_DIR="$HERE/build"
PAYLOAD_DIR="$HERE/payload"
OUT_DIR="$HERE/output"
KS_FILE="$HERE/bedrock-almalinux-10.ks"

mkdir -p "$BUILD_DIR" "$PAYLOAD_DIR" "$OUT_DIR"

# ── Argument parsing ──────────────────────────────────────────────
SKIP_PAYLOAD_REFRESH=0
QUICK=0
for arg in "$@"; do
    case "$arg" in
        --skip-payload-refresh) SKIP_PAYLOAD_REFRESH=1 ;;
        --quick) QUICK=1 ;;
        *) echo "unknown arg: $arg" >&2; exit 1 ;;
    esac
done

# ── Source ISO ────────────────────────────────────────────────────
ALMA_VERSION="10"
if [ "$QUICK" -eq 1 ]; then
    SRC_KIND="boot"   # 926 MB, network install
else
    SRC_KIND="dvd"    # 8.5 GB, fully offline
fi
SRC_ISO_NAME="AlmaLinux-${ALMA_VERSION}-latest-x86_64-${SRC_KIND}.iso"
SRC_ISO="$BUILD_DIR/$SRC_ISO_NAME"
SRC_URL="https://repo.almalinux.org/almalinux/${ALMA_VERSION}/isos/x86_64/$SRC_ISO_NAME"

OUT_ISO="$OUT_DIR/bedrock-install-almalinux-${ALMA_VERSION}.iso"

# ── Step 1: download AlmaLinux source ISO if missing ──────────────
echo "[bedrock-iso] step 1/5: source ISO"
if [ ! -f "$SRC_ISO" ]; then
    echo "  fetching $SRC_URL ..."
    curl -fSL --progress-bar -o "$SRC_ISO" "$SRC_URL"
fi
echo "  source: $SRC_ISO ($(du -h "$SRC_ISO" | cut -f1))"

# ── Step 2: refresh payload directory ─────────────────────────────
# Layout:
#   payload/
#     install.sh                ← from installer/
#     bedrock                   ← from installer/
#     bedrock-rust              ← from installer/binaries/
#     bedrock-fence-watchdog    ← from installer/
#     mgmt.tar.gz               ← from installer/
#     lib/*.py                  ← from installer/lib/
#     configs/*                 ← from installer/configs/
#     rpms/                     ← ELRepo packages (kmod-drbd9x +
#                                  drbd9x-utils + elrepo-release)
#     wheels/                   ← Python wheels for mgmt deps
#     virtio-win.iso            ← Windows VM driver disk
#     alpine.qcow2              ← cattle-VM default boot image
#     kopia                     ← Kopia binary

if [ "$SKIP_PAYLOAD_REFRESH" -eq 0 ]; then
    echo "[bedrock-iso] step 2/5: refreshing payload"
    rm -rf "$PAYLOAD_DIR"
    mkdir -p "$PAYLOAD_DIR"/{binaries,lib,configs,rpms,wheels}

    # 2a. Bedrock files. Layout mirrors what install.sh expects from
    #     a BEDROCK_REPO-style HTTP server — same paths whether the
    #     operator's BEDROCK_REPO is a URL or `file:///…`. So
    #     bedrock-rust goes under binaries/ (install.sh fetches
    #     ${BEDROCK_REPO}/binaries/bedrock-rust), lib modules under
    #     lib/, configs/ holds the systemd units.
    cp "$INSTALLER/install.sh"                 "$PAYLOAD_DIR/install.sh"
    cp "$INSTALLER/bedrock"                    "$PAYLOAD_DIR/bedrock"
    cp "$INSTALLER/bedrock-net"                "$PAYLOAD_DIR/bedrock-net"
    cp "$INSTALLER/bedrock-fence-watchdog"     "$PAYLOAD_DIR/bedrock-fence-watchdog"
    cp "$INSTALLER/bedrock-cert-refresh"       "$PAYLOAD_DIR/bedrock-cert-refresh"
    cp "$INSTALLER/bedrock-mdns"               "$PAYLOAD_DIR/bedrock-mdns"
    cp "$INSTALLER/bedrock-redirect"           "$PAYLOAD_DIR/bedrock-redirect"
    cp "$INSTALLER/mgmt.tar.gz"                "$PAYLOAD_DIR/mgmt.tar.gz"
    # All cluster-time binaries (mgmt: victoria-metrics, victoria-logs,
    # node_exporter; rust daemon: bedrock-rust). install.sh copies the
    # rust daemon to /usr/local/bin; mgmt_install + agent_install pull
    # the rest from $BEDROCK_REPO/binaries/ when `bedrock init` /
    # `bedrock join` runs.
    for b in bedrock-rust victoria-metrics victoria-logs node_exporter \
             vmagent vlagent vmbackup vmrestore rqlited weed; do
        if [ -f "$INSTALLER/binaries/$b" ]; then
            cp "$INSTALLER/binaries/$b" "$PAYLOAD_DIR/binaries/$b"
        else
            # Auto-fetch missing upstream binaries we know how to find.
            # Each upstream URL is pinned to a specific version below;
            # bump as needed when upgrading. Kept here (not in git)
            # because some of these (weed at 144 MB, vmbackup at 65 MB)
            # exceed GitHub's per-file size cap.
            case "$b" in
                weed)
                    echo "  fetching weed v4.25 ($b not in git)..."
                    curl -fSL --progress-bar -o /tmp/weed.tgz \
                        "https://github.com/seaweedfs/seaweedfs/releases/download/4.25/linux_amd64.tar.gz" \
                        && tar xzf /tmp/weed.tgz -C /tmp \
                        && install -m 0755 /tmp/weed "$PAYLOAD_DIR/binaries/$b" \
                        && install -m 0755 /tmp/weed "$INSTALLER/binaries/$b" \
                        && rm -f /tmp/weed.tgz /tmp/weed
                    ;;
                rqlited)
                    echo "  fetching rqlited v10.0.5 ($b not in git)..."
                    curl -fSL --progress-bar -o /tmp/rqlite.tgz \
                        "https://github.com/rqlite/rqlite/releases/download/v10.0.5/rqlite-v10.0.5-linux-amd64.tar.gz" \
                        && tar xzf /tmp/rqlite.tgz -C /tmp \
                        && install -m 0755 /tmp/rqlite-v10.0.5-linux-amd64/rqlited \
                                          "$PAYLOAD_DIR/binaries/$b" \
                        && install -m 0755 /tmp/rqlite-v10.0.5-linux-amd64/rqlited \
                                          "$INSTALLER/binaries/$b" \
                        && rm -rf /tmp/rqlite.tgz /tmp/rqlite-v10.0.5-linux-amd64
                    ;;
                *)
                    echo "  WARN: $INSTALLER/binaries/$b not found — produce it first" >&2
                    ;;
            esac
        fi
    done
    # Python helper bundled alongside the static binaries.
    if [ -f "$INSTALLER/binaries/vm_exporter.py" ]; then
        cp "$INSTALLER/binaries/vm_exporter.py" "$PAYLOAD_DIR/binaries/vm_exporter.py"
    fi
    cp "$INSTALLER/lib/"*.py                   "$PAYLOAD_DIR/lib/"
    # Non-Python lib assets — bedrock_schema.sql is consumed by
    # mgmt_install at `bedrock init` time to bootstrap rqlite.
    cp "$INSTALLER/lib/"*.sql                  "$PAYLOAD_DIR/lib/" 2>/dev/null || true
    cp -r "$INSTALLER/configs/"*               "$PAYLOAD_DIR/configs/" 2>/dev/null || true

    # 2b. ELRepo + DRBD RPMs — pinned versions for reproducibility.
    EL=10
    ELREPO_RPMS=(
        "https://www.elrepo.org/elrepo-release-${EL}.el${EL}.elrepo.noarch.rpm"
        "https://elrepo.org/linux/elrepo/el${EL}/x86_64/RPMS/kmod-drbd9x-9.3.2-1.el${EL}_1.elrepo.x86_64.rpm"
        "https://elrepo.org/linux/elrepo/el${EL}/x86_64/RPMS/drbd9x-utils-9.34.0-1.el${EL}.elrepo.x86_64.rpm"
    )
    echo "  fetching ELRepo + DRBD RPMs..."
    for url in "${ELREPO_RPMS[@]}"; do
        out="$PAYLOAD_DIR/rpms/$(basename "$url")"
        [ -f "$out" ] || curl -fsSL -o "$out" "$url"
        echo "    $(basename "$out") ($(du -h "$out" | cut -f1))"
    done

    # 2c. Kopia binary (≥256-bit hash backup repo client)
    KOPIA_VERSION="0.21.1"
    KOPIA_URL="https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz"
    if [ ! -f "$PAYLOAD_DIR/kopia" ]; then
        echo "  fetching kopia ${KOPIA_VERSION}..."
        curl -fsSL -o "$BUILD_DIR/kopia.tgz" "$KOPIA_URL"
        tar -xzf "$BUILD_DIR/kopia.tgz" -C "$BUILD_DIR"
        cp "$BUILD_DIR/kopia-${KOPIA_VERSION}-linux-x64/kopia" "$PAYLOAD_DIR/kopia"
        chmod +x "$PAYLOAD_DIR/kopia"
    fi
    echo "    kopia ($(du -h "$PAYLOAD_DIR/kopia" | cut -f1))"

    # 2d. Python wheels for mgmt deps. We use pip download into the
    # wheels/ dir; bedrock-bootstrap then `pip install --no-index
    # --find-links wheels/` for full offline.
    if [ "$QUICK" -eq 0 ]; then
        echo "  downloading Python wheels..."
        pip3 download -q --dest "$PAYLOAD_DIR/wheels" \
            fastapi uvicorn paramiko websockets pydantic python-multipart msgpack \
            >/dev/null
        echo "    $(ls "$PAYLOAD_DIR/wheels" | wc -l) wheels ($(du -sh "$PAYLOAD_DIR/wheels" | cut -f1))"
    fi

    # 2e. Big-asset bundles. Skipped in --quick (saves ~1 GB).
    if [ "$QUICK" -eq 0 ]; then
        # virtio-win for Windows VM driver disk
        if [ ! -f "$PAYLOAD_DIR/virtio-win.iso" ]; then
            echo "  fetching virtio-win.iso..."
            curl -fsSL -o "$PAYLOAD_DIR/virtio-win.iso" \
                "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
        fi
        # Alpine for cattle-VM default boot disk
        if [ ! -f "$PAYLOAD_DIR/alpine.qcow2" ]; then
            echo "  fetching alpine.qcow2..."
            ALPINE_URL="https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/nocloud_alpine-3.21.7-x86_64-bios-cloudinit-r0.qcow2"
            curl -fsSL -o "$PAYLOAD_DIR/alpine.qcow2" "$ALPINE_URL"
        fi
    fi

    chmod +x "$PAYLOAD_DIR"/install.sh "$PAYLOAD_DIR"/bedrock \
              "$PAYLOAD_DIR"/bedrock-net \
              "$PAYLOAD_DIR"/bedrock-fence-watchdog \
              "$PAYLOAD_DIR"/bedrock-cert-refresh \
              "$PAYLOAD_DIR"/bedrock-mdns \
              "$PAYLOAD_DIR"/bedrock-redirect 2>/dev/null || true
    chmod +x "$PAYLOAD_DIR"/binaries/* 2>/dev/null || true
fi

PAYLOAD_SIZE=$(du -sh "$PAYLOAD_DIR" | cut -f1)
echo "  payload total: $PAYLOAD_SIZE"

# ── Step 3: extract source ISO contents ───────────────────────────
echo "[bedrock-iso] step 3/5: extract source ISO"
EXTRACT_DIR="$BUILD_DIR/iso-extract"
rm -rf "$EXTRACT_DIR"
mkdir -p "$EXTRACT_DIR"
xorriso -osirrox on -indev "$SRC_ISO" -extract / "$EXTRACT_DIR" 2>&1 | tail -3
chmod -R u+w "$EXTRACT_DIR"

# ── Step 4: stage kickstart + payload + edit boot configs ─────────
echo "[bedrock-iso] step 4/5: stage kickstart + payload"

# Kickstart at root of ISO — anaconda finds it via inst.ks=cdrom:/dev/sr0:/ks.cfg
cp "$KS_FILE" "$EXTRACT_DIR/ks.cfg"

# Payload at /bedrock on the ISO; %post copies into /var/lib/bedrock-install
rm -rf "$EXTRACT_DIR/bedrock"
cp -r "$PAYLOAD_DIR" "$EXTRACT_DIR/bedrock"

# Add inst.ks=... to the bootloader configs so anaconda runs unattended.
# We also rewrite the stage2 label so anaconda finds the install
# medium under our new volume id (Bedrock-Install-10) — without this,
# the installer would look for the old AlmaLinux label and fail.
# Kernel args added to every install entry:
#   inst.ks=…       — auto-load our kickstart
#   console=tty0,ttyS0 — render anaconda on both video AND serial,
#                        so headless installs (testbed virt-install
#                        --graphics none, USB-on-real-hw with a
#                        serial port) work without a monitor.
#   rd.live.check=0  — skip the boot-time ISO checksum (we rebuild
#                      the ISO above; original implant no longer
#                      matches; we re-implant a fresh one as the last
#                      step, but rd.live.check=0 also suppresses the
#                      90-second prompt that appears even when the
#                      checksum is valid).
#   inst.ks=hd:LABEL=…:/ks.cfg — load ks.cfg from the install medium
#                      *without* the unmount/re-insert dance that
#                      `inst.ks=cdrom:/dev/sr0:/ks.cfg` triggers (the
#                      cdrom syntax pops the tray, expects an
#                      operator to re-insert; on a one-medium install
#                      that's a deadlock — the same disc holds both
#                      ks.cfg AND stage2, so re-inserting is the only
#                      way to proceed but anaconda can't auto-do it).
KS_ARG="inst.ks=hd:LABEL=Bedrock-Install-${ALMA_VERSION}:/ks.cfg console=tty0 console=ttyS0,115200n8 rd.live.check=0"

# In --quick mode the source is boot.iso, which carries NO package
# repository on the disc — only kernel + initrd + stage2. The
# kickstart says `cdrom` to remain valid for the DVD path, so we
# override the actual install repo via `inst.repo=` kernel arg in
# --quick mode. Real hardware offline installs use the DVD path
# where the on-disc repo is sufficient and no override is needed.
if [ "$QUICK" -eq 1 ]; then
    KS_ARG="$KS_ARG inst.repo=https://repo.almalinux.org/almalinux/${ALMA_VERSION}/BaseOS/x86_64/os/"
fi
NEW_VOLID="Bedrock-Install-${ALMA_VERSION}"

# Discover the source ISO's volume label so we can rewrite it.
SRC_VOLID=$(xorriso -indev "$SRC_ISO" 2>&1 | \
            awk -F"'" '/^Volume id/ {print $2; exit}')
echo "  source volid: ${SRC_VOLID:-?}"
echo "  new    volid: ${NEW_VOLID}"

# isolinux/isolinux.cfg (BIOS, only present on some images)
if [ -f "$EXTRACT_DIR/isolinux/isolinux.cfg" ]; then
    sed -i "s|append initrd=initrd.img|append initrd=initrd.img $KS_ARG|" \
        "$EXTRACT_DIR/isolinux/isolinux.cfg"
    [ -n "$SRC_VOLID" ] && sed -i "s|LABEL=$SRC_VOLID|LABEL=$NEW_VOLID|g" \
        "$EXTRACT_DIR/isolinux/isolinux.cfg"
    sed -i 's|^timeout .*|timeout 10|' "$EXTRACT_DIR/isolinux/isolinux.cfg"
fi

# Grub configs — UEFI uses /EFI/BOOT/grub.cfg, BIOS-grub uses
# /boot/grub2/grub.cfg. Rewrite both.
for grub_cfg in "$EXTRACT_DIR/EFI/BOOT/grub.cfg" \
                "$EXTRACT_DIR/boot/grub2/grub.cfg"; do
    if [ -f "$grub_cfg" ]; then
        # Add our kickstart arg to every linux/linuxefi entry that
        # doesn't already mention inst.ks (idempotent if the source
        # ISO already carried one).
        sed -i "/inst.ks=/!s|linuxefi /images/pxeboot/vmlinuz|linuxefi /images/pxeboot/vmlinuz $KS_ARG|" "$grub_cfg"
        sed -i "/inst.ks=/!s|linux /images/pxeboot/vmlinuz|linux /images/pxeboot/vmlinuz $KS_ARG|" "$grub_cfg"
        # Rewrite the stage2 label so anaconda finds OUR ISO.
        [ -n "$SRC_VOLID" ] && sed -i "s|LABEL=$SRC_VOLID|LABEL=$NEW_VOLID|g" "$grub_cfg"
        # 1-second timeout instead of 60.
        sed -i 's|^set timeout=.*|set timeout=1|' "$grub_cfg"
    fi
done

# ── Step 5: repack the ISO with xorriso ───────────────────────────
echo "[bedrock-iso] step 5/5: repacking ISO"

# Use -indev / -outdev mode so xorriso replicates the source ISO's
# exact boot setup (isolinux MBR, El Torito catalogue, EFI partition)
# without us having to extract & re-pass the bootloader binaries by
# hand. `-update_rl` overlays the modified $EXTRACT_DIR onto the
# image, picking up our edited isolinux.cfg / grub.cfg + the kickstart
# + the /bedrock payload, while `-boot_image any replay` reuses the
# original boot info verbatim.
rm -f "$OUT_ISO"
xorriso \
    -indev "$SRC_ISO" \
    -outdev "$OUT_ISO" \
    -volid "Bedrock-Install-${ALMA_VERSION}" \
    -boot_image any replay \
    -update_r "$EXTRACT_DIR" / \
    -close on \
    -commit_eject all 2>&1 | tail -8

# Implant a fresh isomd5 checksum so anaconda's built-in
# checkisomd5@dev-sr0.service doesn't refuse to mount our ISO. The
# installer's initrd embeds checkisomd5; without a valid implant it
# halts with "Media check failed... System will halt in 12 hours"
# even when rd.live.check=0 is on the cmdline (the prompt is
# suppressed, but the failure path can still trip if the implant is
# missing entirely).
if command -v implantisomd5 >/dev/null 2>&1; then
    echo "  implanting isomd5..."
    implantisomd5 --supported-iso "$OUT_ISO" >/dev/null
else
    echo "  WARN: implantisomd5 not installed (apt: isomd5sum). The ISO" >&2
    echo "        will boot but anaconda may complain about media check." >&2
fi

# ── Done ───────────────────────────────────────────────────────────
SIZE=$(du -h "$OUT_ISO" | cut -f1)
SHA=$(sha256sum "$OUT_ISO" | cut -d' ' -f1)
echo
echo "=========================================="
echo "  bedrock-install ISO built"
echo "=========================================="
echo "  path:   $OUT_ISO"
echo "  size:   $SIZE"
echo "  sha256: $SHA"
echo
echo "  next:"
echo "    sudo dd if=$OUT_ISO of=/dev/sdX bs=4M status=progress"
echo "    (or in testbed: virt-install --cdrom $OUT_ISO …)"
echo
