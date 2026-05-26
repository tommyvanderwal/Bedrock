#!/usr/bin/env bash
# Pet-VM failover end-to-end test.
#
# Pre-condition: 4-node cluster already initialised, storage tier
# promoted to DRBD. Run after the main test_e2e_offline.sh has
# finished steps 5b (storage promote) and the cluster is healthy.
#
# Scenario:
#   1. Create a pet VM on sim-1 (`failover_order=[sim-1, sim-2]`).
#   2. Wait for the VM to be running.
#   3. Isolate sim-1's mesh NICs (network partition; leave LAN/SSH
#      from the workstation alive so we can observe).
#   4. Watch the suspend-on-no-quorum task suspend the VM on sim-1
#      within ~30 s of isolation start (T+20 + tick slack).
#   5. Watch the takeover-after-peer-down task on sim-2 drbdadm-primary
#      the resource, write the new UUID, and `virsh start` within
#      ~50 s of isolation start (T+35 + tick + start latency).
#   6. Verify the pet VM is running on sim-2 and NOT on sim-1.
#   7. Restore sim-1's network and verify it doesn't steal the VM
#      back (vms.host in rqlite now says sim-2; the boot/reactor
#      paths must respect it).
#
# Usage:
#   testbed/test_pet_vm_failover.sh
#
# Exit code: 0 if every assertion passes, 1 otherwise.

set -u
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED"

C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'
pass()  { echo "${C_G}PASS${C_0} $*"; }
fail()  { echo "${C_R}FAIL${C_0} $*"; }
note()  { echo "${C_Y}--- ${C_0} $*"; }
step()  { echo; echo "${C_B}### $* ###${C_0}"; }

ALL_PASS=1
mark_fail() { ALL_PASS=0; fail "$@"; }

sim_ip() {
    python3 -c "
import sys; sys.path.insert(0,'$TESTBED')
from spawn import get_mgmt_ip
print(get_mgmt_ip($1) or '')
"
}

_CM_DIR=${CM_DIR:-/tmp/bedrock-petvm-cm}
mkdir -p "$_CM_DIR" 2>/dev/null
sssh() {
    local node=$1; shift
    local ip; ip=$(sim_ip "$node")
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o ControlMaster=auto -o ControlPath="$_CM_DIR/%r@%h:%p" -o ControlPersist=300 \
        "root@${ip}" "$@"
}

vm_running_on() {
    local vm=$1 node=$2
    sssh "$node" "virsh list --state-running --name 2>/dev/null | grep -qx '$vm'"
}

vm_paused_on() {
    local vm=$1 node=$2
    sssh "$node" "virsh list --state-paused --name 2>/dev/null | grep -qx '$vm'"
}

rqlite_query() {
    local node=$1 sql=$2
    sssh "$node" "curl -fsSL 'http://127.0.0.1:4001/db/query?level=strong' \
        -d '[\"$sql\"]' 2>/dev/null \
        | python3 -c 'import sys,json; r=json.load(sys.stdin)[\"results\"][0]; \
                      vals=r.get(\"values\") or []; \
                      print(vals[0][0] if vals else \"\")'"
}

# ──────────────────────────────────────────────
PET_NAME="pet-failover-test"

step "Pre: confirm 4 sims healthy + storage tier = drbd"
for i in 1 2 3 4; do
    if ! sssh $i 'systemctl is-active --quiet bedrock-d bedrock-rqlited' 2>/dev/null; then
        fail "sim-$i not healthy"
        exit 1
    fi
done
TIER_MODE=$(rqlite_query 1 'SELECT mode FROM tiers WHERE tier_name=\\\"critical\\\"')
if [ "$TIER_MODE" != "drbd" ]; then
    fail "critical tier mode = '$TIER_MODE' (need 'drbd'). Run test_e2e_offline.sh first."
    exit 1
fi
pass "all 4 sims healthy + critical tier = drbd"

step "1. Create pet VM '$PET_NAME' on sim-1"
sssh 1 "bedrock vm create $PET_NAME --type pet --ram 512 --disk 2 2>&1 | tail -8" \
    || mark_fail "vm create returned non-zero"

note "wait up to 90s for VM to be running on sim-1"
RUNNING_AT_BOOT=0
for t in 10 20 30 40 50 60 70 80 90; do
    sleep 10
    if vm_running_on "$PET_NAME" 1; then
        RUNNING_AT_BOOT=$t
        break
    fi
done
if [ $RUNNING_AT_BOOT -gt 0 ]; then
    pass "pet VM running on sim-1 within ${RUNNING_AT_BOOT}s"
else
    mark_fail "pet VM NEVER started on sim-1 — aborting failover test"
    exit 1
fi

# Confirm failover_order is what we expect (sim-1, sim-2) — pet VMs
# get the home node + one peer per the VmCreate logic.
ORDER=$(rqlite_query 1 "SELECT failover_order FROM vms WHERE vm_name=\\\"$PET_NAME\\\"")
note "failover_order in rqlite: $ORDER"
if echo "$ORDER" | grep -q "bedrock-sim-2"; then
    pass "failover_order includes sim-2 (the takeover target)"
else
    mark_fail "failover_order = '$ORDER' — expected sim-2 as peer"
fi

# Snapshot the pre-isolation DRBD UUID for the takeover assertion
PRE_UUID=$(rqlite_query 1 "SELECT current_uuid FROM drbd_resources WHERE name=\\\"vm-$PET_NAME-disk0\\\"")
note "pre-isolation drbd_resources.current_uuid for vm-$PET_NAME-disk0 = '$PRE_UUID'"

# ─────────────────────────────────────────────────────────────────
step "2. Isolate sim-1's mesh NICs (network partition)"
WS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
note "workstation IP allowlisted on sim-1's br0: $WS_IP"

ISOLATE_T0=$(date +%s)
sssh 1 "bash -c '
iptables-save > /tmp/iptables-pre-petfailover 2>/dev/null
iptables -I OUTPUT -o br0 -j DROP
iptables -I INPUT  -i br0 -j DROP
iptables -I OUTPUT -o br0 -d $WS_IP -j ACCEPT
iptables -I INPUT  -i br0 -s $WS_IP -j ACCEPT
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do
    ip link set \$nic down 2>/dev/null
done
echo isolated
'" || mark_fail "iptables on sim-1 failed"

# ─────────────────────────────────────────────────────────────────
step "3. Watch for sim-1 to suspend the pet VM (T+20..30s)"
SUSPENDED_AT=0
for t in 10 15 20 25 30 35 40; do
    sleep 5
    if vm_paused_on "$PET_NAME" 1; then
        SUSPENDED_AT=$(($(date +%s) - ISOLATE_T0))
        break
    fi
done
if [ $SUSPENDED_AT -gt 0 ]; then
    pass "sim-1 suspended pet VM at T+${SUSPENDED_AT}s"
else
    mark_fail "sim-1 did NOT suspend pet VM within 40s — suspend_on_no_quorum_task broken?"
fi

# ─────────────────────────────────────────────────────────────────
step "4. Watch for sim-2 to take over (T+35..70s)"
TAKEOVER_AT=0
for t in 10 20 30 40 50 60 70; do
    sleep 10
    if vm_running_on "$PET_NAME" 2; then
        TAKEOVER_AT=$(($(date +%s) - ISOLATE_T0))
        break
    fi
done
if [ $TAKEOVER_AT -gt 0 ]; then
    pass "sim-2 has pet VM RUNNING at T+${TAKEOVER_AT}s"
else
    mark_fail "sim-2 did NOT start pet VM within 70s — takeover_after_peer_down_task broken?"
fi

# DRBD-UUID write-after-promote: the cluster's recorded UUID should
# now be different from PRE_UUID, because sim-2's drbdadm primary
# bumped it and record_uuid_after_promote wrote it to rqlite.
POST_UUID=$(rqlite_query 2 "SELECT current_uuid FROM drbd_resources WHERE name=\\\"vm-$PET_NAME-disk0\\\"")
note "post-takeover drbd_resources.current_uuid = '$POST_UUID'"
if [ -n "$POST_UUID" ] && [ "$POST_UUID" != "$PRE_UUID" ]; then
    pass "DRBD current_uuid recorded by sim-2 after promote ('$PRE_UUID' → '$POST_UUID')"
else
    mark_fail "DRBD current_uuid did NOT advance — record_uuid_after_promote silent fail"
fi

# vms.host in rqlite should now say sim-2 (set by _takeover_one's
# vm_state_change call).
NEW_HOST=$(rqlite_query 2 "SELECT host FROM vms WHERE vm_name=\\\"$PET_NAME\\\"")
note "vms.host = '$NEW_HOST'"
if echo "$NEW_HOST" | grep -q "bedrock-sim-2"; then
    pass "vms.host updated to sim-2"
else
    mark_fail "vms.host = '$NEW_HOST' — expected bedrock-sim-2"
fi

# ─────────────────────────────────────────────────────────────────
step "5. Restore sim-1's network"
sssh 1 "bash -c '
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do
    ip link set \$nic up 2>/dev/null
done
iptables -F INPUT
iptables -F OUTPUT
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
rm -f /run/bedrock-no-quorum
echo restored
'" || note "restore returned non-zero"

note "wait 45s for sim-1 to rejoin + reconcile"
sleep 45

# After rejoin, sim-1 should NOT have the pet VM running. The
# failover decision lives in rqlite; sim-1's recovery path should see
# vms.host=sim-2 and either keep the local suspended copy frozen
# (until the 5-min kill timer) or destroy it during reconcile.
#
# Cumulative pass condition: VM is running on sim-2 AND not running
# on sim-1.
if vm_running_on "$PET_NAME" 2; then
    pass "post-restore: pet VM still running on sim-2"
else
    mark_fail "post-restore: pet VM dropped off sim-2"
fi
if vm_running_on "$PET_NAME" 1; then
    mark_fail "post-restore: pet VM ALSO running on sim-1 — split-brain"
else
    pass "post-restore: pet VM is NOT running on sim-1 (no split-brain)"
fi

# ─────────────────────────────────────────────────────────────────
step "6. Cleanup: destroy + undefine the pet VM"
sssh 2 "virsh destroy $PET_NAME 2>/dev/null; virsh undefine $PET_NAME 2>/dev/null; \
        bedrock vm destroy $PET_NAME 2>&1 | tail -3" || true

echo
if [ $ALL_PASS = 1 ]; then
    echo "${C_G}━━ ALL PASS ━━${C_0}"
    exit 0
else
    echo "${C_R}━━ AT LEAST ONE FAIL ━━${C_0}"
    exit 1
fi
