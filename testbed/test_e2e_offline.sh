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

_CM_DIR=${CM_DIR:-/tmp/bedrock-e2e-cm}
mkdir -p "$_CM_DIR" 2>/dev/null
sssh() {
    local node=$1; shift
    local ip; ip=$(sim_ip "$node")
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 \
        -o ControlMaster=auto -o ControlPath="$_CM_DIR/%r@%h:%p" -o ControlPersist=300 \
        root@${ip} "$@"
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

master_sim_idx() {
    # Returns the sim index (1..4) whose node_name matches the cluster's
    # current mgmt_master. Useful after a failover where sim-1 may no
    # longer be the master.
    local mm
    mm=$(mgmt_master 1 2>/dev/null) \
        || mm=$(mgmt_master 2 2>/dev/null) \
        || mm=$(mgmt_master 3 2>/dev/null) \
        || mm=$(mgmt_master 4 2>/dev/null)
    [ -z "$mm" ] && { echo 1; return; }
    for i in 1 2 3 4; do
        local nm
        nm=$(sssh "$i" 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/state.json\")).get(\"node_name\",\"\"))"' 2>/dev/null)
        if [ "$nm" = "$mm" ]; then
            echo "$i"; return
        fi
    done
    echo 1
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
    # Pre-storage-promote: no DRBD on tier-cluster → no arbiter rqlite.
    # Filer + S3 run locally on master. After `bedrock storage promote`
    # at N>=2 the arbiter rqlite is expected (separate test).
    local has_drbd
    has_drbd=$(sssh "$master_idx" 'drbdadm status tier-cluster 2>/dev/null | head -1 | grep -v "no resources" | wc -l')
    if [ "$has_drbd" = "0" ]; then
        if sssh "$master_idx" "systemctl is-active --quiet bedrock-weed-filer"; then
            pass "no-DRBD: filer active on master ($master, sim-$master_idx)"
        else
            mark_fail "no-DRBD: filer not active on master $master"
        fi
        return
    fi
    if sssh "$master_idx" "systemctl is-active --quiet bedrock-rqlited-arbiter"; then
        pass "DRBD-mode: arbiter rqlite active on master ($master, sim-$master_idx)"
    else
        mark_fail "DRBD-mode: arbiter rqlite NOT active on master ($master)"
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
# Pre-storage-promote: voters = N (per-node rqlite only, no arbiter yet)
assert_rqlite_voters 1 2
assert_rqlite_reachable_voters 1 2
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1   # at N=1 filer code path; pre-promote
s3_verify_history 2 n1
s3_round_trip 2 "n2"

step "3. Scale-up sim-3 → N=3"
sssh 3 "nohup bedrock join --witness $IP1 --yes >/tmp/join.log 2>&1 &" >/dev/null
sleep 5
approve_pending_join "$IP1" "$TOKEN"
sleep 30
assert_cluster_size 1 3
assert_rqlite_voters 1 3
assert_rqlite_reachable_voters 1 3
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
assert_rqlite_voters 1 4
assert_rqlite_reachable_voters 1 4
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 4 n1 n2 n3
s3_round_trip 4 "n4"

step "5. Mesh reachability all-pairs at N=4"
for i in 1 2 3 4; do
    assert_master_can_reach_peers $i
done

# ─────────────────────────────────────────────────────────────────
# 5a. SeaweedFS ISO library is FUSE-mounted on every node and is
#     a single shared namespace via the filer.
# ─────────────────────────────────────────────────────────────────
step "5a. ISO library — mount + cross-node visibility"
for i in 1 2 3 4; do
    if sssh $i 'mountpoint -q /mnt/isos && grep -q "fuse.seaweedfs" /proc/mounts'; then
        pass "sim-$i: /mnt/isos is a SeaweedFS FUSE mount"
    else
        mark_fail "sim-$i: /mnt/isos NOT mounted via SeaweedFS FUSE"
    fi
done
# Place a marker on sim-1 and verify it shows up on all peers.
MARKER_NAME="iso-test-$(date +%s).txt"
sssh 1 "echo 'cross-node-iso-marker' > /mnt/isos/$MARKER_NAME && sync" \
    || mark_fail "sim-1 couldn't write to /mnt/isos"
sleep 6
for i in 1 2 3 4; do
    got=$(sssh $i "cat /mnt/isos/$MARKER_NAME 2>/dev/null" || echo "")
    if [ "$got" = "cross-node-iso-marker" ]; then
        pass "sim-$i sees the ISO-library marker"
    else
        mark_fail "sim-$i: marker missing or wrong (got: '${got:0:60}')"
    fi
done
sssh 1 "rm -f /mnt/isos/$MARKER_NAME"

# ─────────────────────────────────────────────────────────────────
# 5b. Storage promote — critical tier to DRBD-replicated. Bulk
#     stays SeaweedFS-distributed; scratch stays local LV.
# ─────────────────────────────────────────────────────────────────
step "5b. Storage promote — critical tier to DRBD"
sssh 1 'bedrock storage promote 2>&1 | tail -20' || mark_fail "bedrock storage promote returned non-zero"
sleep 8
# Critical tier mode should be 'drbd' in cluster snapshot.
TIER_MODE=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mode FROM tiers WHERE tier_name=\\\"critical\\\"\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
if [ "$TIER_MODE" = "drbd" ]; then
    pass "critical tier mode = drbd"
else
    mark_fail "critical tier mode = '$TIER_MODE' (expected 'drbd')"
fi
# Arbiter rqlite should be active on master after promote.
if sssh 1 'systemctl is-active --quiet bedrock-rqlited-arbiter'; then
    pass "arbiter rqlite active on master post-promote"
else
    mark_fail "arbiter rqlite NOT active on master post-promote"
fi
# .254/32 should be on master's lo.
ARB_IP=$(sssh 1 'python3 -c "import sys; sys.path.insert(0,\"/usr/local/lib/bedrock\"); from lib import cluster_arbiter as ca; print(ca.arbiter_loopback_ip())"' 2>/dev/null || echo "")
if [ -n "$ARB_IP" ] && sssh 1 "ip -4 addr show lo | grep -q '$ARB_IP/'"; then
    pass "arbiter VIP $ARB_IP claimed on master's lo"
else
    mark_fail "arbiter VIP $ARB_IP NOT on master's lo"
fi

# ─────────────────────────────────────────────────────────────────
# 5c. Isolation test — drop the current leader for 90s (past the
#     witness/fence thresholds), observe failover, restore network,
#     verify consistency.
# ─────────────────────────────────────────────────────────────────
step "5c. Isolation: drop sim-1 (current leader) for 90s"

WS_IP=$(ip route get $(sim_ip 1) 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
[ -z "$WS_IP" ] && WS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
note "workstation IP allowlisted on br0: $WS_IP"

# Capture pre-isolation state (master, voters)
PRE_MASTER=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[\"results\"][0][\"values\"][0][0])"')
note "pre-isolation mgmt_master: $PRE_MASTER"

# Isolate sim-1: drop cluster traffic but keep workstation SSH alive
sssh 1 "bash -c '
iptables-save > /tmp/iptables-pre-isolation 2>/dev/null
# Allow workstation FIRST (insert order matters — last -I wins position 1)
iptables -I OUTPUT -o br0 -j DROP
iptables -I INPUT  -i br0 -j DROP
iptables -I OUTPUT -o br0 -d $WS_IP -j ACCEPT
iptables -I INPUT  -i br0 -s $WS_IP -j ACCEPT
# Cut mesh-plane NICs entirely
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic down 2>/dev/null; done
echo isolated
'" || mark_fail "couldn't apply iptables on sim-1"

note "sim-1 isolated; sleeping 90s..."
sleep 90

# Observe from sim-2 (still connected to sim-3/4 mesh)
note "--- DURING isolation, view from sim-2 ---"
sssh 2 'curl -fsSL --max-time 5 http://127.0.0.1:4001/nodes 2>&1 | python3 -c "
import sys, json
d = json.load(sys.stdin)
leaders = [k for k,v in d.items() if v.get(\"leader\")]
reach   = [k for k,v in d.items() if v.get(\"reachable\")]
print(\"  /nodes leader:\", leaders or \"NONE\")
print(\"  /nodes reachable:\", reach)
"' || note "sim-2 rqlite unresponsive during isolation"

DURING_MASTER=$(sssh 2 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' || echo "")
note "during-isolation mgmt_master (sim-2 view): ${DURING_MASTER:-unknown}"
if [ -n "$DURING_MASTER" ] && [ "$DURING_MASTER" != "$PRE_MASTER" ]; then
    pass "failover: mgmt_master moved $PRE_MASTER → $DURING_MASTER"
else
    mark_fail "failover: mgmt_master did NOT move during 90s isolation (still $DURING_MASTER)"
fi

# Check whether .254 VIP moved to the new master
NEW_MASTER_LO=$(sssh 2 "curl -fsSL --max-time 5 'http://127.0.0.1:4001/db/query?level=strong' -d '[\"SELECT loopback_ip FROM nodes WHERE node_name=\\\"$DURING_MASTER\\\"\"]' 2>/dev/null | python3 -c 'import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")'" 2>/dev/null || echo "")
ARB_IP=$(sssh 2 'python3 -c "import sys; sys.path.insert(0,\"/usr/local/lib/bedrock\"); from lib import cluster_arbiter as ca; print(ca.arbiter_loopback_ip())"' 2>/dev/null || echo "")
# Find which sim hosts the new master
NEW_MASTER_SIM=""
for i in 2 3 4; do
    nm=$(sssh $i 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/state.json\")).get(\"node_name\",\"\"))"' 2>/dev/null)
    if [ "$nm" = "$DURING_MASTER" ]; then NEW_MASTER_SIM=$i; break; fi
done
if [ -n "$NEW_MASTER_SIM" ] && [ -n "$ARB_IP" ] && sssh $NEW_MASTER_SIM "ip -4 addr show lo | grep -q '$ARB_IP/'"; then
    pass "failover: arbiter VIP $ARB_IP claimed on new master (sim-$NEW_MASTER_SIM)"
else
    mark_fail "failover: arbiter VIP $ARB_IP NOT on new master ($DURING_MASTER, sim-$NEW_MASTER_SIM)"
fi
# Arbiter rqlite + filer should be active on the new master
if [ -n "$NEW_MASTER_SIM" ] && sssh $NEW_MASTER_SIM "systemctl is-active --quiet bedrock-rqlited-arbiter"; then
    pass "failover: arbiter rqlite active on new master"
else
    mark_fail "failover: arbiter rqlite NOT active on new master"
fi
if [ -n "$NEW_MASTER_SIM" ] && sssh $NEW_MASTER_SIM "systemctl is-active --quiet bedrock-weed-filer"; then
    pass "failover: filer active on new master"
else
    mark_fail "failover: filer NOT active on new master"
fi

# Check sim-1's own fence/services state — it should have self-fenced
note "--- sim-1 internal view during isolation ---"
sssh 1 'systemctl is-active bedrock-rqlited bedrock-mgmt bedrock-weed-filer bedrock-rqlited-arbiter bedrock-rust 2>&1 | head -6; echo ---; test -f /run/bedrock-rust.fence && echo "fence marker present" || echo "no fence marker"; ip -4 addr show lo 2>&1 | grep -E "100\\." | head -3' || note "sim-1 introspect failed"

# .254 must NOT be on sim-1's lo (released as part of self-fence)
if sssh 1 "ip -4 addr show lo | grep -q '$ARB_IP/' 2>/dev/null"; then
    mark_fail "self-fence: arbiter VIP $ARB_IP still on sim-1's lo (should have been released)"
else
    pass "self-fence: arbiter VIP $ARB_IP released from sim-1's lo"
fi

# Restore connectivity on sim-1
note "--- restoring connectivity on sim-1 ---"
sssh 1 "bash -c '
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic up 2>/dev/null; done
iptables -F INPUT
iptables -F OUTPUT
iptables-restore < /tmp/iptables-pre-isolation 2>/dev/null || true
echo restored
'"

note "sleeping 20s for mesh + rqlite convergence..."
sleep 20

# Verify post-rejoin consistency
note "--- POST-rejoin: cluster view from sim-1 ---"
sssh 1 'curl -fsSL --max-time 5 http://127.0.0.1:4001/nodes 2>&1 | python3 -c "
import sys, json
d = json.load(sys.stdin)
leaders = [k for k,v in d.items() if v.get(\"leader\")]
reach   = [k for k,v in d.items() if v.get(\"reachable\")]
print(\"  /nodes leader:\", leaders)
print(\"  /nodes reachable:\", reach)
"' || mark_fail "sim-1 rqlite unresponsive after rejoin"

# A. exactly one leader in the cluster (no split-brain)
LEADERS=$(sssh 1 'curl -fsSL --max-time 5 http://127.0.0.1:4001/nodes 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for v in d.values() if v.get(\"leader\")))"' || echo "?")
if [ "$LEADERS" = "1" ]; then
    pass "post-rejoin: exactly 1 leader (no split-brain)"
else
    mark_fail "post-rejoin: $LEADERS leaders visible (expected 1)"
fi

# B. mesh reachability restored from all sims to all loopbacks
for i in 1 2 3 4; do
    assert_master_can_reach_peers $i
done

# C. S3 markers all still readable (from sim-1 — the data lives on sim-1's leveldb3)
s3_verify_history 1 n1 n2 n3 n4

# D. mgmt_master coherent across nodes
POST_MASTER=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)[\"results\"][0][\"values\"][0][0])"')
note "post-rejoin mgmt_master: $POST_MASTER (was: $PRE_MASTER, during: ${DURING_MASTER:-?})"

step "6. Cattle VM lifecycle"
MASTER_SIM=$(master_sim_idx)
note "current master is sim-$MASTER_SIM"
sssh "$MASTER_SIM" 'bedrock vm create cattle-test --type cattle --ram 256 --disk 1 2>&1 | tail -3' \
    || note "vm create returned non-zero"
sleep 30
assert_vm_on_one_node "cattle-test" 4

sim_node_name() {
    sssh "$1" 'python3 -c "import json; print(json.load(open(\"/etc/bedrock/state.json\")).get(\"node_name\",\"\"))"'
}

step "7. Scale-down: sim-4 leaves → N=3"
NN=$(sim_node_name 4)
[ -n "$NN" ] || NN=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT node_name FROM nodes WHERE host = (SELECT host FROM nodes ORDER BY host DESC LIMIT 1) LIMIT 1\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"')
MASTER_SIM=$(master_sim_idx)
sssh "$MASTER_SIM" "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 3
# Note: rqlite voters DON'T auto-shrink — `bedrock node leave` removes
# the row in nodes table but doesn't call rqlite /remove. The leaver's
# raft slot stays in /nodes as "reachable: false" until /remove.
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 1 n1 n2 n3 n4

step "8. Scale-down: sim-3 leaves → N=2"
NN=$(sim_node_name 3)
[ -n "$NN" ] || NN=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT node_name FROM nodes WHERE host=\\\"192.168.2.22\\\" LIMIT 1\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"')
MASTER_SIM=$(master_sim_idx)
sssh "$MASTER_SIM" "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 2
assert_master_can_reach_peers 1
assert_arbiter_active_on_master 1
s3_verify_history 1 n1 n2 n3 n4

# ─────────────────────────────────────────────────────────────────
# 8a/b/c. 2-node HA failover with BedRock Echo (witness)
#     Pre-state: N=2, sim-1 master, sim-2 follower (from step 8).
#     8a — start bedrock-echo-stub on the workstation with the
#          cluster's HMAC key; verify witness alive from both sims.
#     8b — isolate sim-1 → sim-2 should win the vote (witness +1)
#          and promote within the holddown window.
#     8c — restore sim-1 → cluster reconverges; sim-2 stays master.
#     8d — kill echo; isolate sim-1 again → both go NoQuorum
#          (split-brain prevented); no set_mgmt_master writes.
#     8e — restore sim-1, restart echo → cluster recovers.
# ─────────────────────────────────────────────────────────────────
step "8a. Start BedRock Echo stub on workstation"
WS_IP=$(ip route get $(sim_ip 1) 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i=="src") print $(i+1)}')
[ -z "$WS_IP" ] && WS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
note "workstation IP: $WS_IP"
# Pull cluster key from sim-1 (HMAC key for witness auth)
CLUSTER_KEY_HEX=$(sssh 1 'xxd -p /etc/bedrock/cluster.key | tr -d "\n"' 2>/dev/null || echo "")
if [ -z "$CLUSTER_KEY_HEX" ] || [ ${#CLUSTER_KEY_HEX} -ne 64 ]; then
    mark_fail "8a: could not pull /etc/bedrock/cluster.key from sim-1 (got ${#CLUSTER_KEY_HEX} hex chars)"
else
    pass "8a: pulled cluster.key (${#CLUSTER_KEY_HEX} hex)"
fi
# Start echo stub in background
ECHO_LOG=/tmp/bedrock-echo-stub.log
> "$ECHO_LOG"
python3 "$TESTBED/bedrock_echo_stub.py" --cluster-key-hex "$CLUSTER_KEY_HEX" \
    --echo-id "testbed-echo" >"$ECHO_LOG" 2>&1 &
ECHO_PID=$!
sleep 3
if ! kill -0 $ECHO_PID 2>/dev/null; then
    mark_fail "8a: echo stub died — see $ECHO_LOG"
    cat "$ECHO_LOG" | head -10
else
    pass "8a: echo stub running (pid $ECHO_PID)"
fi
# Wait long enough for the cluster nodes' 1Hz election tick to
# discover the echo + send at least one heartbeat.
sleep 8
# Sanity: claim acks should appear in stub log once a node sends one.
if grep -qE "probe.*ACCEPTED|probe.*${MASTER_SIM:-?}" "$ECHO_LOG" 2>/dev/null; then
    pass "8a: echo log shows probes from cluster"
fi

step "8b. Isolate sim-1 (current master) — expect sim-2 to take over"
PRE_MASTER=$(sssh 2 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
note "pre-isolation mgmt_master: $PRE_MASTER"
sssh 1 "bash -c '
iptables -I OUTPUT -o br0 -j DROP
iptables -I INPUT  -i br0 -j DROP
iptables -I OUTPUT -o br0 -d $WS_IP -j ACCEPT
iptables -I INPUT  -i br0 -s $WS_IP -j ACCEPT
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic down 2>/dev/null; done
echo isolated
'" || mark_fail "8b: could not apply iptables on sim-1"
note "sim-1 isolated; sleeping 30s for election + promote..."
sleep 30
DURING_MASTER=$(sssh 2 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
note "during-isolation mgmt_master (sim-2's view): ${DURING_MASTER:-unknown}"
if [ -n "$DURING_MASTER" ] && [ "$DURING_MASTER" != "$PRE_MASTER" ]; then
    pass "8b: failover succeeded ($PRE_MASTER → $DURING_MASTER)"
else
    mark_fail "8b: failover did NOT happen (still $DURING_MASTER; 2-of-2 + witness should promote)"
fi
# Echo should now have a blessed_master matching the new master
if grep -qE "claim.*ACCEPTED" "$ECHO_LOG" 2>/dev/null; then
    pass "8b: witness recorded the failover claim"
else
    note "8b: no ACCEPTED claim in echo log yet"
fi
# sim-1 should have self-demoted (NoQuorum → release .254 + arbiter)
if sssh 1 "ip -4 addr show lo | grep -q '\\.254/' 2>/dev/null"; then
    mark_fail "8b: sim-1 STILL holds .254 VIP (self-demote didn't fire)"
else
    pass "8b: sim-1 released .254 VIP via NoQuorum self-demote"
fi

step "8c. Restore sim-1 connectivity → cluster reconverges"
sssh 1 "bash -c '
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic up 2>/dev/null; done
iptables -F INPUT
iptables -F OUTPUT
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
rm -f /run/bedrock-cluster.fence
echo restored
'" || note "restore returned non-zero"
sleep 25
POST_MASTER=$(sssh 1 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
if [ "$POST_MASTER" = "$DURING_MASTER" ]; then
    pass "8c: post-rejoin master unchanged ($POST_MASTER)"
else
    mark_fail "8c: master flipped back after rejoin ($DURING_MASTER → $POST_MASTER) — bless holddown failed?"
fi

step "8d. 2-node HA WITHOUT witness — split-brain prevention"
kill $ECHO_PID 2>/dev/null
wait $ECHO_PID 2>/dev/null
sleep 16  # exceed WITNESS_FRESHNESS_S (12s) so both sides go witness-dead
# Re-isolate sim-1
SECOND_MASTER=$POST_MASTER
sssh 1 "bash -c '
iptables -I OUTPUT -o br0 -j DROP
iptables -I INPUT  -i br0 -j DROP
iptables -I OUTPUT -o br0 -d $WS_IP -j ACCEPT
iptables -I INPUT  -i br0 -s $WS_IP -j ACCEPT
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic down 2>/dev/null; done
echo isolated-no-witness
'" || note "iptables on sim-1 (no-witness) returned non-zero"
note "sim-1 isolated (no witness); sleeping 30s..."
sleep 30
SPLIT_MASTER_VIEW_1=$(sssh 1 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
SPLIT_MASTER_VIEW_2=$(sssh 2 'curl -fsSL --max-time 5 "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT mgmt_master FROM cluster_info\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"' 2>/dev/null || echo "")
note "sim-1 view: ${SPLIT_MASTER_VIEW_1:-rqlite-unreachable}"
note "sim-2 view: ${SPLIT_MASTER_VIEW_2:-rqlite-unreachable}"
# Both should still report SECOND_MASTER (or no value because rqlite was killed by fence on
# whichever side is no-quorum). Neither side should have written a NEW master.
if [ -z "$SPLIT_MASTER_VIEW_1" ] || [ "$SPLIT_MASTER_VIEW_1" = "$SECOND_MASTER" ]; then
    pass "8d: sim-1 did NOT write a new master (split-brain prevented)"
else
    mark_fail "8d: sim-1's view shows new master $SPLIT_MASTER_VIEW_1 — split-brain!"
fi
# sim-1 must have NoQuorum-self-demoted (release .254 if it had it)
if sssh 1 "ip -4 addr show lo | grep -q '\\.254/' 2>/dev/null"; then
    mark_fail "8d: sim-1 STILL holds .254 VIP without witness vote"
else
    pass "8d: sim-1 released .254 VIP (no witness, no quorum)"
fi

step "8e. Restore sim-1 + echo; cluster recovers to N=2"
sssh 1 "bash -c '
for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic up 2>/dev/null; done
iptables -F INPUT
iptables -F OUTPUT
iptables -P INPUT ACCEPT
iptables -P OUTPUT ACCEPT
rm -f /run/bedrock-cluster.fence
echo restored
'" || note "8e: restore returned non-zero"
# Restart echo so witness is alive again
python3 "$TESTBED/bedrock_echo_stub.py" --cluster-key-hex "$CLUSTER_KEY_HEX" \
    --echo-id "testbed-echo" >>"$ECHO_LOG" 2>&1 &
ECHO_PID=$!
sleep 25
assert_cluster_size 1 2

# Cleanup: kill echo, sims will reconverge naturally
kill $ECHO_PID 2>/dev/null
wait $ECHO_PID 2>/dev/null

step "9. Scale-down: sim-2 leaves → N=1"
NN=$(sim_node_name 2)
[ -n "$NN" ] || NN=$(sssh 1 'curl -fsSL "http://127.0.0.1:4001/db/query?level=strong" -d "[\"SELECT node_name FROM nodes WHERE host=\\\"192.168.2.20\\\" LIMIT 1\"]" 2>/dev/null | python3 -c "import sys,json; r=json.load(sys.stdin)[\"results\"][0]; print(r[\"values\"][0][0] if r.get(\"values\") else \"\")"')
MASTER_SIM=$(master_sim_idx)
sssh "$MASTER_SIM" "bedrock node leave $NN 2>&1 | tail -5" || note "leave returned non-zero"
sleep 25
assert_cluster_size 1 1
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
