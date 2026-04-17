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
    BEDROCK_REPO="http://192.168.2.145:8000"
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
)
for f in "${LIB_FILES[@]}"; do
    curl -fsSL -o "${LIB_DIR}/${f}" "${BEDROCK_REPO}/lib/${f}" \
        || die "Failed to fetch lib/${f}"
done

# ── Record repo location for future subcommands ────────────────────────────

mkdir -p /etc/bedrock
echo "BEDROCK_REPO=$BEDROCK_REPO" > /etc/bedrock/installer.env
chmod 600 /etc/bedrock/installer.env

# ── Run the Python bootstrap ──────────────────────────────────────────────

log "Running bedrock bootstrap..."
exec /usr/local/bin/bedrock bootstrap
