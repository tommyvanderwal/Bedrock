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

if ! curl -fsSL --max-time 5 "${BEDROCK_REPO}/" >/dev/null 2>&1; then
    die "Cannot reach repo at $BEDROCK_REPO. Check BEDROCK_REPO env var."
fi

# ── Install minimal prereqs ────────────────────────────────────────────────

log "Installing prerequisites..."
dnf install -y -q python3 python3-pip curl >/dev/null 2>&1 || {
    warn "dnf install failed (already installed?). Continuing."
}

# ── Download bedrock CLI + lib ─────────────────────────────────────────────

INSTALL_DIR=/usr/local/bin
LIB_DIR=/usr/local/lib/bedrock/lib

mkdir -p "$LIB_DIR"

log "Downloading bedrock CLI..."
curl -fsSL "${BEDROCK_REPO}/bedrock" -o "${INSTALL_DIR}/bedrock"
chmod +x "${INSTALL_DIR}/bedrock"

log "Downloading bedrock-rust daemon..."
curl -fsSL "${BEDROCK_REPO}/binaries/bedrock-rust" -o "${INSTALL_DIR}/bedrock-rust"
chmod +x "${INSTALL_DIR}/bedrock-rust"

log "Downloading bedrock-net (mesh discovery + routing daemon)..."
curl -fsSL "${BEDROCK_REPO}/bedrock-net" -o "${INSTALL_DIR}/bedrock-net"
chmod +x "${INSTALL_DIR}/bedrock-net"
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-net.service" \
    -o /etc/systemd/system/bedrock-net.service
# Service unit is enabled by `bedrock init` / `bedrock join` once
# loopback_ip is allocated, not now (the daemon needs cluster state
# to do anything useful, and starting it pre-init would just no-op
# in a tight retry loop).

log "Installing bedrock-rust systemd unit..."
mkdir -p /etc/systemd/system /etc/bedrock /var/lib/bedrock/log
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-rust.service" \
    -o /etc/systemd/system/bedrock-rust.service
systemctl daemon-reload
# Enabled but NOT started — `bedrock init` / `bedrock join` writes
# /etc/bedrock/daemon.toml first, then starts the service. Starting
# it without a config file would just fail-loop.
systemctl enable bedrock-rust.service >/dev/null 2>&1 || true

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
systemctl enable bedrock-rqlited.service >/dev/null 2>&1 || true

# DRBD + libvirtd are NOT auto-started at boot. The mgmt service's
# orchestrator decides when it's safe (cluster contact established,
# role is leader/follower) and starts them imperatively. This is the
# fence-aware boot model documented in cluster-protocol-overview.md
# §"boot orchestration".
systemctl disable drbd >/dev/null 2>&1 || true
systemctl disable libvirtd >/dev/null 2>&1 || true

# Independent fence watchdog: reboots the node if /tmp/bedrock-rust.fence
# stays present for > 5 min, indicating mgmt's fence cleanup hung or
# crashed. Runs as a systemd timer every 30s; survives mgmt crashes
# because it doesn't depend on mgmt at all.
log "Installing bedrock-fence-watchdog..."
curl -fsSL "${BEDROCK_REPO}/bedrock-fence-watchdog" \
    -o /usr/local/bin/bedrock-fence-watchdog
chmod +x /usr/local/bin/bedrock-fence-watchdog
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-fence-watchdog.service" \
    -o /etc/systemd/system/bedrock-fence-watchdog.service
curl -fsSL "${BEDROCK_REPO}/configs/bedrock-fence-watchdog.timer" \
    -o /etc/systemd/system/bedrock-fence-watchdog.timer
systemctl daemon-reload
systemctl enable --now bedrock-fence-watchdog.timer >/dev/null 2>&1 || true

# Dashboard TLS cert refresh — pulls local-ip.co's wildcard cert
# every 24 h (OnBootSec=2min so a fresh install gets it in minutes).
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

# mDNS responder + port-80 redirector — any node on the LAN can be
# reached by browsing to `bedrock.local`, which then 302-redirects
# to that node's HTTPS dashboard URL.
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
)
for f in "${LIB_FILES[@]}"; do
    curl -fsSL -o "${LIB_DIR}/${f}" "${BEDROCK_REPO}/lib/${f}" \
        || die "Failed to fetch lib/${f}"
done

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
