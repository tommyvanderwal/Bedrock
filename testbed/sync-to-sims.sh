#!/usr/bin/env bash
# sync-to-sims.sh — push current Python code to running sims.
#
# The bedrock-install ISO bakes installer/lib/ + bedrock_d/ + mgmt/
# at build time; iterating on those without rebuilding the ISO means
# scp-ing the changes to each running sim. This script automates that
# loop:
#
#   testbed/sync-to-sims.sh           # push to sims 1..4
#   testbed/sync-to-sims.sh 1 2       # push to specific sims
#   testbed/sync-to-sims.sh --restart # also restart bedrock-d on each
#
# Use during the rewrite to test individual code changes without the
# 20-minute reset → up → wait → init cycle. ISO rebuild stays the
# authoritative path for end-to-end "does a fresh install work" tests.
set -u
TESTBED=$(dirname "$(readlink -f "$0")")
REPO=$(cd "$TESTBED/.." && pwd)
cd "$REPO"

RESTART=0
TARGETS=()
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=1 ;;
        [1-4])     TARGETS+=("$arg") ;;
        *) echo "usage: $0 [--restart] [1] [2] [3] [4]"; exit 2 ;;
    esac
done
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=(1 2 3 4)
fi

# Build the tarballs once
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

tar czf "$TMPDIR/lib.tgz" --exclude=__pycache__ --exclude='*.pyc' -C installer lib
tar czf "$TMPDIR/mgmt.tgz" --exclude=__pycache__ --exclude='*.pyc' -C mgmt .
tar czf "$TMPDIR/bedrock_d.tgz" --exclude=__pycache__ --exclude='*.pyc' bedrock_d
cp installer/bedrock "$TMPDIR/bedrock"
cp installer/bedrock-d "$TMPDIR/bedrock-d"
cp installer/configs/bedrock-*.service "$TMPDIR/" 2>/dev/null

for n in "${TARGETS[@]}"; do
    ip=$(python3 -c "
import sys; sys.path.insert(0, '$TESTBED')
from spawn import get_mgmt_ip
print(get_mgmt_ip($n) or '')
")
    if [ -z "$ip" ]; then
        echo "✗ sim-$n: no IP (not running?)"
        continue
    fi
    echo "── sim-$n ($ip) ──"
    # Push everything in one scp + extract pass
    scp -q -o StrictHostKeyChecking=no \
        "$TMPDIR/lib.tgz" "$TMPDIR/mgmt.tgz" "$TMPDIR/bedrock_d.tgz" \
        "$TMPDIR/bedrock" "$TMPDIR/bedrock-d" \
        "$TMPDIR"/bedrock-*.service \
        "root@$ip:/tmp/" 2>&1 | sed 's/^/  /'
    ssh -o StrictHostKeyChecking=no -o BatchMode=yes "root@$ip" "
        cp /tmp/bedrock /usr/local/bin/bedrock && chmod +x /usr/local/bin/bedrock
        cp /tmp/bedrock-d /usr/local/bin/bedrock-d && chmod +x /usr/local/bin/bedrock-d
        tar xzf /tmp/lib.tgz -C /usr/local/lib/bedrock/
        mkdir -p /opt/bedrock/mgmt
        tar xzf /tmp/mgmt.tgz -C /opt/bedrock/mgmt/
        tar xzf /tmp/bedrock_d.tgz -C /usr/local/lib/bedrock/
        for s in /tmp/bedrock-*.service; do
            [ -f \"\$s\" ] && cp \"\$s\" /etc/systemd/system/
        done
        systemctl daemon-reload
        if [ $RESTART = 1 ]; then
            systemctl restart bedrock-d 2>/dev/null
            echo \"  bedrock-d: \$(systemctl is-active bedrock-d)\"
        fi
    "
done
echo ""
echo "done."
