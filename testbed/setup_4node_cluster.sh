#!/usr/bin/env bash
# Spawn or refresh a 4-node Bedrock testbed cluster to the point where
# DRBD critical tier is promoted and ready for VM-failover testing.
#
# Steps:
#   0. (caller is expected to have already run testbed/spawn.py up 4)
#   1. Pubkey + sync-to-sims push the current repo code
#   2. Start BedRock Echo stub on the workstation
#   3. bedrock init on sim-1 (1-node cluster)
#   4. bedrock join sim-2, 3, 4
#   5. bedrock storage promote (critical tier → DRBD)
#
# At the end, the cluster is N=4 healthy and ready for
# test_pet_vm_failover.sh to be run against it.
#
# Idempotent at the pubkey + sync stages; init/join will fail on
# already-joined nodes (run from scratch after spawn down/up).

set -u
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED/.."

C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'
pass()  { echo "${C_G}PASS${C_0} $*"; }
fail()  { echo "${C_R}FAIL${C_0} $*"; }
note()  { echo "${C_Y}--- ${C_0} $*"; }
step()  { echo; echo "${C_B}### $* ###${C_0}"; }

sim_ip() {
    python3 -c "
import sys; sys.path.insert(0,'$TESTBED')
from spawn import get_mgmt_ip
print(get_mgmt_ip($1) or '')
"
}

sssh() {
    local node=$1; shift
    local ip; ip=$(sim_ip "$node")
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 \
        "root@${ip}" "$@"
}

# ─────────────────────────────────────────────────────────────────
step "0. Confirm 4 sims are alive + .bootstrap-done"
for i in 1 2 3 4; do
    ip=$(sim_ip $i)
    if [ -z "$ip" ]; then fail "sim-$i not running"; exit 1; fi
    # Try key auth first; fall back to password 'bedrock' to install
    # pubkey on first contact.
    if ! ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o BatchMode=yes -o ConnectTimeout=5 "root@$ip" 'true' 2>/dev/null; then
        note "sim-$i: pubkey not in authorized_keys — pushing via password"
        sshpass -p bedrock ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o PreferredAuthentications=password -o PubkeyAuthentication=no \
            "root@$ip" "mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
                        echo '$(cat ~/.ssh/id_ed25519.pub)' >> /root/.ssh/authorized_keys && \
                        chmod 600 /root/.ssh/authorized_keys"
    fi
    if sssh $i 'test -f /var/lib/bedrock-install/.bootstrap-done && which bedrock >/dev/null' 2>/dev/null; then
        pass "sim-$i ready at $ip"
    else
        fail "sim-$i: bootstrap-done or bedrock CLI missing"
        exit 1
    fi
done

# ─────────────────────────────────────────────────────────────────
step "1. Sync current repo code to sims + restart bedrock-d"
"$TESTBED/sync-to-sims.sh" --restart 1 2 3 4

# ─────────────────────────────────────────────────────────────────
step "2. bedrock init on sim-1 (1-node cluster)"
WS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
note "workstation/witness IP: $WS_IP"

# `bedrock init` only takes --name; witness goes through `bedrock
# witness add` after the cluster is up + the echo stub is running.
INIT_OUT=$(sssh 1 'set -o pipefail; bedrock init --name test-failover 2>&1' \
    | tee /tmp/setup-init.log | tail -10)
echo "$INIT_OUT"
if echo "$INIT_OUT" | grep -qE "ERROR|FAIL|Traceback"; then
    fail "bedrock init failed (see /tmp/setup-init.log)"; exit 1
fi
pass "sim-1 init done; waiting 10s for services"
sleep 10

# ─────────────────────────────────────────────────────────────────
step "3. Start BedRock Echo stub on workstation with cluster.key from sim-1"
pkill -f "bedrock_echo_stub" 2>/dev/null
sleep 1
ECHO_LOG=/tmp/bedrock-echo-stub.log
> "$ECHO_LOG"

CLUSTER_KEY_HEX=$(sssh 1 'python3 -c "print(open(\"/etc/bedrock/cluster.key\",\"rb\").read().hex())" 2>/dev/null')
if [ -z "$CLUSTER_KEY_HEX" ]; then
    fail "couldn't read cluster.key from sim-1"; exit 1
fi
python3 "$TESTBED/bedrock_echo_stub.py" --cluster-key-hex "$CLUSTER_KEY_HEX" \
    --echo-id "testbed-echo" --verbose >"$ECHO_LOG" 2>&1 &
ECHO_PID=$!
sleep 3
if kill -0 $ECHO_PID 2>/dev/null; then
    pass "echo stub started with real cluster key (pid $ECHO_PID)"
else
    fail "echo stub died; see $ECHO_LOG"; exit 1
fi

# Register the workstation as a witness in rqlite. The pubkey field
# isn't validated today (v0.1 uses the shared cluster_key for AEAD);
# 64 zeros is a placeholder for the v0.2 per-witness-key model.
sssh 1 "bedrock witness add testbed-echo ${WS_IP}:12321 $(printf '0%.0s' {1..64}) 2>&1 | tail -5" \
    || note "witness add returned non-zero (may already exist; continuing)"

api_token() {
    sssh "$1" 'curl -sS -X POST http://127.0.0.1:8001/api/login \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"root\",\"password\":\"admin\"}"' \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null
}

approve_pending_join() {
    local master_ip=$1 token=$2
    local rid
    rid=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 root@${master_ip} \
        "curl -fsSL 'http://127.0.0.1:4001/db/query?level=strong' \
            -d '[\"SELECT request_id FROM join_requests WHERE state=\\\"pending\\\" ORDER BY created_at DESC LIMIT 1\"]' 2>&1 \
        | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")'")
    if [ -z "$rid" ]; then fail "no pending join"; return 1; fi
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 root@${master_ip} \
        "curl -sS -X POST http://127.0.0.1:8001/api/join/approve \
            -H 'Content-Type: application/json' \
            -H 'Authorization: Bearer ${token}' \
            -d '{\"request_id\":\"${rid}\"}'" >/dev/null
}

IP1=$(sim_ip 1)
TOKEN=$(api_token 1)
[ -n "$TOKEN" ] || { fail "no operator token from sim-1"; exit 1; }

for i in 2 3 4; do
    step "4.$((i-1)) Join sim-$i → N=$i"
    # bedrock join takes a positional node_ip (the IP of any current
    # cluster member); --yes auto-confirms the join prompt.
    sssh $i "nohup bedrock join $IP1 --yes >/tmp/join.log 2>&1 &" >/dev/null
    sleep 5
    approve_pending_join "$IP1" "$TOKEN" || { fail "approve sim-$i failed"; exit 1; }
    sleep 30
    NODES=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT COUNT(*) FROM nodes\"]" 2>/dev/null | python3 -c "import sys,json;r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else 0)"')
    if [ "$NODES" = "$i" ]; then
        pass "cluster size = $NODES after sim-$i join"
    else
        fail "expected N=$i, got nodes=$NODES"; exit 1
    fi
done

# ─────────────────────────────────────────────────────────────────
step "5. bedrock storage promote (cluster singleton tier → DRBD)"
sssh 1 'bedrock storage promote 2>&1 | tail -10' \
    || fail "storage promote returned non-zero (continuing)"
sleep 10
TIER_MODE=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mode FROM tiers WHERE tier_name=\\\"cluster\\\"\"]" 2>/dev/null | python3 -c "import sys,json;r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"')
if [ "$TIER_MODE" = "drbd" ]; then
    pass "cluster singleton tier mode = drbd; ready for failover testing"
else
    fail "cluster singleton tier mode = '$TIER_MODE' (expected 'drbd')"
fi

echo
echo "${C_G}━━ 4-node DRBD-promoted cluster ready ━━${C_0}"
echo "Run: testbed/test_pet_vm_failover.sh"
echo "Echo stub PID: $ECHO_PID  (log: $ECHO_LOG)"
