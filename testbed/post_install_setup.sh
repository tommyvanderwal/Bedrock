#!/usr/bin/env bash
# Per-sim post-install setup: drop the dev box's SSH pubkey into root's
# authorized_keys so the test scripts can use key-based SSH from here on.
# Idempotent; safe to re-run.
#
# Usage:
#   sshpass -p bedrock ssh ... root@<sim-ip> < post_install_setup.sh
#   or
#   testbed/post_install_setup.sh sim_index
set -euo pipefail
PUBKEY="$(cat ~/.ssh/id_ed25519.pub)"
i="${1:-}"
if [ -z "$i" ]; then
    # Run-locally mode: stdin = the pubkey, install it.
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    if ! grep -qxF "$PUBKEY" /root/.ssh/authorized_keys 2>/dev/null; then
        echo "$PUBKEY" >> /root/.ssh/authorized_keys
    fi
    chmod 600 /root/.ssh/authorized_keys
    echo "OK pubkey installed"
    exit 0
fi

# Remote-driver mode: SSH into sim-$i with password and inject pubkey.
ip="192.168.2.$((200 + i))"
sshpass -p bedrock ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o ConnectTimeout=10 \
    "root@${ip}" \
    "mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
     grep -qxF '${PUBKEY}' /root/.ssh/authorized_keys 2>/dev/null || \
       echo '${PUBKEY}' >> /root/.ssh/authorized_keys && \
     chmod 600 /root/.ssh/authorized_keys && echo OK"
