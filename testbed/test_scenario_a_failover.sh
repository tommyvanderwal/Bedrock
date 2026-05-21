#!/usr/bin/env bash
# Scenario A failover: fully isolate the current master, confirm the
# survivor takes over via the witness, then heal and verify rqlite
# correctly records the takeover (the LMS-write reconcile path).
#
# Usage:
#   testbed/test_scenario_a_failover.sh
#
# Pre-condition: 2-node cluster already initialised (sim-1 + sim-2),
# bedrock-d healthy on both, witness reachable from both, .254
# bound on whichever sim ran ``bedrock init``.
set -u
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED/.."

C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
pass() { echo "${C_G}PASS${C_0} $*"; }
fail() { echo "${C_R}FAIL${C_0} $*"; }
note() { echo "${C_Y}---${C_0} $*"; }

SIM1_IP=$(python3 -c "import sys; sys.path.insert(0,'testbed'); from spawn import get_mgmt_ip; print(get_mgmt_ip(1) or '')")
SIM2_IP=$(python3 -c "import sys; sys.path.insert(0,'testbed'); from spawn import get_mgmt_ip; print(get_mgmt_ip(2) or '')")
DEVBOX_IP=192.168.2.193     # this host — also runs the witness stub
[ -z "$SIM1_IP" ] && fail "sim-1 not running" && exit 1
[ -z "$SIM2_IP" ] && fail "sim-2 not running" && exit 1
note "sim-1=$SIM1_IP  sim-2=$SIM2_IP  devbox(=witness)=$DEVBOX_IP"

# Detect current master by who holds .254
MASTER_IP=""
SURVIVOR_IP=""
for ip in $SIM1_IP $SIM2_IP; do
    if ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=3 \
            "root@$ip" "ip -4 addr show lo | grep -q '100.69.150.254'" 2>/dev/null; then
        MASTER_IP=$ip
    else
        SURVIVOR_IP=$ip
    fi
done
[ -z "$MASTER_IP" ] && fail "no node holds .254 — cluster not in steady state" && exit 1
note "current master: $MASTER_IP   survivor-to-be: $SURVIVOR_IP"

# Snapshot pre-test rqlite mgmt_master row (for the reconcile assertion)
PRE_MASTER=$(ssh "root@$MASTER_IP" \
    'curl -sS http://127.0.0.1:4001/db/query --data-urlencode "q=SELECT mgmt_master FROM cluster_info WHERE id=1" 2>/dev/null' \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['results'][0]['values'][0][0])" 2>/dev/null)
note "pre-test rqlite mgmt_master = $PRE_MASTER"

# ── 1. Full isolation of MASTER ────────────────────────────────────────
note "Step 1: drop-all iptables on master (DROP INPUT/OUTPUT except lo + SSH-back-from-devbox)"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "root@$MASTER_IP" "
iptables -F INPUT
iptables -F OUTPUT
iptables -P INPUT DROP
iptables -P OUTPUT DROP
# loopback OK
iptables -A INPUT  -i lo -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
# allow my SSH session back from the devbox so I can restore
iptables -A INPUT  -s $DEVBOX_IP -p tcp --dport 22 -j ACCEPT
iptables -A OUTPUT -d $DEVBOX_IP -p tcp --sport 22 -j ACCEPT
echo '  master firewall rules ready'
"
ISO_START=$(date +%s)

# ── 2. Watch for failover ─────────────────────────────────────────────
note "Step 2: poll up to 60s for master to drop .254 and survivor to bind it"
DEMOTED=0
PROMOTED=0
for t in 5 10 15 20 25 30 35 40 45 50 55 60; do
    sleep 5
    m=$(ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=3 \
           "root@$MASTER_IP"   "ip -4 -br addr show lo 2>/dev/null | grep -oE '100\.[0-9]+\.[0-9]+\.[0-9]+' | tr '\n' ' '" 2>/dev/null)
    s=$(ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=3 \
           "root@$SURVIVOR_IP" "ip -4 -br addr show lo 2>/dev/null | grep -oE '100\.[0-9]+\.[0-9]+\.[0-9]+' | tr '\n' ' '" 2>/dev/null)
    echo "  t=${t}s   master: $m  |  survivor: $s"
    echo "$m" | grep -qv "100.69.150.254" && DEMOTED=$t
    echo "$s" | grep -q  "100.69.150.254" && { PROMOTED=$t; break; }
done

if [ $DEMOTED -gt 0 ]; then pass "master demoted .254 within ${DEMOTED}s"; else fail "master never released .254"; fi
if [ $PROMOTED -gt 0 ]; then pass "survivor bound .254 within ${PROMOTED}s"; else fail "survivor never took .254"; fi

# ── 3. Heal the partition ─────────────────────────────────────────────
note "Step 3: restore master connectivity (flush iptables)"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes "root@$MASTER_IP" "
iptables -F INPUT
iptables -F OUTPUT
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
echo '  master firewall cleared'
"

# ── 4. Verify rqlite recorded the takeover ────────────────────────────
note "Step 4: poll up to 30s for rqlite cluster_info.mgmt_master to reflect the new master"
SURVIVOR_NAME=$(ssh "root@$SURVIVOR_IP" 'hostname')
RECORDED=""
for t in 5 10 15 20 25 30; do
    sleep 5
    RECORDED=$(ssh "root@$SURVIVOR_IP" \
        'curl -sS http://127.0.0.1:4001/db/query --data-urlencode "q=SELECT mgmt_master FROM cluster_info WHERE id=1" 2>/dev/null' \
        | python3 -c "import sys,json
try:
    d=json.load(sys.stdin); print(d['results'][0]['values'][0][0])
except Exception: print('')" 2>/dev/null)
    echo "  t=${t}s   rqlite mgmt_master = '$RECORDED'   (expect '$SURVIVOR_NAME')"
    [ "$RECORDED" = "$SURVIVOR_NAME" ] && break
done
[ "$RECORDED" = "$SURVIVOR_NAME" ] && pass "rqlite recorded LMS takeover" \
    || fail "rqlite still names old master ('$RECORDED' != '$SURVIVOR_NAME')"

# ── 5. Cluster steady-state ───────────────────────────────────────────
note "Step 5: 10s settle, then both nodes should see one master + healthy neighbours"
sleep 10
for ip in $MASTER_IP $SURVIVOR_IP; do
    n=$(ssh "root@$ip" "ip -4 -br addr show lo | grep -oE '100\.65\.75\.[0-9]+' | tr '\n' ' '")
    echo "  $ip: lo = $n"
done

note "── Scenario A complete ──"
