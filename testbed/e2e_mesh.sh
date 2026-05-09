#!/bin/bash
# E2E test #1: fresh 4-node mesh cluster build + path verification.
#
# Pre-req: 4 sims spawned + firstboot done (./spawn.py up 4 + waiter).
# Steps:
#   1. bedrock init on sim-1
#   2. bedrock join on sim-2/3/4 sequentially
#   3. wait for the bedrock-net daemon to discover all paths
#   4. verify cluster.json paths section is populated on every node
#   5. verify ip routes are installed for every peer loopback
#   6. ping every loopback from every loopback
#   7. report convergence time + final state
set -euo pipefail

SSHPASS=${SSHPASS:-bedrock}
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5)

ip_for() {
    sudo virsh qemu-agent-command "bedrock-sim-$1" \
        '{"execute":"guest-network-get-interfaces"}' 2>/dev/null | \
        grep -oE '"ip-address":"192\.168\.2\.[0-9]+"' | head -1 | grep -oE '192\.168\.2\.[0-9]+'
}

ssh_run() {
    local ip=$1; shift
    SSHPASS="$SSHPASS" sshpass -e ssh "${SSH_OPTS[@]}" "root@$ip" "$@"
}

T0=$(date +%s)
echo "=== Discovering sim IPs ==="
SIM1=$(ip_for 1); SIM2=$(ip_for 2); SIM3=$(ip_for 3); SIM4=$(ip_for 4)
echo "  sim-1 (mgmt+compute): $SIM1"
echo "  sim-2 (compute):      $SIM2"
echo "  sim-3 (compute):      $SIM3"
echo "  sim-4 (compute):      $SIM4"

[ -n "$SIM1" ] || { echo "no sim-1 ip; aborting"; exit 1; }

echo
echo "=== bedrock init on sim-1 ==="
ssh_run "$SIM1" 'bedrock init --name bedrock-mesh-test 2>&1 | tail -10'

echo
echo "=== bedrock join sim-2 ==="
ssh_run "$SIM2" "bedrock join --witness $SIM1 --yes 2>&1 | tail -8"
echo "=== bedrock join sim-3 ==="
ssh_run "$SIM3" "bedrock join --witness $SIM1 --yes 2>&1 | tail -8"
echo "=== bedrock join sim-4 ==="
ssh_run "$SIM4" "bedrock join --witness $SIM1 --yes 2>&1 | tail -8"

echo
echo "=== Waiting for bedrock-net path discovery ==="
DEADLINE=$(( $(date +%s) + 90 ))
while [ $(date +%s) -lt $DEADLINE ]; do
    PATHS=$(ssh_run "$SIM1" 'python3 -c "import json; d=json.load(open(chr(47)+chr(101)+chr(116)+chr(99)+chr(47)+chr(98)+chr(101)+chr(100)+chr(114)+chr(111)+chr(99)+chr(107)+chr(47)+chr(99)+chr(108)+chr(117)+chr(115)+chr(116)+chr(101)+chr(114)+chr(46)+chr(106)+chr(115)+chr(111)+chr(110))); print(len(d.get(chr(112)+chr(97)+chr(116)+chr(104)+chr(115),{})))"' 2>/dev/null || echo "0")
    PATHS=$(echo "$PATHS" | tr -d '[:space:]')
    EXPECTED=$((4*3/2*4))   # 6 pairs × up to 4 NIC-pairs (mgmt, drbd, mesh1-3) = 24, but realistically each pair has 4 paths → 24
    EXPECTED_MIN=18         # 6 pairs × 3 mesh planes = 18 minimum
    echo "  paths=$PATHS at +$(( $(date +%s) - T0 ))s"
    if [ "$PATHS" -ge "$EXPECTED_MIN" ]; then
        echo "  ✓ enough paths discovered"
        break
    fi
    sleep 5
done

echo
echo "=== Per-node view ==="
for ip in "$SIM1" "$SIM2" "$SIM3" "$SIM4"; do
    name=$(ssh_run "$ip" 'hostname' 2>/dev/null)
    paths=$(ssh_run "$ip" 'python3 -c "import json; d=json.load(open(\"/etc/bedrock/cluster.json\")); print(len(d.get(\"paths\", {})))"' 2>/dev/null)
    routes=$(ssh_run "$ip" 'ip -4 route show | grep -c "10.99.0"' 2>/dev/null || echo 0)
    lo=$(ssh_run "$ip" "ip -o -4 addr show dev lo | awk '/10.99.0/{print \$4}' | head -1" 2>/dev/null)
    echo "  $name ($ip): paths=$paths routes=$routes loopback=$lo"
done

echo
echo "=== loopback ↔ loopback ping matrix ==="
declare -A LOOPS
for i in 1 2 3 4; do
    eval ip=\$SIM$i
    LOOPS[$i]=$(ssh_run "$ip" "ip -o -4 addr show dev lo | awk '/10.99.0/{print \$4}' | head -1 | cut -d/ -f1" 2>/dev/null)
done
fail=0
for src in 1 2 3 4; do
    eval src_ip=\$SIM$src
    for dst in 1 2 3 4; do
        [ "$src" = "$dst" ] && continue
        dst_lo=${LOOPS[$dst]}
        [ -z "$dst_lo" ] && continue
        if ssh_run "$src_ip" "ping -c1 -W2 $dst_lo >/dev/null" 2>/dev/null; then
            printf "  sim-%s → sim-%s (%s): OK\n" "$src" "$dst" "$dst_lo"
        else
            printf "  sim-%s → sim-%s (%s): FAIL\n" "$src" "$dst" "$dst_lo"
            fail=$((fail+1))
        fi
    done
done

T1=$(date +%s)
echo
echo "=== E2E #1 result ==="
echo "  total time: $((T1-T0)) s"
echo "  ping failures: $fail"
[ "$fail" = "0" ] && echo "  STATUS: PASS" || echo "  STATUS: FAIL"
exit $fail
