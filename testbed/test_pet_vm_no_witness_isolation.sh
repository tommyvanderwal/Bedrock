#!/usr/bin/env bash
# Pet-VM failover — "both nodes isolated, no witness" scenarios.
#
# Pre-condition: 4-node cluster with critical tier on DRBD. Run
# setup_4node_cluster.sh first.
#
# Tests three scenarios from Tommy's 2026-05-26 spec:
#
#   Scenario A — partition <5min, no peer promoted
#     - Stop echo (no witness)
#     - virsh destroy sims 2+3 (so sim-1 + sim-4 lose quorum: their
#       2 nodes = 20 votes < majority 21, witness dead)
#     - sim-1 + sim-4 stay connected to each other (DRBD ok)
#     - Expect: sim-1 suspends pet VM at T+~20s; sim-4 does NOT
#       take over (rqlite NoQuorum gates the takeover); DRBD UUID
#       stays the same on both sides
#     - Restore sims 2+3 + echo at T+60s (well under 5 min kill)
#     - Expect: sim-1 resumes the paused VM; DRBD UUID still the
#       same (no resync, no promote happened)
#
#   Scenario B — partition >5min, isolated host kills its suspended VMs
#     - Same isolation setup
#     - Wait T+330s (5.5 min)
#     - Expect: at T+~5min sim-1's kill_suspended_after_5min_task
#       virsh-destroys the paused VM
#     - Restore at T+360s
#     - (post-restore behavior of a killed VM is operator-driven —
#       this test just confirms the kill happened)
#
#   Scenario C — already covered by test_pet_vm_failover.sh
#
# Usage:
#   testbed/test_pet_vm_no_witness_isolation.sh A    # scenario A only
#   testbed/test_pet_vm_no_witness_isolation.sh B    # scenario B only
#   testbed/test_pet_vm_no_witness_isolation.sh AB   # both (default)

set -u
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED"

WHICH="${1:-AB}"

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

_CM_DIR=${CM_DIR:-/tmp/bedrock-no-wit-cm}
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
    # Pipe body via stdin to avoid multi-level shell-escape mess. mTLS
    # post the 2026-05-26 TLS rollout.
    local node=$1 sql=$2
    local body
    body=$(python3 -c 'import json,sys;print(json.dumps([sys.argv[1]]))' "$sql")
    local ip; ip=$(sim_ip "$node")
    printf '%s' "$body" | ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o ControlMaster=auto -o ControlPath="$_CM_DIR/%r@%h:%p" -o ControlPersist=300 \
        "root@${ip}" "curl -fsSL 'https://127.0.0.1:4001/db/query?level=strong' \
            --cacert /etc/bedrock/ca.crt \
            --cert /etc/bedrock/node.crt --key /etc/bedrock/node.key.pem \
            --data-binary @- 2>/dev/null \
        | python3 -c 'import sys,json
r=json.load(sys.stdin)[\"results\"][0]
vals=r.get(\"values\") or []
print(vals[0][0] if vals else \"\")'"
}

drbd_uuid_on() {
    local node=$1 resource=$2
    sssh "$node" "cat /sys/kernel/debug/drbd/resources/$resource/volumes/0/data_gen_id 2>/dev/null | head -1 | sed 's/^0x//'"
}

PET_NAME="pet-no-witness-test"

# ─────────────────────────────────────────────────────────────────
step "Pre: confirm cluster healthy + DRBD"
TIER_MODE=$(rqlite_query 1 "SELECT mode FROM tiers WHERE tier_name='critical'")
if [ "$TIER_MODE" != "drbd" ]; then
    fail "critical tier mode = '$TIER_MODE' (need 'drbd')"; exit 1
fi
pass "critical tier = drbd"

step "1. Create pet VM on sim-1 (skip if it already exists from a previous run)"
EXISTS=$(rqlite_query 1 "SELECT vm_name FROM vms WHERE vm_name='$PET_NAME'")
if [ -z "$EXISTS" ]; then
    sssh 1 "bedrock vm create $PET_NAME --type pet --ram 512 --disk 2 2>&1 | tail -3" \
        || mark_fail "vm create returned non-zero"
    sssh 1 "virsh start $PET_NAME 2>&1 | tail -2" || note "virsh start non-zero"
else
    note "pet VM row exists; using it"
    if ! vm_running_on "$PET_NAME" 1; then
        sssh 1 "virsh start $PET_NAME 2>&1 | tail -2" || note "virsh start non-zero"
    fi
fi

# Wait for VM running
note "wait up to 60s for VM running on sim-1"
RUN_AT=0
for t in 10 20 30 40 50 60; do
    sleep 10
    if vm_running_on "$PET_NAME" 1; then RUN_AT=$t; break; fi
done
[ $RUN_AT -gt 0 ] && pass "pet VM running on sim-1 within ${RUN_AT}s" \
    || { mark_fail "pet VM never reached running"; exit 1; }

# Discover peer
ORDER=$(rqlite_query 1 "SELECT failover_order FROM vms WHERE vm_name='$PET_NAME'")
PEER_NAME=$(echo "$ORDER" | python3 -c 'import json,sys;o=json.loads(sys.stdin.read());print(o[1] if len(o)>1 else "")')
PEER_IP=$(rqlite_query 1 "SELECT host FROM nodes WHERE node_name='$PEER_NAME'")
PEER_SIM=""
for i in 1 2 3 4; do [ "$(sim_ip $i)" = "$PEER_IP" ] && PEER_SIM=$i && break; done
[ -n "$PEER_SIM" ] && pass "DRBD peer: $PEER_NAME = sim-$PEER_SIM" \
    || { mark_fail "could not resolve peer"; exit 1; }

# Identify "other" sims that we'll knock out to break quorum
OTHER_SIMS=()
for i in 1 2 3 4; do
    [ "$i" = "1" ] && continue
    [ "$i" = "$PEER_SIM" ] && continue
    OTHER_SIMS+=("$i")
done
note "other sims (will be destroyed to drop quorum): ${OTHER_SIMS[*]}"

# Wait DRBD UpToDate both sides. First-sync is slow on the testbed
# bridge (~25 min for 2 GB) so we allow up to 30 min. Check the
# PEER side's local disk specifically — `disk:UpToDate` substring
# alone matches against `peer-disk:UpToDate` which the Primary
# emits as soon as the Secondary CLAIMS UpToDate even though the
# replication is mid-sync.
note "wait DRBD UpToDate on sim-1 + sim-$PEER_SIM (up to 30 min for first-sync)"
DRBD_OK=0
for t in $(seq 30 30 1800); do
    sleep 30
    s1=$(sssh 1 "drbdadm status vm-$PET_NAME-disk0 2>/dev/null" || echo "")
    sP=$(sssh $PEER_SIM "drbdadm status vm-$PET_NAME-disk0 2>/dev/null" || echo "")
    # Both sides must report their LOCAL disk as UpToDate (the
    # first 'disk:' line in each output is the local side).
    s1_local=$(echo "$s1" | awk '/disk:/{print; exit}')
    sP_local=$(echo "$sP" | awk '/disk:/{print; exit}')
    if echo "$s1_local" | grep -q "disk:UpToDate" && \
       echo "$sP_local" | grep -q "disk:UpToDate"; then
        DRBD_OK=$t; break
    fi
done
[ $DRBD_OK -gt 0 ] && pass "DRBD UpToDate on both sides at ${DRBD_OK}s" \
    || { mark_fail "DRBD never reached UpToDate"; exit 1; }

# Snapshot the pre-isolation DRBD UUID — must NOT change in scenario A
PRE_UUID_SIM1=$(drbd_uuid_on 1 "vm-$PET_NAME-disk0")
PRE_UUID_PEER=$(drbd_uuid_on $PEER_SIM "vm-$PET_NAME-disk0")
note "pre-isolation DRBD UUIDs: sim-1=$PRE_UUID_SIM1  sim-$PEER_SIM=$PRE_UUID_PEER"

# ═════════════════════════════════════════════════════════════════
# SCENARIO A: short isolation, no peer promoted, resume on reconnect
# ═════════════════════════════════════════════════════════════════
if echo "$WHICH" | grep -q A; then

step "A.1. Stop echo (no witness) + virsh destroy sims ${OTHER_SIMS[*]}"
pkill -f "bedrock_echo_stub" 2>/dev/null
sleep 1
for n in "${OTHER_SIMS[@]}"; do
    virsh destroy bedrock-sim-$n 2>&1 | tail -1
done
ISOLATE_T0=$(date +%s)
note "echo stopped + sims ${OTHER_SIMS[*]} destroyed at T+0"

step "A.2. Wait for sim-1 to suspend pet VM (should be ~T+20-30s)"
SUSPEND_AT=0
for t in 10 15 20 25 30 40 50 60; do
    sleep 5
    if vm_paused_on "$PET_NAME" 1; then
        SUSPEND_AT=$(($(date +%s) - ISOLATE_T0))
        break
    fi
done
[ $SUSPEND_AT -gt 0 ] && pass "sim-1 suspended pet VM at T+${SUSPEND_AT}s" \
    || mark_fail "sim-1 did NOT suspend pet VM within 60s"

step "A.3. Verify sim-$PEER_SIM did NOT take over (no rqlite quorum → takeover refuses)"
sleep 60   # well past TAKEOVER_AFTER_PEER_DOWN_S=35s
if vm_running_on "$PET_NAME" $PEER_SIM; then
    mark_fail "SPLIT BRAIN — sim-$PEER_SIM started pet VM despite no rqlite quorum"
else
    pass "sim-$PEER_SIM correctly DID NOT take over (waited 60s post-isolation)"
fi
MID_UUID_PEER=$(drbd_uuid_on $PEER_SIM "vm-$PET_NAME-disk0")
if [ "$MID_UUID_PEER" = "$PRE_UUID_PEER" ]; then
    pass "sim-$PEER_SIM DRBD UUID unchanged (no promote happened)"
else
    mark_fail "sim-$PEER_SIM DRBD UUID changed ($PRE_UUID_PEER → $MID_UUID_PEER) — peer promoted!"
fi

step "A.4. Restore: virsh start sims ${OTHER_SIMS[*]} + restart echo"
for n in "${OTHER_SIMS[@]}"; do
    virsh start bedrock-sim-$n 2>&1 | tail -1
done
# Restart echo with cluster.key
CLUSTER_KEY_HEX=$(sssh 1 'python3 -c "print(open(\"/etc/bedrock/cluster.key\",\"rb\").read().hex())"')
ECHO_LOG=/tmp/bedrock-echo-stub.log
nohup python3 "$TESTBED/bedrock_echo_stub.py" --cluster-key-hex "$CLUSTER_KEY_HEX" \
    --echo-id "testbed-echo" --verbose > "$ECHO_LOG" 2>&1 < /dev/null &
sleep 5
pgrep -f bedrock_echo_stub >/dev/null && pass "echo restarted" || mark_fail "echo failed to restart"

step "A.5. Wait for sim-1's recovery path to resume the paused VM"
RESUME_AT=0
T_RESTORE=$(date +%s)
for t in 10 20 30 40 60 90 120; do
    sleep 10
    if vm_running_on "$PET_NAME" 1; then
        RESUME_AT=$(($(date +%s) - T_RESTORE))
        break
    fi
done
[ $RESUME_AT -gt 0 ] && pass "sim-1 RESUMED pet VM within ${RESUME_AT}s of restore" \
    || mark_fail "sim-1 did NOT resume pet VM within 120s of restore"

# UUID must STILL be unchanged — no promote happened on either side
POST_UUID_SIM1=$(drbd_uuid_on 1 "vm-$PET_NAME-disk0")
POST_UUID_PEER=$(drbd_uuid_on $PEER_SIM "vm-$PET_NAME-disk0")
note "post-restore DRBD UUIDs: sim-1=$POST_UUID_SIM1  sim-$PEER_SIM=$POST_UUID_PEER"
if [ "$POST_UUID_SIM1" = "$PRE_UUID_SIM1" ] && [ "$POST_UUID_PEER" = "$PRE_UUID_PEER" ]; then
    pass "DRBD UUIDs unchanged on both sides (no resync needed)"
else
    mark_fail "DRBD UUID changed — a resync happened (pre=$PRE_UUID_SIM1/$PRE_UUID_PEER, post=$POST_UUID_SIM1/$POST_UUID_PEER)"
fi

fi   # WHICH=A

# ═════════════════════════════════════════════════════════════════
# SCENARIO B: long isolation (>5min), VM gets killed
# ═════════════════════════════════════════════════════════════════
if echo "$WHICH" | grep -q B; then

step "B.1. Re-isolate (echo down + sims ${OTHER_SIMS[*]} destroyed) for 5.5 min"
pkill -f "bedrock_echo_stub" 2>/dev/null
sleep 1
for n in "${OTHER_SIMS[@]}"; do
    virsh destroy bedrock-sim-$n 2>&1 | tail -1
done
ISOLATE_T0=$(date +%s)

note "wait T+330s (5.5 min — past KILL_AFTER_SUSPEND_S=300s)"
sleep 60
note "  T+60s — sim-1 should be paused"
vm_paused_on "$PET_NAME" 1 && pass "sim-1 paused at T+60s" || note "sim-1 not paused yet"
sleep 270   # total 330s
elapsed=$(($(date +%s) - ISOLATE_T0))
note "elapsed T+${elapsed}s — checking kill"

step "B.2. Verify sim-1 killed the suspended pet VM (virsh destroyed it)"
# After kill, VM is neither running nor paused on sim-1
if vm_running_on "$PET_NAME" 1 || vm_paused_on "$PET_NAME" 1; then
    mark_fail "sim-1 did NOT kill the suspended pet VM after 5+ min"
else
    pass "sim-1 killed (virsh destroyed) the pet VM after 5min suspend"
fi

step "B.3. Restore"
for n in "${OTHER_SIMS[@]}"; do
    virsh start bedrock-sim-$n 2>&1 | tail -1
done
CLUSTER_KEY_HEX=$(sssh 1 'python3 -c "print(open(\"/etc/bedrock/cluster.key\",\"rb\").read().hex())"')
nohup python3 "$TESTBED/bedrock_echo_stub.py" --cluster-key-hex "$CLUSTER_KEY_HEX" \
    --echo-id "testbed-echo" --verbose > /tmp/bedrock-echo-stub.log 2>&1 < /dev/null &
sleep 30
note "scenario B done; post-restore VM state is operator-driven"

fi   # WHICH=B

# ─────────────────────────────────────────────────────────────────
echo
if [ $ALL_PASS = 1 ]; then
    echo "${C_G}━━ ALL PASS ━━${C_0}"
    exit 0
else
    echo "${C_R}━━ AT LEAST ONE FAIL ━━${C_0}"
    exit 1
fi
