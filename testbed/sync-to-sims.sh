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

# Post-sync verification: md5 three sentinel files (one per tarball) so a
# silent extraction miss FAILS LOUDLY instead of leaving stale code running.
# A transient mgmt.tgz miss on 2026-05-29 ran the old orchestrator.py and
# faked a reboot-resilience FAIL — never again.
EXPECT_ORCH=$(md5sum mgmt/orchestrator.py | cut -d' ' -f1)
EXPECT_STATE=$(md5sum installer/lib/state.py | cut -d' ' -f1)
EXPECT_VMFAIL=$(md5sum bedrock_d/orchestrator/vm_failover.py | cut -d' ' -f1)
EXPECT_BACKUP=$(md5sum mgmt/backup.py | cut -d' ' -f1)
EXPECT_APP=$(md5sum mgmt/app.py | cut -d' ' -f1)
SYNC_FAIL=0

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
        # Verify the three sentinel files actually landed (catch silent
        # extraction misses before they masquerade as product failures).
        GOT_ORCH=\$(md5sum /opt/bedrock/mgmt/orchestrator.py 2>/dev/null | cut -d' ' -f1)
        GOT_STATE=\$(md5sum /usr/local/lib/bedrock/lib/state.py 2>/dev/null | cut -d' ' -f1)
        GOT_VMFAIL=\$(md5sum /usr/local/lib/bedrock/bedrock_d/orchestrator/vm_failover.py 2>/dev/null | cut -d' ' -f1)
        GOT_BACKUP=\$(md5sum /opt/bedrock/mgmt/backup.py 2>/dev/null | cut -d' ' -f1)
        GOT_APP=\$(md5sum /opt/bedrock/mgmt/app.py 2>/dev/null | cut -d' ' -f1)
        VOK=1
        [ \"\$GOT_ORCH\"   = \"$EXPECT_ORCH\"   ] || { echo \"  ✗ VERIFY: orchestrator.py mismatch (\$GOT_ORCH != $EXPECT_ORCH)\"; VOK=0; }
        [ \"\$GOT_STATE\"  = \"$EXPECT_STATE\"  ] || { echo \"  ✗ VERIFY: state.py mismatch\"; VOK=0; }
        [ \"\$GOT_VMFAIL\" = \"$EXPECT_VMFAIL\" ] || { echo \"  ✗ VERIFY: vm_failover.py mismatch\"; VOK=0; }
        [ \"\$GOT_BACKUP\" = \"$EXPECT_BACKUP\" ] || { echo \"  ✗ VERIFY: backup.py mismatch (\$GOT_BACKUP != $EXPECT_BACKUP)\"; VOK=0; }
        [ \"\$GOT_APP\"    = \"$EXPECT_APP\"    ] || { echo \"  ✗ VERIFY: app.py mismatch\"; VOK=0; }
        [ \$VOK = 1 ] && echo \"  ✓ deploy verified\" || { echo \"  ✗ DEPLOY VERIFY FAILED — stale code\"; exit 7; }
        systemctl daemon-reload
        if [ $RESTART = 1 ]; then
            systemctl restart bedrock-d 2>/dev/null
            echo \"  bedrock-d: \$(systemctl is-active bedrock-d)\"
        fi
    " || SYNC_FAIL=1
done
echo ""
if [ "$SYNC_FAIL" = 1 ]; then
    echo "✗✗ SYNC FAILED on at least one node — deployed code is STALE. Fix before testing." >&2
    exit 1
fi
echo "done (deploy verified on all targets)."
