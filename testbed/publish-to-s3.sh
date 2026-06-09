#!/usr/bin/env bash
# Publish the Bedrock install repo to a public S3 prefix.
#
# Use:
#   testbed/publish-to-s3.sh --prefix dev            # rolling dev build
#   testbed/publish-to-s3.sh --prefix dev --with-iso # also push the ~10GB ISO
#   testbed/publish-to-s3.sh --prefix v0.8 --tag     # frozen release
#   testbed/publish-to-s3.sh --prefix v0.8 --tag --as-latest --with-iso
#   testbed/publish-to-s3.sh --prefix dev --dry-run  # show what would change
#
# Safety rails:
#   * Allowlist sync — only the install artefacts listed below get pushed.
#     Everything else is left out: tests, docs, .git, build intermediates,
#     symlinks, anything matching *.key / *.pem / *.env / cluster.key*.
#   * Refuses to push a versioned prefix (v*) without --tag.
#   * Refuses to push to an existing versioned prefix without
#     --allow-tag-overwrite (versioned releases should be immutable).
#   * Refuses to run with a dirty working tree unless --allow-dirty.
#   * --dry-run prints rclone's planned changes; no upload happens.
#
# Credentials: ~/.config/bedrock-s3/rclone.conf (mode 0600, outside repo).
set -euo pipefail

CONFIG="$HOME/.config/bedrock-s3/rclone.conf"
REMOTE="bedrock:bedrock"               # rclone remote name : bucket name
REPO="$(cd "$(dirname "$0")/.." && pwd)"

PREFIX=""
TAG_MODE=0
WITH_ISO=0
DRY_RUN=0
ALLOW_DIRTY=0
ALLOW_TAG_OVERWRITE=0
AS_LATEST=0

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)               PREFIX="$2"; shift 2 ;;
        --tag)                  TAG_MODE=1; shift ;;
        --with-iso)             WITH_ISO=1; shift ;;
        --dry-run)              DRY_RUN=1; shift ;;
        --allow-dirty)          ALLOW_DIRTY=1; shift ;;
        --allow-tag-overwrite)  ALLOW_TAG_OVERWRITE=1; shift ;;
        --as-latest)            AS_LATEST=1; shift ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0 ;;
        *)
            echo "ERROR: unknown arg: $1" >&2
            exit 2 ;;
    esac
done

[ -n "$PREFIX" ] || { echo "ERROR: --prefix is required (e.g. dev, v0.8)" >&2; exit 2; }
[ -f "$CONFIG" ] || { echo "ERROR: rclone config missing at $CONFIG" >&2; exit 2; }

# Versioned releases need --tag and must be immutable.
case "$PREFIX" in
    v[0-9]*)
        if [ $TAG_MODE -ne 1 ]; then
            echo "ERROR: pushing to versioned prefix '$PREFIX' requires --tag" >&2
            exit 2
        fi
        ;;
    dev|latest)
        if [ $TAG_MODE -eq 1 ]; then
            echo "ERROR: --tag is for versioned prefixes only (got --prefix $PREFIX)" >&2
            exit 2
        fi
        ;;
    *)
        echo "ERROR: --prefix must be 'dev', 'latest', or 'vN*' (got: $PREFIX)" >&2
        exit 2
        ;;
esac

# Working tree clean unless explicitly allowed.
cd "$REPO"
if [ "$(git status --porcelain | wc -l)" -gt 0 ] && [ $ALLOW_DIRTY -ne 1 ]; then
    echo "ERROR: working tree has uncommitted changes. Commit first, or pass --allow-dirty." >&2
    git status --short
    exit 2
fi

# If --tag, refuse overwrite of an existing release unless --allow-tag-overwrite.
if [ $TAG_MODE -eq 1 ] && [ $ALLOW_TAG_OVERWRITE -ne 1 ]; then
    existing=$(rclone --config "$CONFIG" lsf "$REMOTE/$PREFIX/" 2>/dev/null | head -1)
    if [ -n "$existing" ]; then
        echo "ERROR: $PREFIX/ already has content on S3 (e.g. $existing)." >&2
        echo "       Releases should be immutable; pass --allow-tag-overwrite to override." >&2
        exit 2
    fi
fi

COMMIT=$(git rev-parse --short HEAD)
COMMIT_LONG=$(git rev-parse HEAD)
NOW_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Build a temp tree with ONLY the allowlisted paths, in the layout we want
# the bucket to have. rclone sync against this temp tree → S3.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

echo "[publish] staging into $STAGE"

# ── Allowlist ────────────────────────────────────────────────────────────
# install.sh + CLI + daemon + small helper scripts
# (install.sh's curl URLs are exactly these top-level names)
install -m 0755 -D "$REPO/installer/install.sh"            "$STAGE/install.sh"
install -m 0755 -D "$REPO/installer/bedrock"               "$STAGE/bedrock"
install -m 0755 -D "$REPO/installer/bedrock-d"             "$STAGE/bedrock-d"
install -m 0755 -D "$REPO/installer/bedrock-cert-refresh"  "$STAGE/bedrock-cert-refresh"
install -m 0755 -D "$REPO/installer/bedrock-mdns"          "$STAGE/bedrock-mdns"
install -m 0755 -D "$REPO/installer/bedrock-redirect"      "$STAGE/bedrock-redirect"

# lib/ — every .py + .sql (.md is documentation; not part of the runtime payload)
mkdir -p "$STAGE/lib"
find "$REPO/lib" -maxdepth 1 -type f \( -name "*.py" -o -name "*.sql" \) \
    -exec install -m 0644 {} "$STAGE/lib/" \;

# bedrock_d/ — shipped as a tarball so install.sh doesn't need to
# enumerate files (and new submodules don't need an install.sh edit).
# Extracted to /usr/local/lib/bedrock/ on the target, where the
# bedrock CLI + bedrock-d daemon already have sys.path entries.
( cd "$REPO" && tar czf "$STAGE/bedrock_d.tar.gz" \
    --exclude="__pycache__" --exclude="*.pyc" \
    bedrock_d )

# mgmt/ — same shape. Extracted to /opt/bedrock/. Includes the
# Svelte UI build dir which is large but already minified.
#
# Before tarring, run `npm run build` if mgmt/ui/src/ is newer than
# mgmt/ui/build/. The deployed dashboard serves mgmt/ui/build/; if
# src/ has changed (endpoint renames, new pages, etc.) without a
# rebuild, the shipped UI POSTs to old endpoints and gets 405s.
if [ -d "$REPO/mgmt/ui/src" ] && \
   [ -n "$(find "$REPO/mgmt/ui/src" -newer "$REPO/mgmt/ui/build" -print -quit 2>/dev/null)" ]; then
    if command -v npm >/dev/null 2>&1; then
        echo "[publish] mgmt/ui/src/ newer than ui/build/ — running npm build"
        ( cd "$REPO/mgmt/ui" && npm run build ) 2>&1 | tail -5
    else
        echo "[publish] WARN: mgmt/ui/src/ is newer than ui/build/ but npm is not installed" >&2
        echo "[publish]       The published UI bundle will be STALE." >&2
    fi
fi
( cd "$REPO" && tar czf "$STAGE/mgmt.tar.gz" \
    --exclude="__pycache__" --exclude="*.pyc" \
    --exclude="mgmt/ui/node_modules" \
    --exclude="mgmt/ui/.svelte-kit" \
    --exclude="mgmt/ui/src" \
    mgmt )

# systemd unit files + timers + sshd dropins
mkdir -p "$STAGE/configs"
find "$REPO/installer/configs" -maxdepth 1 -type f \
    \( -name "*.service" -o -name "*.timer" -o -name "*.conf" \) \
    -exec install -m 0644 {} "$STAGE/configs/" \;

# binaries/ + wheels/ — re-use the iso-build payload that's already curated
if [ -d "$REPO/installer/iso-build/payload/binaries" ]; then
    cp -r "$REPO/installer/iso-build/payload/binaries" "$STAGE/binaries"
fi
if [ -d "$REPO/installer/iso-build/payload/wheels" ]; then
    cp -r "$REPO/installer/iso-build/payload/wheels" "$STAGE/wheels"
    # Manifest for HTTP installs — install.sh enumerates this list and
    # curl-fetches each entry. (Object stores disable bucket listing so
    # we can't directory-scrape.)
    ( cd "$STAGE/wheels" && find . -maxdepth 1 -type f -name "*.whl" \
        -printf "%f\n" | sort > MANIFEST.txt )
fi

# rpms/ — DRBD kmod + utils + ELRepo release. Same manifest pattern.
# These are the slow leg of `bedrock bootstrap` if dnf has to pull
# from elrepo.org's mirrors; mirroring them on S3 means a Hetzner
# install is fast even on a cold box.
if [ -d "$REPO/installer/iso-build/payload/rpms" ]; then
    cp -r "$REPO/installer/iso-build/payload/rpms" "$STAGE/rpms"
    ( cd "$STAGE/rpms" && find . -maxdepth 1 -type f -name "*.rpm" \
        -printf "%f\n" | sort > MANIFEST.txt )
fi

# ISOs (optional). Push both the net-install and offline variants
# under their versioned names. See docs/install-and-iso.md.
if [ $WITH_ISO -eq 1 ]; then
    net_iso="$REPO/installer/iso-build/output/bedrock-installer-${PREFIX}.iso"
    offline_iso="$REPO/installer/iso-build/output/bedrock-installer-${PREFIX}-offline.iso"
    pushed=0
    if [ -f "$net_iso" ]; then
        install -m 0644 "$net_iso" "$STAGE/bedrock-installer-${PREFIX}.iso"
        pushed=$((pushed+1))
    fi
    if [ -f "$offline_iso" ]; then
        install -m 0644 "$offline_iso" "$STAGE/bedrock-installer-${PREFIX}-offline.iso"
        pushed=$((pushed+1))
    fi
    if [ $pushed -eq 0 ]; then
        echo "ERROR: --with-iso requested but no ISOs found for version '$PREFIX' in" >&2
        echo "       $REPO/installer/iso-build/output/" >&2
        echo "       Build them first: installer/iso-build/build-iso.sh --version $PREFIX" >&2
        exit 2
    fi
fi

# Version manifest
cat > "$STAGE/VERSION" <<EOF
prefix=$PREFIX
commit=$COMMIT_LONG
commit_short=$COMMIT
published_at=$NOW_UTC
EOF

# Per-file SHA-256s for verification on the download side
( cd "$STAGE" && find . -type f -not -name "SHA256SUMS" \
    -exec sha256sum {} + \
  | sed 's|  \./|  |' \
  > SHA256SUMS )

# ── Hard exclude (paranoia even after allowlist) ─────────────────────────
# Refuse if anything that looks secret-ish made it into the stage tree.
found_bad=$(find "$STAGE" -type f \
    \( -name "*.key" -o -name "*.pem" -o -name "*.env" \
       -o -name "cluster.key*" -o -name "id_*" \
       -o -name "*.pyc" \) 2>/dev/null)
if [ -n "$found_bad" ]; then
    echo "ERROR: secret-looking file(s) reached stage tree — refusing to upload:" >&2
    echo "$found_bad" >&2
    exit 2
fi

echo ""
echo "[publish] stage tree summary:"
( cd "$STAGE" && find . -type f | wc -l ) | xargs printf "  %s files\n"
du -sh "$STAGE" | awk '{printf "  total size: %s\n", $1}'
echo ""

# ── Sync ─────────────────────────────────────────────────────────────────
SYNC_FLAGS="--config $CONFIG --progress --transfers 8 --checkers 16"
# (acl=public-read + no_check_bucket are set in rclone.conf so they apply to
# all operations against this remote; not CLI flags on rclone 1.60.)
if [ $DRY_RUN -eq 1 ]; then
    SYNC_FLAGS="$SYNC_FLAGS --dry-run"
    echo "[publish] DRY RUN — no upload"
fi

# ISO PROTECTION (load-bearing): `rclone sync` makes the destination MATCH the
# stage tree — so a publish WITHOUT --with-iso (no ISOs staged) would DELETE the
# installer ISOs already on S3. The ISOs are long-lived release artifacts that must
# ALWAYS stay (they may lag the payload a little — fine). So EXCLUDE them from every
# sync: rclone never lists an excluded path on EITHER side, so an excluded ISO in
# the destination is invisible to the delete pass and is left untouched. The sync
# still prunes stale NON-iso payload as before. Fresh ISOs are then uploaded
# ADDITIVELY via `rclone copy` (which never deletes) only when --with-iso.
ISO_EXCLUDE='bedrock-installer-*.iso'

upload_isos_to() {  # $1 = destination remote path; copies whatever ISOs are staged
    local dest="$1" iso
    for iso in "bedrock-installer-${PREFIX}.iso" \
               "bedrock-installer-${PREFIX}-offline.iso"; do
        if [ -f "$STAGE/$iso" ]; then
            echo "[publish] copying ISO $iso → $dest (additive, never deletes)"
            rclone copy $SYNC_FLAGS "$STAGE/$iso" "$dest"
        fi
    done
}

echo "[publish] syncing $STAGE/ → $REMOTE/$PREFIX/ (ISOs excluded — never deleted)"
rclone sync $SYNC_FLAGS --exclude "$ISO_EXCLUDE" "$STAGE/" "$REMOTE/$PREFIX/"
[ $WITH_ISO -eq 1 ] && upload_isos_to "$REMOTE/$PREFIX/"

# ── Copy to /latest/ if requested ────────────────────────────────────────
if [ $AS_LATEST -eq 1 ]; then
    if [ $TAG_MODE -ne 1 ]; then
        echo "WARNING: --as-latest without --tag is unusual; skipping latest update." >&2
    else
        echo "[publish] copying $PREFIX/ → latest/ (ISOs excluded from sync)"
        rclone sync $SYNC_FLAGS --exclude "$ISO_EXCLUDE" "$STAGE/" "$REMOTE/latest/"
        [ $WITH_ISO -eq 1 ] && upload_isos_to "$REMOTE/latest/"
    fi
fi

echo ""
echo "[publish] done."
[ $DRY_RUN -eq 0 ] && echo "  install URL: https://bedrock.fsn1.your-objectstorage.com/$PREFIX/install.sh"
if [ $DRY_RUN -eq 0 ] && [ $WITH_ISO -eq 1 ]; then
    [ -f "$STAGE/bedrock-installer-${PREFIX}.iso" ] && \
        echo "  net ISO URL: https://bedrock.fsn1.your-objectstorage.com/$PREFIX/bedrock-installer-${PREFIX}.iso"
    [ -f "$STAGE/bedrock-installer-${PREFIX}-offline.iso" ] && \
        echo "  offline ISO: https://bedrock.fsn1.your-objectstorage.com/$PREFIX/bedrock-installer-${PREFIX}-offline.iso"
fi
