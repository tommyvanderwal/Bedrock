#!/usr/bin/env bash
# Offline-ISO E2E test: assumes sims booted from bedrock-install ISO
# and firstboot install.sh has finished. No network REPO required.
#
# Tests (each transition):
#   * cluster size (nodes table)
#   * rqlite Raft voters (incl. arbiter at N>=2 → N+1)
#   * reachable Raft voters (mesh routing works both directions)
#   * arbiter co-located with mgmt master
#   * S3 marker put/get round-trip (anonymous SeaweedFS)
#   * cattle VM running on exactly one node
#
# Lifecycle:
#   1→2→3→4 then 4→3→2→1 via bedrock node leave
#   Prior-phase S3 markers must survive every transition.

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

sssh() {
    local node=$1; shift
    local ip; ip=$(sim_ip "$node")
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 root@${ip} "$@"
}

api_token() {
    # Mgmt API HTTP is loopback-only; go through SSH on the master node.
    local node=$1
    sssh "$node" 'curl -sS -X POST http://127.0.0.1:8080/api/login \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"root\",\"password\":\"admin\"}"' \
        | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))' 2>/dev/null
}

approve_pending_join() {
    local master_ip=$1
    local token=$2
    local rid
    rid=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 root@${master_ip} \
        "curl -fsSL 'http://127.0.0.1:4001/db/query?level=strong' \
            -d '[\"SELECT request_id FROM join_requests WHERE state=\\\"pending\\\" ORDER BY created_at DESC LIMIT 1\"]' 2>&1 \
        | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")'")
    if [ -z "$rid" ]; then
        mark_fail "no pending join to approve"
        return 1
    fi
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 root@${master_ip} \
        "curl -sS -X POST http://127.0.0.1:8080/api/join/approve \
            -H 'Content-Type: application/json' \
            -H 'Authorization: Bearer ${token}' \
            -d '{\"request_id\":\"${rid}\"}'" >/dev/null
    return 0
}

assert_rqlite_voters() {
    local node=$1 expected=$2
    local got=""
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        got=$(sssh "$node" 'curl -fsSL http://127.0.0.1:4001/nodes 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for v in d.values() if v.get(\"voter\")))"')
        [ "$got" = "$expected" ] && break
        sleep 3
    done
    if [ "$got" = "$expected" ]; then
        pass "rqlite voters @ sim-$node = $got (== $expected, attempt $attempt)"
    else
        mark_fail "rqlite voters @ sim-$node = $got (expected $expected)"
    fi
}

assert_rqlite_reachable_voters() {
    local node=$1 expected=$2
    local got=""
    # Retry — convergence can take a few seconds after a join/leave.
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        got=$(sssh "$node" 'curl -fsSL http://127.0.0.1:4001/nodes 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for v in d.values() if v.get(\"voter\") and v.get(\"reachable\")))"')
        [ "$got" = "$expected" ] && break
        sleep 3
    done
    if [ "$got" = "$expected" ]; then
        pass "rqlite reachable voters @ sim-$node = $got (== $expected, attempt $attempt)"
    else
        mark_fail "rqlite reachable voters @ sim-$node = $got (expected $expected)"
    fi
}

assert_cluster_size() {
    local node=$1 expected=$2
    local got
    got=$(sssh "$node" 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT COUNT(*) FROM nodes\"]" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[\"results\"][0][\"values\"][0][0])"' 2>/dev/null)
    if [ "$got" = "$expected" ]; then
        pass "cluster size @ sim-$node = $got (== $expected)"
    else
        mark_fail "cluster size @ sim-$node = $got (expected $expected)"
    fi
}

assert_master_can_reach_peers() {
    local node=$1
    local lo_list
    lo_list=$(sssh "$node" 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT loopback_ip FROM nodes WHERE loopback_ip != \\\"\\\"\"]" 2>/dev/null | python3 -c "import sys,json; print(\" \".join(r[0] for r in json.load(sys.stdin)[\"results\"][0].get(\"values\",[])))"')
    local total=0 ok=0
    for lo in $lo_list; do
        total=$((total+1))
        if sssh "$node" "ping -c 1 -W 1 ${lo} >/dev/null 2>&1"; then
            ok=$((ok+1))
        fi
    done
    if [ "$ok" = "$total" ] && [ "$total" -gt 0 ]; then
        pass "mesh: sim-$node reaches all $ok/$total cluster loopbacks"
    else
        mark_fail "mesh: sim-$node reaches only $ok/$total cluster loopbacks ($lo_list)"
    fi
}

mgmt_master() {
    local node=$1
    sssh "$node" 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/cluster.json\")).get(\"mgmt_master\",\"\"))"'
}

assert_arbiter_active_on_master() {
    local probe_node=$1
    local master
    master=$(mgmt_master "$probe_node")
    [ -z "$master" ] && { mark_fail "no mgmt_master"; return; }
    # Find which sim_idx is master
    local master_idx=""
    for i in 1 2 3 4; do
        local n; n=$(sssh "$i" 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/state.json\")).get(\"node_name\",\"\"))"' 2>/dev/null)
        if [ "$n" = "$master" ]; then master_idx=$i; break; fi
    done
    [ -z "$master_idx" ] && { mark_fail "can't map master $master to sim_idx"; return; }
    # At N=1 there's no arbiter; filer runs locally
    local size; size=$(sssh "$probe_node" 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT COUNT(*) FROM nodes\"]" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[\"results\"][0][\"values\"][0][0])"' 2>/dev/null)
    if [ "$size" = "1" ]; then
        if sssh "$master_idx" "systemctl is-active --quiet bedrock-weed-filer"; then
            pass "N=1: filer active on master ($master, sim-$master_idx)"
        else
            mark_fail "N=1: filer not active on master $master"
        fi
        return
    fi
    # At N>=2: arbiter rqlite must be active on master
    if sssh "$master_idx" "systemctl is-active --quiet bedrock-rqlited-arbiter"; then
        pass "N=$size: arbiter rqlite active on master ($master, sim-$master_idx)"
    else
        mark_fail "N=$size: arbiter rqlite NOT active on master ($master)"
    fi
}

s3_endpoint_for() {
    local node=$1
    sssh "$node" 'python3 -c "import json; c=json.load(open(\"/etc/bedrock/cluster.json\")); n=c[\"nodes\"].get(c[\"mgmt_master\"],{}); print(n.get(\"host\",\"\"))"'
}

s3_put_marker() {
    local node=$1 phase=$2
    local master_ip
    master_ip=$(s3_endpoint_for "$node")
    [ -z "$master_ip" ] && { mark_fail "s3_put_marker: no master host"; return 1; }
    # Idempotent bucket create (200 or already-exists)
    sssh "$node" "curl -sS -X PUT http://${master_ip}:8333/bedrock-test/ >/dev/null"
    local body="phase=$phase ts=$(date -Iseconds)"
    local code
    code=$(sssh "$node" "curl -sS -X PUT --data-raw '$body' \
        http://${master_ip}:8333/bedrock-test/marker-${phase}.txt \
        -o /dev/null -w '%{http_code}'")
    if [ "$code" = "200" ] || [ "$code" = "204" ]; then
        return 0
    else
        return 1
    fi
}

s3_get_marker() {
    local node=$1 phase=$2
    local master_ip
    master_ip=$(s3_endpoint_for "$node")
    [ -z "$master_ip" ] && return 1
    sssh "$node" "curl -sS --fail \
        http://${master_ip}:8333/bedrock-test/marker-${phase}.txt 2>/dev/null"
}

s3_round_trip() {
    local probe_node=$1 phase=$2
    if s3_put_marker "$probe_node" "$phase"; then
        local got
        got=$(s3_get_marker "$probe_node" "$phase" || echo "")
        if echo "$got" | grep -q "phase=$phase"; then
            pass "S3 marker @ $phase round-trip OK"
        else
            mark_fail "S3 marker @ $phase put OK but get failed (got: $got)"
        fi
    else
        mark_fail "S3 marker @ $phase put failed"
    fi
}

s3_verify_history() {
    local probe_node=$1; shift
    note "verifying historical S3 markers @ sim-$probe_node"
    for ph in "$@"; do
        local got
        got=$(s3_get_marker "$probe_node" "$ph" 2>/dev/null || echo "")
        if echo "$got" | grep -q "phase=$ph"; then
            pass "  marker $ph survived"
        else
            mark_fail "  marker $ph LOST"
        fi
    done
}

vm_running_count() {
    local vm=$1 n=$2
    local running=0 hosts=""
    for i in $(seq 1 "$n"); do
        if sssh "$i" "virsh list --state-running --name 2>/dev/null | grep -qx '$vm'"; then
            running=$((running+1))
            hosts="$hosts sim-$i"
        fi
    done
    echo "$running $hosts"
}

assert_vm_on_one_node() {
    local vm=$1 n=$2
    local result; result=$(vm_running_count "$vm" "$n")
    local running="${result%% *}"
    local hosts="${result#* }"
    if [ "$running" = "1" ]; then
        pass "VM $vm running on$hosts (exactly 1 host)"
    else
        mark_fail "VM $vm running on $running nodes ($hosts) — expected 1"
    fi
}

# ──────────────────────────────────────────────
step "0. Confirm all 4 sims have firstboot-done"
for i in 1 2 3 4; do
    ip=$(sim_ip $i)
    if [ -z "$ip" ]; then
        fail "sim-$i: no IP"; exit 1
    fi
    if sssh $i 'test -f /var/lib/bedrock-install/.bootstrap-done && which bedrock' >/dev/null 2>&1; then
        pass "sim-$i ready at $ip"
    else
        fail "sim-$i not ready (.bootstrap-done or bedrock CLI missing)"
        exit 1
    fi
done

step "1. bedrock init on sim-1 → N=1"
sssh 1 'bedrock init --name test-fresh 2>&1 | tail -15'
sleep 5
assert_cluster_size 1 1
assert_rqlite_voters 1 1
assert_arbiter_active_on_master 1
s3_round_trip 1 "n1"

step "2. Scale-up sim-2 → N=2"
IP1=$(sim_ip 1)
TOKEN=$(api_token 1)
[ -n "$TOKEN" ] || { fail "no operator token"; exit 1; }
sssh 2 "nohup bedrock join --witness $IP1 --yes >/tmp/join.log 2>&1 &" >/dev/null
sleep 5
approve_pending_join "$IP1" "$TOKEN"
sleep 30
assert_cluster_size 1 2
assert_rqlite_voters 1 3   # 2 nodes + arbiter
assert_rqlite_reachable_voters 1 3
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 2 n1
s3_round_trip 2 "n2"

step "3. Scale-up sim-3 → N=3"
sssh 3 "nohup bedrock join --witness $IP1 --yes >/tmp/join.log 2>&1 &" >/dev/null
sleep 5
approve_pending_join "$IP1" "$TOKEN"
sleep 30
assert_cluster_size 1 3
assert_rqlite_voters 1 4   # 3 nodes + arbiter
assert_rqlite_reachable_voters 1 4
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 3 n1 n2
s3_round_trip 3 "n3"

step "4. Scale-up sim-4 → N=4"
sssh 4 "nohup bedrock join --witness $IP1 --yes >/tmp/join.log 2>&1 &" >/dev/null
sleep 5
approve_pending_join "$IP1" "$TOKEN"
sleep 30
assert_cluster_size 1 4
assert_rqlite_voters 1 5   # 4 nodes + arbiter
assert_rqlite_reachable_voters 1 5
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 4 n1 n2 n3
s3_round_trip 4 "n4"

step "5. Mesh reachability all-pairs at N=4"
for i in 1 2 3 4; do
    assert_master_can_reach_peers $i
done

step "6. Cattle VM lifecycle"
sssh 1 'bedrock vm create cattle-test --type cattle --ram 256 --disk 1 2>&1 | tail -3' || note "vm create returned non-zero"
sleep 30
assert_vm_on_one_node "cattle-test" 4

sim_node_name() {
    sssh "$1" 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/state.json\")).get(\"node_name\",\"\"))"'
}

step "7. Scale-down: sim-4 leaves → N=3"
NN=$(sim_node_name 4)
sssh 1 "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 3
assert_rqlite_voters 1 4
assert_rqlite_reachable_voters 1 4
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 1 n1 n2 n3 n4

step "8. Scale-down: sim-3 leaves → N=2"
NN=$(sim_node_name 3)
sssh 1 "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 2
assert_rqlite_voters 1 3
assert_rqlite_reachable_voters 1 3
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 1 n1 n2 n3 n4

step "9. Scale-down: sim-2 leaves → N=1"
NN=$(sim_node_name 2)
sssh 1 "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 1
assert_rqlite_voters 1 1
assert_arbiter_active_on_master 1
s3_verify_history 1 n1 n2 n3 n4

step "RESULT"
if [ "$ALL_PASS" = "1" ]; then
    echo "${C_G}=== ALL CHECKS PASSED — full lifecycle E2E green ===${C_0}"
    exit 0
else
    echo "${C_R}=== SOME CHECKS FAILED — see FAIL lines above ===${C_0}"
    exit 1
fi
