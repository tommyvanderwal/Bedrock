#!/usr/bin/env bash
# Build Bedrock installer ISOs.
#
# Produces, by default, BOTH variants for the requested version:
#
#   bedrock-installer-<version>.iso          (net-install, ~1 GB)
#   bedrock-installer-<version>-offline.iso  (full offline, ~4-8 GB)
#
# Both come from the same kickstart (bedrock-almalinux-10.ks) with
# variant-specific placeholders substituted at build time. See
# docs/install-and-iso.md for the design rationale.
#
# Net ISO: starts from AlmaLinux's boot.iso, pulls Alma packages from
#   Alma's mirrors during install, fetches Bedrock from S3 at first
#   boot. Small download, requires internet at install time.
#
# Offline ISO: starts from AlmaLinux's dvd.iso, bundles every Bedrock
#   artefact (RPMs, wheels, binaries, VM images) onto the disc. No
#   network needed at install or first-boot. The deliverable for
#   airgap / MSP-ship-to-site use cases.
#
# Use:
#   ./build-iso.sh                              # version=dev, both variants
#   ./build-iso.sh --version v0.8               # version=v0.8, both variants
#   ./build-iso.sh --version dev --variant net  # just the net ISO
#   ./build-iso.sh --version dev --variant offline
#   ./build-iso.sh --skip-payload-refresh       # reuse cached payload
#   ./build-iso.sh --testbed                    # embed dev-box SSH key
#                                               # for testbed access
#
# Build host requirements:
#   - xorriso  (for ISO repack)
#   - curl, sha256sum, find, sed
#   - isomd5sum (for implantisomd5 — optional but recommended)
#   - pip3 (for fetching Python wheels into the payload, offline build)
#   - ~25 GB free disk for source ISOs + extracts + outputs

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$(cd "$INSTALLER/.." && pwd)"

BUILD_DIR="$HERE/build"
PAYLOAD_DIR="$HERE/payload"
OUT_DIR="$HERE/output"
KS_TEMPLATE="$HERE/bedrock-almalinux-10.ks"

mkdir -p "$BUILD_DIR" "$PAYLOAD_DIR" "$OUT_DIR"

# ── Argument parsing ──────────────────────────────────────────────
VERSION="dev"
VARIANT="both"
SKIP_PAYLOAD_REFRESH=0
TESTBED=0

while [ $# -gt 0 ]; do
    case "$1" in
        --version)               VERSION="$2"; shift 2 ;;
        --variant)               VARIANT="$2"; shift 2 ;;
        --skip-payload-refresh)  SKIP_PAYLOAD_REFRESH=1; shift ;;
        --testbed)               TESTBED=1; shift ;;
        -h|--help)               sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

case "$VARIANT" in
    both|net|offline) ;;
    *) echo "ERROR: --variant must be one of: both, net, offline" >&2; exit 2 ;;
esac

# S3 endpoint the net ISO points at. Version must match the prefix
# under bedrock.fsn1.your-objectstorage.com/<version>/.
S3_BASE="https://bedrock.fsn1.your-objectstorage.com/$VERSION"

ALMA_VERSION="10"

# Map variant → (source ISO kind, output filename, BEDROCK_REPO,
# firstboot ExecStart line, install method directive, label suffix).
NET_SRC_KIND="boot"      # ~1 GB AlmaLinux network install ISO
OFFLINE_SRC_KIND="dvd"   # ~8.5 GB AlmaLinux DVD ISO

NET_OUT_NAME="bedrock-installer-${VERSION}.iso"
OFFLINE_OUT_NAME="bedrock-installer-${VERSION}-offline.iso"

# Authorized-keys block (testbed only). Production ISOs ship empty
# /root/.ssh/authorized_keys; operator wires their own key via
# console at first login or a cloud-init-style mechanism later.
if [ "$TESTBED" -eq 1 ]; then
    AUTHORIZED_KEYS_BLOCK='cat > /root/.ssh/authorized_keys <<PUBKEY_EOF
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHXS8J+TpzUuO2WDCeSxV9baR5p7p14ZtaXWRvVlZgqp tommy@HP-G1a
PUBKEY_EOF'
else
    AUTHORIZED_KEYS_BLOCK='# (production build — no authorized_keys preloaded)'
fi

# ── Step 1: refresh the offline payload directory ─────────────────
# The net build doesn't bundle the payload, but the offline build
# does. We refresh once and re-use; net ISO simply doesn't copy it.

refresh_payload() {
    echo "[bedrock-iso] refreshing offline payload"
    rm -rf "$PAYLOAD_DIR"
    mkdir -p "$PAYLOAD_DIR"/{binaries,lib,configs,rpms,wheels}

    # Bedrock CLI + daemons + helpers + configs. Layout mirrors what
    # install.sh expects from a BEDROCK_REPO HTTP server, so the
    # same code paths work with file:// (offline) and https://
    # (testbed dev repo / S3).
    cp "$INSTALLER/install.sh"            "$PAYLOAD_DIR/install.sh"
    cp "$INSTALLER/bedrock"               "$PAYLOAD_DIR/bedrock"
    cp "$INSTALLER/bedrock-d"             "$PAYLOAD_DIR/bedrock-d"
    cp "$INSTALLER/bedrock-cert-refresh"  "$PAYLOAD_DIR/bedrock-cert-refresh"
    cp "$INSTALLER/bedrock-mdns"          "$PAYLOAD_DIR/bedrock-mdns"
    cp "$INSTALLER/bedrock-redirect"      "$PAYLOAD_DIR/bedrock-redirect"

    # Rebuild mgmt.tar.gz if any mgmt/ source is newer than the tarball.
    # The Svelte UI bundle under mgmt/ui/build/ is what the deployed
    # dashboard actually serves; if mgmt/ui/src/ has changed since the
    # last `npm run build`, the deployed UI will be stale. Run the UI
    # build whenever src/ is newer than build/, before tarring.
    MGMT_DIR="$REPO_ROOT/mgmt"
    UI_SRC="$MGMT_DIR/ui/src"
    UI_BUILD="$MGMT_DIR/ui/build"
    if [ -d "$MGMT_DIR" ]; then
        if [ -d "$UI_SRC" ] && [ -n "$(find "$UI_SRC" -newer "$UI_BUILD" -print -quit 2>/dev/null)" ]; then
            if command -v npm >/dev/null 2>&1; then
                echo "  mgmt/ui/src/ newer than ui/build/ — running npm build"
                ( cd "$MGMT_DIR/ui" && npm run build ) 2>&1 | tail -5
            else
                echo "  WARN: mgmt/ui/src/ is newer than ui/build/ but npm is not installed" >&2
                echo "        The shipped UI bundle will be STALE. Install Node.js to fix." >&2
            fi
        fi
        if [ ! -f "$INSTALLER/mgmt.tar.gz" ] || \
           [ -n "$(find "$MGMT_DIR" -newer "$INSTALLER/mgmt.tar.gz" -print -quit 2>/dev/null)" ]; then
            echo "  mgmt/ newer than mgmt.tar.gz — rebuilding"
            (cd "$REPO_ROOT" && tar czf "$INSTALLER/mgmt.tar.gz" mgmt)
        fi
    fi
    cp "$INSTALLER/mgmt.tar.gz"  "$PAYLOAD_DIR/mgmt.tar.gz"

    # bedrock_d daemon code tarball — install.sh extracts to /opt/bedrock-d
    BEDROCK_D_DIR="$REPO_ROOT/bedrock_d"
    if [ -d "$BEDROCK_D_DIR" ]; then
        if [ ! -f "$INSTALLER/bedrock_d.tar.gz" ] || \
           [ -n "$(find "$BEDROCK_D_DIR" -newer "$INSTALLER/bedrock_d.tar.gz" -print -quit 2>/dev/null)" ]; then
            echo "  bedrock_d/ newer than bedrock_d.tar.gz — rebuilding"
            (cd "$REPO_ROOT" && tar czf "$INSTALLER/bedrock_d.tar.gz" bedrock_d)
        fi
        cp "$INSTALLER/bedrock_d.tar.gz"  "$PAYLOAD_DIR/bedrock_d.tar.gz"
    fi

    # Cluster-time binaries (rqlited, weed, victoria-* etc.)
    for b in victoria-metrics victoria-logs node_exporter \
             vmagent vlagent vmbackup vmrestore rqlited weed; do
        if [ -f "$INSTALLER/binaries/$b" ]; then
            cp "$INSTALLER/binaries/$b" "$PAYLOAD_DIR/binaries/$b"
        else
            case "$b" in
                weed)
                    echo "  fetching weed v4.25 (not in tree)..."
                    curl -fSL --progress-bar -o /tmp/weed.tgz \
                        "https://github.com/seaweedfs/seaweedfs/releases/download/4.25/linux_amd64.tar.gz" \
                        && tar xzf /tmp/weed.tgz -C /tmp \
                        && install -m 0755 /tmp/weed "$PAYLOAD_DIR/binaries/$b" \
                        && install -m 0755 /tmp/weed "$INSTALLER/binaries/$b" \
                        && rm -f /tmp/weed.tgz /tmp/weed
                    ;;
                rqlited)
                    echo "  fetching rqlited v10.0.5 (not in tree)..."
                    curl -fSL --progress-bar -o /tmp/rqlite.tgz \
                        "https://github.com/rqlite/rqlite/releases/download/v10.0.5/rqlite-v10.0.5-linux-amd64.tar.gz" \
                        && tar xzf /tmp/rqlite.tgz -C /tmp \
                        && install -m 0755 /tmp/rqlite-v10.0.5-linux-amd64/rqlited "$PAYLOAD_DIR/binaries/$b" \
                        && install -m 0755 /tmp/rqlite-v10.0.5-linux-amd64/rqlited "$INSTALLER/binaries/$b" \
                        && rm -rf /tmp/rqlite.tgz /tmp/rqlite-v10.0.5-linux-amd64
                    ;;
                *)
                    echo "  WARN: $INSTALLER/binaries/$b not found — produce it first" >&2
                    ;;
            esac
        fi
    done
    if [ -f "$INSTALLER/binaries/vm_exporter.py" ]; then
        cp "$INSTALLER/binaries/vm_exporter.py" "$PAYLOAD_DIR/binaries/vm_exporter.py"
    fi

    # Lib + configs. MIRROR-WITH-DELETE the lib dir: the payload is
    # staged additively across builds (and reused under
    # --skip-payload-refresh), so a plain `cp` lets files DELETED from
    # installer/lib (e.g. the removed lib/vm.py) survive as stale
    # orphans in the payload. rsync --delete prunes anything no longer
    # present under installer/lib so the offline ISO can never ship a
    # lib/*.py that no longer exists in source. (lesson_iso_payload_drift)
    rsync -a --delete \
        --include='*.py' --include='*.sql' --exclude='*' \
        "$INSTALLER/lib/"  "$PAYLOAD_DIR/lib/"
    cp -r "$INSTALLER/configs/"*  "$PAYLOAD_DIR/configs/" 2>/dev/null || true

    # ELRepo + DRBD RPMs.
    EL=10
    ELREPO_RPMS=(
        "https://www.elrepo.org/elrepo-release-${EL}.el${EL}.elrepo.noarch.rpm"
        "https://elrepo.org/linux/elrepo/el${EL}/x86_64/RPMS/kmod-drbd9x-9.3.2-1.el${EL}_1.elrepo.x86_64.rpm"
        "https://elrepo.org/linux/elrepo/el${EL}/x86_64/RPMS/drbd9x-utils-9.34.0-1.el${EL}.elrepo.x86_64.rpm"
    )
    for url in "${ELREPO_RPMS[@]}"; do
        out="$PAYLOAD_DIR/rpms/$(basename "$url")"
        [ -f "$out" ] || curl -fsSL -o "$out" "$url"
    done
    # Manifest so install.sh's RPM fetch knows what to pull (used by
    # the http(s) repo path; redundant for file:// but harmless).
    (cd "$PAYLOAD_DIR/rpms" && ls *.rpm > MANIFEST.txt) || true

    # Kopia (backup client).
    KOPIA_VERSION="0.21.1"
    # Staged under binaries/ so publish-to-s3 (which syncs binaries/)
    # uploads it and install.sh fetches it like weed/rqlited.
    if [ ! -f "$PAYLOAD_DIR/binaries/kopia" ]; then
        echo "  fetching kopia ${KOPIA_VERSION}..."
        curl -fsSL -o "$BUILD_DIR/kopia.tgz" \
            "https://github.com/kopia/kopia/releases/download/v${KOPIA_VERSION}/kopia-${KOPIA_VERSION}-linux-x64.tar.gz"
        tar -xzf "$BUILD_DIR/kopia.tgz" -C "$BUILD_DIR"
        mkdir -p "$PAYLOAD_DIR/binaries"
        cp "$BUILD_DIR/kopia-${KOPIA_VERSION}-linux-x64/kopia" "$PAYLOAD_DIR/binaries/kopia"
        chmod +x "$PAYLOAD_DIR/binaries/kopia"
    fi

    # Python wheels for mgmt deps. Always include httpx — install.sh
    # depends on it from the wheels dir before any http call.
    echo "  downloading Python wheels..."
    pip3 download -q --dest "$PAYLOAD_DIR/wheels" \
        fastapi uvicorn paramiko websockets pydantic python-multipart msgpack httpx \
        >/dev/null
    (cd "$PAYLOAD_DIR/wheels" && ls *.whl > MANIFEST.txt) || true

    # virtio-win + alpine — big-asset bundles for VM creation.
    if [ ! -f "$PAYLOAD_DIR/virtio-win.iso" ]; then
        echo "  fetching virtio-win.iso..."
        curl -fsSL -o "$PAYLOAD_DIR/virtio-win.iso" \
            "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
    fi
    if [ ! -f "$PAYLOAD_DIR/alpine.qcow2" ]; then
        echo "  fetching alpine.qcow2..."
        curl -fsSL -o "$PAYLOAD_DIR/alpine.qcow2" \
            "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/cloud/nocloud_alpine-3.21.7-x86_64-bios-cloudinit-r0.qcow2"
    fi

    chmod +x "$PAYLOAD_DIR"/install.sh "$PAYLOAD_DIR"/bedrock \
              "$PAYLOAD_DIR"/bedrock-d \
              "$PAYLOAD_DIR"/bedrock-cert-refresh \
              "$PAYLOAD_DIR"/bedrock-mdns \
              "$PAYLOAD_DIR"/bedrock-redirect 2>/dev/null || true
    chmod +x "$PAYLOAD_DIR"/binaries/* 2>/dev/null || true

    echo "  payload total: $(du -sh "$PAYLOAD_DIR" | cut -f1)"
}

# Fail loudly if the staged payload still carries pre-cutover / deleted
# artifacts. Runs whether or not refresh_payload ran (so a reused cached
# payload under --skip-payload-refresh can't smuggle stale orphans onto
# the disc either). (lesson_iso_payload_drift)
assert_payload_clean() {
    local errs=0
    if [ -e "$PAYLOAD_DIR/lib/vm.py" ]; then
        echo "ERROR: $PAYLOAD_DIR/lib/vm.py is a removed legacy module" \
             "(internal-meta, hardcoded VG) — stale payload orphan." >&2
        errs=1
    fi
    if [ -f "$PAYLOAD_DIR/bedrock" ] && \
       grep -Eq 'from lib import vm|vm_mod' "$PAYLOAD_DIR/bedrock"; then
        echo "ERROR: $PAYLOAD_DIR/bedrock is the pre-cutover in-process CLI" \
             "(imports lib.vm) — stale payload orphan; expected the thin" \
             "HTTP-client bedrock from $INSTALLER/bedrock." >&2
        errs=1
    fi
    if [ "$errs" -ne 0 ]; then
        echo "Refusing to build offline ISO from a drifted payload." \
             "Re-run without --skip-payload-refresh to restage from source." >&2
        exit 3
    fi
}

if [ "$VARIANT" != "net" ]; then
    if [ "$SKIP_PAYLOAD_REFRESH" -eq 0 ]; then
        refresh_payload
    fi
    assert_payload_clean
fi

# ── ISO build function ────────────────────────────────────────────
# Args: $1 = variant (net|offline)
build_one() {
    local variant="$1"
    local src_kind out_name include_payload firstboot_exec install_method bedrock_repo variant_label
    local src_iso src_url out_iso

    if [ "$variant" = "net" ]; then
        src_kind="$NET_SRC_KIND"
        out_name="$NET_OUT_NAME"
        include_payload=0
        install_method="url --url=https://repo.almalinux.org/almalinux/${ALMA_VERSION}/BaseOS/x86_64/os/"
        bedrock_repo="$S3_BASE"
        # NOTE: use `;` with `set -e` instead of `&&` — awk's gsub
        # treats `&` as the matched-text backreference.
        firstboot_exec="ExecStart=/bin/bash -c 'set -e; curl -fsSL \${BEDROCK_REPO}/install.sh -o /tmp/bedrock-install.sh; bash /tmp/bedrock-install.sh'"
        variant_label="the net-install ISO ($VERSION)"
    else
        src_kind="$OFFLINE_SRC_KIND"
        out_name="$OFFLINE_OUT_NAME"
        include_payload=1
        install_method="cdrom"
        bedrock_repo="file:///var/lib/bedrock-install"
        firstboot_exec="ExecStart=/bin/bash /var/lib/bedrock-install/install.sh"
        variant_label="the offline ISO ($VERSION)"
    fi

    src_iso="$BUILD_DIR/AlmaLinux-${ALMA_VERSION}-latest-x86_64-${src_kind}.iso"
    src_url="https://repo.almalinux.org/almalinux/${ALMA_VERSION}/isos/x86_64/AlmaLinux-${ALMA_VERSION}-latest-x86_64-${src_kind}.iso"
    out_iso="$OUT_DIR/$out_name"

    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo "  Building $out_name  (variant: $variant)"
    echo "═══════════════════════════════════════════════════════════════════"

    # Source ISO.
    if [ ! -f "$src_iso" ]; then
        echo "[bedrock-iso] fetching $(basename "$src_iso")..."
        curl -fSL --progress-bar -o "$src_iso" "$src_url"
    fi
    echo "[bedrock-iso] source: $(basename "$src_iso") ($(du -h "$src_iso" | cut -f1))"

    # Materialize the kickstart from the template with variant-specific
    # substitutions.
    local ks_out="$BUILD_DIR/bedrock-almalinux-10-${variant}.ks"
    # Use awk for multi-line-safe substitution. The placeholders are
    # single-line tokens so sed is fine for those.
    awk -v auth="$AUTHORIZED_KEYS_BLOCK" \
        -v repo="$bedrock_repo" \
        -v exec="$firstboot_exec" \
        -v method="$install_method" \
        -v variant="$variant_label" '
        {
            gsub(/__BEDROCK_AUTHORIZED_KEYS__/, auth)
            gsub(/__BEDROCK_REPO__/, repo)
            gsub(/__BEDROCK_FIRSTBOOT_EXEC__/, exec)
            gsub(/__BEDROCK_INSTALL_METHOD__/, method)
            gsub(/__BEDROCK_VARIANT__/, variant)
            print
        }
    ' "$KS_TEMPLATE" > "$ks_out"

    # Extract source ISO.
    local extract_dir="$BUILD_DIR/iso-extract-${variant}"
    rm -rf "$extract_dir"
    mkdir -p "$extract_dir"
    echo "[bedrock-iso] extracting source ISO..."
    xorriso -osirrox on -indev "$src_iso" -extract / "$extract_dir" 2>&1 | tail -3
    chmod -R u+w "$extract_dir"

    # Place kickstart at /ks.cfg on the ISO.
    cp "$ks_out" "$extract_dir/ks.cfg"

    # Bundle payload only for offline variant.
    if [ "$include_payload" -eq 1 ]; then
        echo "[bedrock-iso] bundling payload..."
        rm -rf "$extract_dir/bedrock"
        cp -r "$PAYLOAD_DIR" "$extract_dir/bedrock"
    fi

    # Boot config edits.
    local volid="Bedrock-Installer-${VERSION}-${variant}"
    # Truncate volid to ISO 9660's 32-char limit.
    volid="${volid:0:32}"
    local ks_arg="inst.ks=hd:LABEL=${volid}:/ks.cfg console=tty0 console=ttyS0,115200n8 rd.live.check=0"

    # Net build also needs inst.repo= to point at Alma upstream, since
    # boot.iso carries no on-disc package repository.
    if [ "$variant" = "net" ]; then
        ks_arg="$ks_arg inst.repo=https://repo.almalinux.org/almalinux/${ALMA_VERSION}/BaseOS/x86_64/os/"
    fi

    # Discover source volid so we can rewrite LABEL= references.
    local src_volid
    src_volid=$(xorriso -indev "$src_iso" 2>&1 | awk -F"'" '/^Volume id/ {print $2; exit}' || true)

    # isolinux.cfg (BIOS) — if present.
    if [ -f "$extract_dir/isolinux/isolinux.cfg" ]; then
        sed -i "s|append initrd=initrd.img|append initrd=initrd.img $ks_arg|" \
            "$extract_dir/isolinux/isolinux.cfg"
        [ -n "$src_volid" ] && sed -i "s|LABEL=$src_volid|LABEL=$volid|g" \
            "$extract_dir/isolinux/isolinux.cfg"
        sed -i 's|^timeout .*|timeout 10|' "$extract_dir/isolinux/isolinux.cfg"
    fi

    # grub configs (UEFI + BIOS-grub).
    for grub_cfg in "$extract_dir/EFI/BOOT/grub.cfg" \
                    "$extract_dir/boot/grub2/grub.cfg"; do
        if [ -f "$grub_cfg" ]; then
            sed -i "/inst.ks=/!s|linuxefi /images/pxeboot/vmlinuz|linuxefi /images/pxeboot/vmlinuz $ks_arg|" "$grub_cfg"
            sed -i "/inst.ks=/!s|linux /images/pxeboot/vmlinuz|linux /images/pxeboot/vmlinuz $ks_arg|" "$grub_cfg"
            [ -n "$src_volid" ] && sed -i "s|LABEL=$src_volid|LABEL=$volid|g" "$grub_cfg"
            sed -i 's|^set timeout=.*|set timeout=1|' "$grub_cfg"
        fi
    done

    # Repack.
    echo "[bedrock-iso] repacking $out_name..."
    rm -f "$out_iso"
    xorriso \
        -indev "$src_iso" \
        -outdev "$out_iso" \
        -volid "$volid" \
        -boot_image any replay \
        -update_r "$extract_dir" / \
        -close on \
        -commit_eject all 2>&1 | tail -6

    if command -v implantisomd5 >/dev/null 2>&1; then
        implantisomd5 --supported-iso "$out_iso" >/dev/null
    fi

    local size sha
    size=$(du -h "$out_iso" | cut -f1)
    sha=$(sha256sum "$out_iso" | cut -d' ' -f1)
    echo "[bedrock-iso] built: $out_iso  ($size)"
    echo "             sha256: $sha"
}

# ── Build the requested variants ──────────────────────────────────
if [ "$VARIANT" = "both" ] || [ "$VARIANT" = "net" ]; then
    build_one net
fi
if [ "$VARIANT" = "both" ] || [ "$VARIANT" = "offline" ]; then
    build_one offline
fi

# ── Summary ───────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "  Build complete (version=$VERSION)"
echo "═══════════════════════════════════════════════════════════════════"
ls -lh "$OUT_DIR"/bedrock-installer-${VERSION}*.iso 2>/dev/null | awk '{print "  " $9 "  " $5}'
echo ""
echo "  Net ISO points at:  $S3_BASE"
echo "  Offline ISO uses:   file:///var/lib/bedrock-install (bundled)"
echo ""
echo "  Test with:"
echo "    sudo dd if=$OUT_DIR/$NET_OUT_NAME of=/dev/sdX bs=4M status=progress"
echo "    virt-install --cdrom $OUT_DIR/$OFFLINE_OUT_NAME ..."
echo ""
