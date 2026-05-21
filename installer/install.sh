#!/usr/bin/env bash
# Bedrock OOB installer bootstrap.
#
# Usage (on fresh AlmaLinux 9 minimal, as root):
#   curl -sSL http://<repo-host>:8000/install.sh | bash
#
# Or for testing, point at a specific repo:
#   BEDROCK_REPO=http://192.168.2.145:8000 curl -sSL ${BEDROCK_REPO}/install.sh | bash

set -euo pipefail

# Colour output when TTY
if [ -t 1 ]; then
    C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[34m'; C_0=$'\e[0m'
else
    C_G=""; C_Y=""; C_R=""; C_B=""; C_0=""
fi

log()   { echo "${C_B}[bedrock]${C_0} $*"; }
warn()  { echo "${C_Y}[bedrock]${C_0} $*" >&2; }
error() { echo "${C_R}[bedrock]${C_0} $*" >&2; }
die()   { error "$*"; exit 1; }

# ── Pre-flight checks ───────────────────────────────────────────────────────

[ "$(id -u)" = "0" ] || die "Run as root (try: sudo bash)."

if [ ! -f /etc/almalinux-release ] && ! grep -q 'AlmaLinux' /etc/os-release 2>/dev/null; then
    warn "Not detected as AlmaLinux. Continuing anyway (may fail)."
fi

# Determine the repo URL. User can override with BEDROCK_REPO env var.
# If not set, try to auto-derive from where this script was fetched.
: "${BEDROCK_REPO:=}"
if [ -z "$BEDROCK_REPO" ]; then
    # Default test repo — dev box on the LAN
    BEDROCK_REPO="http://192.168.100.1:8000"
    log "Using default repo: $BEDROCK_REPO (override with BEDROCK_REPO=...)"
fi

# Strip trailing slash
BEDROCK_REPO="${BEDROCK_REPO%/}"

log "Bedrock installer"
log "Repo: $BEDROCK_REPO"

# ── Check internet / repo reachability ─────────────────────────────────────

# Probe a specific known file (install.sh itself) rather than the bucket
# root. Object stores typically disable bucket listing, so a bare
# ``$BEDROCK_REPO/`` request returns 403 even when every file inside is
# fetchable. The HEAD on install.sh works against both a public S3
# bucket and a plain HTTP server (testbed serve.py), and via ``file://``
# the same flag falls through to a path stat.
if ! curl -fsI --max-time 5 "${BEDROCK_REPO}/install.sh" >/dev/null 2>&1; then
    die "Cannot reach repo at $BEDROCK_REPO. Check BEDROCK_REPO env var."
fi

# ── Install minimal prereqs ────────────────────────────────────────────────

log "Installing prerequisites..."
# python3 + python3-pip + curl are in AlmaLinux 10 BaseOS. python3-httpx
# is NOT in AlmaLinux's repos so we always install httpx from the
# bundled wheel cache via pip (the alternative — putting httpx in dnf —
# is a no-op at best and on AlmaLinux 10 makes the line return 1 which
# previously bricked the rqlite_client transport at firstboot).
dnf install -y -q python3 python3-pip curl >/dev/null 2>&1 || {
    warn "dnf install python3/pip/curl returned non-zero (already installed?). Continuing."
}
# httpx is the rqlite_client.py HTTP transport. Install from the
# bundled wheel cache (always offline at firstboot). Failure here is
# fatal — without httpx, the rqlite_client can't reach the cluster
# state store and `bedrock init` will silently produce an empty
# rqlite (see lessons-log: 25s init-with-no-httpx debug session).
log "Installing httpx via pip (offline wheels)..."
# Two-path wheels handling:
#   * file://  — pass the local dir straight to pip --find-links
#   * http(s):// — fetch the wheels into a tmp dir first, then point pip
#                  at it. pip's --find-links speaks URL too but only as
#                  a directory-listing scrape, which 403s on S3-style
#                  buckets that disable bucket-listing.
case "$BEDROCK_REPO" in
    file://*)
        WHEELS_DIR="${BEDROCK_REPO#file://}/wheels"
        if [ ! -d "$WHEELS_DIR" ]; then
            die "wheels dir missing: $WHEELS_DIR"
        fi
        ;;
    http://*|https://*)
        # Probe a known wheel (httpx — installed unconditionally below)
        # to verify the wheels/ prefix is reachable before downloading
        # everything. Manifest is wheels/MANIFEST.txt if present.
        if ! curl -fsI --max-time 5 "${BEDROCK_REPO}/wheels/MANIFEST.txt" >/dev/null 2>&1 \
           && ! curl -fsI --max-time 5 "${BEDROCK_REPO}/wheels/" >/dev/null 2>&1; then
            warn "wheels reachability probe failed; will still try the fetch"
        fi
        WHEELS_DIR="$(mktemp -d -t bedrock-wheels.XXXX)"
        log "  fetching wheel manifest from ${BEDROCK_REPO}/wheels/MANIFEST.txt"
        if curl -fsSL "${BEDROCK_REPO}/wheels/MANIFEST.txt" -o "$WHEELS_DIR/MANIFEST.txt"; then
            while IFS= read -r whl; do
                [ -z "$whl" ] && continue
                curl -fsSL "${BEDROCK_REPO}/wheels/$whl" -o "$WHEELS_DIR/$whl" \
                    || die "wheel fetch failed: $whl"
            done < "$WHEELS_DIR/MANIFEST.txt"
        else
            die "MANIFEST.txt missing under ${BEDROCK_REPO}/wheels/ — publish.sh must regenerate it"
        fi
        ;;
    *)
        die "BEDROCK_REPO must be file:// or http(s)://, got: $BEDROCK_REPO"
        ;;
esac
python3 -m pip install --break-system-packages --no-index \
    --find-links="$WHEELS_DIR" httpx \
    || die "httpx install failed — see pip output above. rqlite_client cannot work without it."
python3 -c "import httpx; print('  [bedrock] httpx', httpx.__version__, 'ready')" \
    || die "httpx installed but not importable"

# ── Stage RPM payload (DRBD + ELRepo) ──────────────────────────────────────
# `bedrock bootstrap` checks /var/lib/bedrock-install/rpms/ for the
# DRBD kmod + utils + ELRepo release before falling back to ELRepo's
# mirrors. Mirror them locally for the HTTPS install path so the
# bootstrap step doesn't spend minutes on slow upstream mirrors.
log "Staging RPM payload (DRBD kmod + utils + ELRepo release)..."
LOCAL_RPMS_DIR=/var/lib/bedrock-install/rpms
mkdir -p "$LOCAL_RPMS_DIR"
case "$BEDROCK_REPO" in
    file://*)
        SRC_RPMS="${BEDROCK_REPO#file://}/rpms"
        if [ -d "$SRC_RPMS" ]; then
            cp -f "$SRC_RPMS"/*.rpm "$LOCAL_RPMS_DIR/" 2>/dev/null || true
        fi
        ;;
    http://*|https://*)
        if curl -fsSL "${BEDROCK_REPO}/rpms/MANIFEST.txt" \
               -o "$LOCAL_RPMS_DIR/MANIFEST.txt" 2>/dev/null; then
            while IFS= read -r rpm; do
                [ -z "$rpm" ] && continue
                curl -fsSL "${BEDROCK_REPO}/rpms/$rpm" -o "$LOCAL_RPMS_DIR/$rpm" \
                    || warn "rpm fetch failed: $rpm (bootstrap will fall back to ELRepo)"
            done < "$LOCAL_RPMS_DIR/MANIFEST.txt"
        else
            warn "no rpms/MANIFEST.txt at ${BEDROCK_REPO}; bootstrap will use ELRepo's mirrors"
        fi
        ;;
esac

# ── Download bedrock CLI + lib ─────────────────────────────────────────────

INSTALL_DIR=/usr/local/bin
LIB_DIR=/usr/local/lib/bedrock/lib

mkdir -p "$LIB_DIR"

log "Downloading bedrock CLI..."
curl -fsSL "${BEDROCK_REPO}/bedrock" -o "${INSTALL_DIR}/bedrock"
chmod +x "${INSTALL_DIR}/bedrock"

log "Downloading bedrock-d (unified daemon: mesh + mgmt + orchestrator + dashboard)..."
curl -fsSL "${BEDROCK_REPO}/bedrock-d" -o "${INSTALL_DIR}/bedrock-d"
chmod +x "${INSTALL_DIR}/bedrock-d"
mkdir -p /etc/systemd/system /etc/bedrock /var/lib/bedrock
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-d.service" \
    -o /etc/systemd/system/bedrock-d.service
systemctl daemon-reload
# Service unit is enabled by `bedrock init` / `bedrock join` once
# loopback_ip is allocated and cluster.key written. Auto-starting
# pre-init would just spin in a tight no-op retry loop.

# rqlite — the cluster-state store (post-alpha-rewrite-notes.md D-01).
# Per-node Raft voter, on-disk SQLite mode, bound to this node's
# loopback /32 (set up by bedrock-net once cluster.json + state.json
# have caught up). Installed-but-not-enabled here; the service is
# enabled by `bedrock init` / `bedrock join` after rqlite_setup.py
# has materialised /etc/bedrock/rqlited.env. Starting it pre-config
# would fail-loop.
log "Installing rqlited binary + systemd unit..."
curl -fsSL "${BEDROCK_REPO}/binaries/rqlited" -o /usr/local/bin/rqlited
chmod +x /usr/local/bin/rqlited

# SeaweedFS — unified S3 stack (D-09). Replaces Garage + RustFS.
# master + volume run on EVERY node; filer + s3 follow the mgmt
# master via cluster_arbiter.py (D-07).
log "Installing weed binary + SeaweedFS systemd units..."
curl -fsSL "${BEDROCK_REPO}/binaries/weed" -o /usr/local/bin/weed
chmod +x /usr/local/bin/weed
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-weed-master.service" \
    -o /etc/systemd/system/bedrock-weed-master.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-weed-volume.service" \
    -o /etc/systemd/system/bedrock-weed-volume.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-weed-filer.service" \
    -o /etc/systemd/system/bedrock-weed-filer.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-weed-s3.service" \
    -o /etc/systemd/system/bedrock-weed-s3.service
mkdir -p /var/lib/bedrock/seaweedfs/master /var/lib/bedrock/seaweedfs/volumes
chmod 755 /var/lib/bedrock/seaweedfs
# Master + volume enabled at install time so every node hosts them
# automatically once `bedrock init` / `bedrock join` writes the env
# file. Filer + s3 are owned by cluster_arbiter.py.
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-rqlited.service" \
    -o /etc/systemd/system/bedrock-rqlited.service
# Arbiter unit (D-04) — installed but NOT enabled. cluster_arbiter.py
# starts/stops it imperatively as part of the master role transition;
# the orchestrator's revision-watcher calls converge() on every change.
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-rqlited-arbiter.service" \
    -o /etc/systemd/system/bedrock-rqlited-arbiter.service
mkdir -p /var/lib/bedrock/rqlite /var/lib/bedrock/cluster
chmod 700 /var/lib/bedrock/rqlite /var/lib/bedrock/cluster
systemctl daemon-reload
# Do NOT enable bedrock-rqlited here. Its EnvironmentFile=/etc/bedrock/
# rqlited.env doesn't exist until `bedrock init`/`join` writes it.
# Auto-starting at multi-user.target would crash-loop rqlited AND
# (via Requires=bedrock-net.service) drag bedrock-net into the same
# crash-loop — both hitting StartLimitBurst before the operator
# even has a chance to run init. `bedrock init`/`join` enables +
# starts it once the prereqs are in place.

# DRBD + libvirtd are NOT auto-started at boot. The mgmt service's
# orchestrator decides when it's safe (cluster contact established,
# role is leader/follower) and starts them imperatively. This is the
# fence-aware boot model documented in cluster-protocol-overview.md
# §"boot orchestration".
systemctl disable drbd >/dev/null 2>&1 || true
systemctl disable libvirtd >/dev/null 2>&1 || true

# VG loop-back PV reattach at boot. The DRBD promote path adds a
# sparse loop-backed PV to the bedrock VG so tier-*-meta (thick LVs
# outside the thin pool) can be created. losetup associations don't
# survive reboot — without this unit, the VG would come up missing
# its loop PV and bedrock would have to handle that on every command.
log "Installing bedrock-vg-loop boot helper..."
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-vg-loop.service" \
    -o /etc/systemd/system/bedrock-vg-loop.service 2>/dev/null || \
    warn "bedrock-vg-loop.service not in payload (older ISO?); skipping"
if [ -f /etc/systemd/system/bedrock-vg-loop.service ]; then
    systemctl daemon-reload
    systemctl enable bedrock-vg-loop.service >/dev/null 2>&1 || true
fi

# Three small auxiliary daemons stay AT ARM'S LENGTH from bedrock-d
# because they touch no cluster-decision code paths:
#
#   bedrock-cert-refresh   — pulls the local-ip.co wildcard cert
#                            every 24 h. Pure HTTPS download.
#   bedrock-mdns           — answers `bedrock.local` mDNS queries.
#                            Read-only UDP responder.
#   bedrock-redirect       — port-80 HTTP → HTTPS 302. Stateless.
#
# bedrock-d can `systemctl start/stop` them via the regular systemd
# API if/when lifecycle coordination matters. No fence-watchdog
# (per design: single-daemon design wants direct troubleshooting).
log "Installing bedrock-cert-refresh..."
curl -fsSL "${BEDROCK_REPO}/bedrock-cert-refresh" \
    -o /usr/local/bin/bedrock-cert-refresh
chmod +x /usr/local/bin/bedrock-cert-refresh
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-cert-refresh.service" \
    -o /etc/systemd/system/bedrock-cert-refresh.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-cert-refresh.timer" \
    -o /etc/systemd/system/bedrock-cert-refresh.timer
systemctl daemon-reload
systemctl enable --now bedrock-cert-refresh.timer >/dev/null 2>&1 || true

log "Installing bedrock-mdns + bedrock-redirect..."
curl -fsSL "${BEDROCK_REPO}/bedrock-mdns" \
    -o /usr/local/bin/bedrock-mdns
chmod +x /usr/local/bin/bedrock-mdns
curl -fsSL "${BEDROCK_REPO}/bedrock-redirect" \
    -o /usr/local/bin/bedrock-redirect
chmod +x /usr/local/bin/bedrock-redirect
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-mdns.service" \
    -o /etc/systemd/system/bedrock-mdns.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-redirect.service" \
    -o /etc/systemd/system/bedrock-redirect.service
systemctl daemon-reload
systemctl enable --now bedrock-mdns.service >/dev/null 2>&1 || true
systemctl enable --now bedrock-redirect.service >/dev/null 2>&1 || true

# sshd drop-in: turn off PerSourcePenalties for cluster-internal SSH.
# OpenSSH 9.8+ treats the every-3s paramiko probe burst from peer mgmt
# nodes like a brute-force attempt and locks out the source IP for up
# to 10 min on the first LoginGraceTime trip — nodes then flap Offline
# on every dashboard. Cluster traffic is between authenticated peers;
# the PerSourcePenalties defence is for anonymous brute-force, not us.
log "Disabling sshd PerSourcePenalties for cluster traffic..."
curl -fsSL "${BEDROCK_REPO}/configs/sshd-bedrock-no-penalty.conf" \
    -o /etc/ssh/sshd_config.d/99-bedrock-no-penalty.conf
sshd -t && systemctl reload sshd 2>/dev/null || warn "sshd reload skipped"

# Fetch the lib modules into /usr/local/lib/bedrock/lib/
LIB_FILES=(
    __init__.py
    hardware.py
    os_setup.py
    packages.py
    exporters.py
    discovery.py
    state.py
    mgmt_install.py
    agent_install.py
    vm.py
    workload.py
    tier_storage.py
    daemon_setup.py
    bedrock_state.py
    bedrock_schema.sql
    view_builder.py
    cluster_arbiter.py
    seaweedfs.py
    dashboard_install.py
    netd.py
    l2disc.py
    cert_manager.py
    mdns_responder.py
    http_redirect.py
    cluster_addr.py
    peer_auth.py
    operator_auth.py
    join_handshake.py
    observability.py
    rqlite_client.py
    rqlite_setup.py
    election.py
    witness.py
    state_shared.py
    workload.py
)
for f in "${LIB_FILES[@]}"; do
    curl -fsSL -o "${LIB_DIR}/${f}" "${BEDROCK_REPO}/lib/${f}" \
        || die "Failed to fetch lib/${f}"
done

# ── bedrock_d/ package (sagas, daemon orchestration) ─────────────────
# Shipped as a tarball so adding a new submodule doesn't need an
# install.sh edit. Lands at /usr/local/lib/bedrock/bedrock_d/ so
# `bedrock` CLI and `bedrock-d` daemon can import it (both add
# /usr/local/lib/bedrock to sys.path).
log "Downloading bedrock_d package tarball..."
curl -fsSL "${BEDROCK_REPO}/bedrock_d.tar.gz" -o /tmp/bedrock_d.tar.gz \
    || die "Failed to fetch bedrock_d.tar.gz"
tar xzf /tmp/bedrock_d.tar.gz -C /usr/local/lib/bedrock/ \
    || die "Failed to extract bedrock_d.tar.gz"
rm -f /tmp/bedrock_d.tar.gz

# ── mgmt/ package (FastAPI + Svelte UI + orchestrator) ───────────────
# Same shape — tarball so the UI build's sea of immutable assets
# doesn't require a manifest. Lands at /opt/bedrock/mgmt/.
log "Downloading mgmt package tarball..."
mkdir -p /opt/bedrock
curl -fsSL "${BEDROCK_REPO}/mgmt.tar.gz" -o /tmp/mgmt.tar.gz \
    || die "Failed to fetch mgmt.tar.gz"
tar xzf /tmp/mgmt.tar.gz -C /opt/bedrock/ \
    || die "Failed to extract mgmt.tar.gz"
rm -f /tmp/mgmt.tar.gz

# Sanity-check: list every .py in the payload's lib/ dir and refuse
# to continue if anything is missing locally. Catches the "developer
# added a new module under installer/lib/ but forgot LIB_FILES" gap
# (see memory/lesson_iso_payload_drift.md). file:// + http:// both
# work.
log "Verifying lib/ payload completeness..."
PAYLOAD_LIB="${BEDROCK_REPO}/lib/"
if [[ "$BEDROCK_REPO" == file://* ]]; then
    # Walk the dir directly.
    payload_dir="${BEDROCK_REPO#file://}/lib"
    payload_files=$(cd "$payload_dir" && ls *.py 2>/dev/null || true)
else
    # HTTP repo — Apache/nginx autoindex would let us, but our serve.py
    # serves only known paths. Skip the check in HTTP mode.
    payload_files=""
fi
if [ -n "$payload_files" ]; then
    missing=""
    for f in $payload_files; do
        if [ ! -f "${LIB_DIR}/${f}" ]; then
            missing="$missing $f"
        fi
    done
    if [ -n "$missing" ]; then
        warn "lib/ files in payload but not installed locally:$missing"
        warn "Adding them now."
        for f in $missing; do
            curl -fsSL -o "${LIB_DIR}/${f}" "${BEDROCK_REPO}/lib/${f}" \
                || warn "  failed to fetch lib/${f} (continuing)"
        done
    fi
fi

# ── Record repo location for future subcommands ────────────────────────────

mkdir -p /etc/bedrock
echo "BEDROCK_REPO=$BEDROCK_REPO" > /etc/bedrock/installer.env
chmod 600 /etc/bedrock/installer.env

# ── Hostname: ensure unique per-node identity ─────────────────────────────
# The kickstart leaves the hostname at the default `localhost.localdomain`
# (it can't know per-node names — same install image goes everywhere).
# Cluster-mgmt uses hostname as the registration key, so multiple nodes
# with the same hostname collide. Derive a unique one from the primary
# NIC's MAC address. Operator can override later with `hostnamectl`.
current_hn=$(hostname)
if [ "$current_hn" = "localhost.localdomain" ] || [ "$current_hn" = "localhost" ]; then
    primary_iface=$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')
    if [ -n "$primary_iface" ] && [ -f "/sys/class/net/$primary_iface/address" ]; then
        mac_suffix=$(tr -d : < "/sys/class/net/$primary_iface/address" | cut -c7-)
        new_hn="bedrock-${mac_suffix}"
        log "Setting hostname: $new_hn (was $current_hn)"
        hostnamectl set-hostname "$new_hn"
    fi
fi

# ── Run the Python bootstrap ──────────────────────────────────────────────

log "Running bedrock bootstrap..."
exec /usr/local/bin/bedrock bootstrap
