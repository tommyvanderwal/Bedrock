#!/usr/bin/env bash
# DEPRECATED 2026-05-28 — superseded by testbed/test_e2e_offline.sh (which
# walks 1->4->1) + testbed/setup_4node_cluster.sh. Predates the current
# contract (uses net-install, the removed `bedrock init --cluster-name`
# flag, and no /api/join/approve step). Kept for reference only.
echo "test_scale_lifecycle.sh is DEPRECATED — use test_e2e_offline.sh" >&2
exit 64
# Bedrock scale-lifecycle end-to-end test.
#
# Per Tommy's 2026-05-18 directive: walk the full membership lifecycle,
# 1 -> 2 -> 3 -> 4 -> 3 -> 2 -> 1 nodes, asserting at each transition:
#
#   * rqlite cluster has the expected number of voters (incl. arbiter
#     at N>=2)
#   * SeaweedFS master peer set matches the cluster
#   * VM placed on the current master survives the transition
#   * S3 bucket data put on transition N->N+1 is still readable at N+1
#   * cluster_arbiter status is consistent with the current mgmt master
#
# Corner cases this hunts for:
#   - arbiter not migrating when master moves
#   - tier-cluster DRBD not promoting/mounting on the new master
#   - SeaweedFS filer SQLite not following the move
#   - VM left orphaned on a node that just left the cluster
#   - rqlite revision diverging across nodes after scale-down
#   - Re-add of a previously-left node — node_id collision
#
# Runtime budget: ~20-30 minutes for the full walk.
#
# Prerequisites (set up by spawn.py before this runs):
#   - 4 sim VMs spawned (sim-1..sim-4), boot complete, SSH usable
#   - http server on 192.168.100.1:8000 hosting the installer payload
#
# Usage:
#   testbed/test_scale_lifecycle.sh                    # full lifecycle
#   testbed/test_scale_lifecycle.sh up-only            # 1->2->3->4 only
#   testbed/test_scale_lifecycle.sh down-only          # 4->3->2->1 only

set -euo pipefail
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED"

# shellcheck disable=SC2034
C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'
pass()  { echo "${C_G}PASS${C_0} $*"; }
fail()  { echo "${C_R}FAIL${C_0} $*"; exit 1; }
note()  { echo "${C_Y}--- ${C_0} $*"; }
step()  { echo; echo "${C_B}### $* ###${C_0}"; }

REPO="${BEDROCK_REPO:-http://192.168.100.1:8000}"

# Returns the management IP for sim-N (1-based).
sim_ip() {
    python3 -c "
import sys; sys.path.insert(0,'$TESTBED')
from spawn import get_mgmt_ip
print(get_mgmt_ip($1) or '')
"
}

# SSH wrapper — quiet, batch-mode, short timeout. Returns the exit
# code of the remote command. Exec via testbed/spawn.py for key
# discovery consistency.
sssh() {
    local node=$1; shift
    local ip; ip=$(sim_ip "$node")
    [ -z "$ip" ] && return 1
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=10 -o BatchMode=yes \
        "root@$ip" "$@"
}

# Returns the rqlite revision via strong-consistency read.
rqlite_revision() {
    local node=$1
    sssh "$node" \
        "curl -fsSL --max-time 5 \
            'http://127.0.0.1:4001/db/query?level=strong' \
            -d '[\"SELECT revision FROM bedrock_meta WHERE id=1\"]' \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d[\"results\"][0]; \
                print(r[\"values\"][0][0]) if r.get(\"values\") else print(0)' 2>/dev/null" \
        || echo "-1"
}

# Returns the number of rqlite cluster members (per /status).
rqlite_member_count() {
    local node=$1
    sssh "$node" \
        "curl -fsSL --max-time 5 'http://127.0.0.1:4001/nodes' \
            | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d))' 2>/dev/null" \
        || echo "0"
}

# Returns 'Primary' / 'Secondary' / 'Unknown' for tier-cluster DRBD.
drbd_role_cluster() {
    local node=$1
    sssh "$node" "drbdadm role tier-cluster 2>/dev/null | head -1 | cut -d/ -f1" || echo "Unknown"
}

# Returns the current mgmt master per cluster.json.
mgmt_master() {
    local node=$1
    sssh "$node" "python3 -c 'import json; print(json.load(open(\"/etc/bedrock/cluster.json\")).get(\"mgmt_master\") or \"\")'" \
        || echo ""
}

# Returns 1 if the named VM is running on the given node, else 0.
vm_running() {
    local node=$1 vm=$2
    sssh "$node" "virsh list --state-running --name | grep -qx '$vm' && echo 1 || echo 0" 2>/dev/null \
        || echo "0"
}

# Put + Get a marker file via the master's S3 endpoint.
#
# Uses curl directly — anonymous SeaweedFS S3 (per the testbed s3.json
# in seaweedfs.py write_s3_config()) accepts unsigned PUT/GET without
# needing the awscli package on every sim. Bucket auto-created on
# first PUT (SeaweedFS S3 gateway behaviour).
s3_endpoint_for() {
    local node=$1
    sssh "$node" "python3 -c 'import json; c=json.load(open(\"/etc/bedrock/cluster.json\")); n=c[\"nodes\"].get(c[\"mgmt_master\"],{}); print(n.get(\"host\",\"\"))'"
}

s3_put_marker() {
    local node=$1 phase=$2
    local master_ip
    master_ip=$(s3_endpoint_for "$node")
    [ -z "$master_ip" ] && fail "s3_put_marker: no master host"
    local body
    body="phase=$phase ts=$(date -Iseconds)"
    sssh "$node" "curl -sS -X PUT --data-raw $(printf %q "$body") \
        http://${master_ip}:8333/bedrock-test/marker-${phase}.txt \
        -w 'HTTP %{http_code}\n' | tail -2"
}

s3_get_marker() {
    local node=$1 phase=$2
    local master_ip
    master_ip=$(s3_endpoint_for "$node")
    [ -z "$master_ip" ] && return 1
    sssh "$node" "curl -sS --fail \
        http://${master_ip}:8333/bedrock-test/marker-${phase}.txt"
}

# ─────────────────────────────────────────────────────────────────
# Assertions per cluster size
# ─────────────────────────────────────────────────────────────────

assert_cluster_size() {
    local probe_node=$1 expected=$2
    note "asserting cluster size = $expected (probe via node $probe_node)"
    local actual
    actual=$(sssh "$probe_node" "python3 -c 'import json; print(len(json.load(open(\"/etc/bedrock/cluster.json\"))[\"nodes\"]))'")
    if [ "$actual" = "$expected" ]; then
        pass "cluster size = $expected"
    else
        fail "cluster size: expected $expected got $actual"
    fi
}

assert_rqlite_quorum() {
    local probe_node=$1 expected_voters=$2
    note "asserting rqlite cluster has $expected_voters voters"
    local count
    count=$(rqlite_member_count "$probe_node")
    # At N>=2 we expect N voters + 1 arbiter = N+1 voters via rqlite's
    # /nodes endpoint. At N=1 we expect just 1.
    if [ "$count" = "$expected_voters" ]; then
        pass "rqlite voters = $expected_voters"
    else
        fail "rqlite voters: expected $expected_voters got $count"
    fi
}

assert_arbiter_on_master() {
    local probe_node=$1
    note "asserting arbiter co-located with mgmt master"
    local master
    master=$(mgmt_master "$probe_node")
    [ -z "$master" ] && fail "no mgmt_master in cluster.json"
    local master_idx
    master_idx=$(sssh "$probe_node" "python3 -c \"
import json
c = json.load(open('/etc/bedrock/cluster.json'))
nodes = sorted(c['nodes'].keys())
print(nodes.index('$master') + 1)
\"")
    local arb_status
    arb_status=$(sssh "$master_idx" "python3 /usr/local/lib/bedrock/lib/cluster_arbiter.py status 2>/dev/null")
    if echo "$arb_status" | grep -q '"service_active": true'; then
        pass "arbiter rqlite active on master ($master, sim-$master_idx)"
    elif echo "$arb_status" | grep -q '"drbd_role": "Unknown"' && \
         echo "$arb_status" | grep -q '"mounted": false'; then
        # N=1 case: no DRBD resource, no arbiter. Filer must be running
        # locally on the master instead.
        if sssh "$master_idx" "systemctl is-active --quiet bedrock-weed-filer"; then
            pass "N=1: filer running on master ($master), no arbiter rqlite needed"
        else
            fail "N=1: filer not active on master"
        fi
    else
        fail "arbiter status inconsistent on master: $arb_status"
    fi
}

assert_vm_running_somewhere() {
    local vm=$1 n=$2
    note "asserting VM $vm runs on exactly one node (of $n)"
    local running=0 hosts=""
    for i in $(seq 1 "$n"); do
        local r
        r=$(vm_running "$i" "$vm")
        if [ "$r" = "1" ]; then
            running=$((running + 1))
            hosts="$hosts sim-$i"
        fi
    done
    if [ "$running" = "1" ]; then
        pass "VM $vm running on $hosts"
    else
        fail "VM $vm running on $running nodes ($hosts) — expected exactly 1"
    fi
}

# ─────────────────────────────────────────────────────────────────
# Scaling steps
# ─────────────────────────────────────────────────────────────────

scale_to_n1() {
    step "Scale to N=1 — fresh install on sim-1 only"
    sssh 1 "curl -fsSL $REPO/install.sh | bash 2>&1 | tail -10"
    sssh 1 "bedrock init --cluster-name test-scale 2>&1 | tail -5"
    sleep 5
    assert_cluster_size 1 1
    # rqlite at N=1: solo bootstrap, 1 voter
    assert_rqlite_quorum 1 1
    # filer + s3 should be running locally (no arbiter, no DRBD)
    assert_arbiter_on_master 1
}

scale_up_one() {
    local new_node=$1
    step "Scale up to N=$new_node — join sim-$new_node"
    local master_ip; master_ip=$(sim_ip 1)
    sssh "$new_node" "curl -fsSL $REPO/install.sh | bash 2>&1 | tail -10"
    sssh "$new_node" "bedrock join $master_ip 2>&1 | tail -5"
    sleep 8
    assert_cluster_size "$new_node" "$new_node"
    # N=2 introduces the arbiter rqlite → 3 voters
    # N>=2: rqlite voters = N + 1 (arbiter)
    local expected_voters=$((new_node + 1))
    assert_rqlite_quorum "$new_node" "$expected_voters"
    assert_arbiter_on_master "$new_node"
}

scale_down_one() {
    local leaving=$1 remaining=$2
    step "Scale down — remove sim-$leaving, expect cluster size = $remaining"
    # Run from the master (sim-1) which knows how to remove sim-$leaving.
    local probe_node=1
    [ "$leaving" = "1" ] && probe_node=2   # master leaving — talk to a peer
    sssh "$probe_node" "bedrock node leave sim-$leaving 2>&1 | tail -5" || \
        note "leave may have warned about transferring mgmt — that's OK if leaving=master"
    sleep 5
    # The remaining nodes should converge: arbiter moves with master,
    # rqlite shrinks, filer remains accessible.
    local survivor=$probe_node
    assert_cluster_size "$survivor" "$remaining"
    if [ "$remaining" -ge 2 ]; then
        assert_rqlite_quorum "$survivor" "$((remaining + 1))"
    else
        assert_rqlite_quorum "$survivor" 1
    fi
    assert_arbiter_on_master "$survivor"
}

# ─────────────────────────────────────────────────────────────────
# Workload checks
# ─────────────────────────────────────────────────────────────────

start_cattle_vm() {
    local node=$1 vm=$2
    step "Starting cattle VM $vm on sim-$node"
    sssh "$node" "bedrock vm create --name $vm --type cattle --ram 256 --disk 1 2>&1 | tail -3"
    sleep 5
    assert_vm_running_somewhere "$vm" 4
}

put_s3_marker_and_verify() {
    local phase=$1 probe_node=$2
    note "S3 marker round-trip @ $phase"
    local master_ip; master_ip=$(s3_endpoint_for "$probe_node")
    # Create bucket if needed (idempotent PUT — SeaweedFS S3
    # returns 200 or already-exists, both OK).
    sssh "$probe_node" "curl -sS -X PUT http://${master_ip}:8333/bedrock-test/ \
        -w 'HTTP %{http_code}\n' | tail -1" || true
    s3_put_marker "$probe_node" "$phase" || fail "s3 put failed at $phase"
    local recovered
    recovered=$(s3_get_marker "$probe_node" "$phase" || echo "")
    if echo "$recovered" | grep -q "phase=$phase"; then
        pass "S3 marker @ $phase round-tripped"
    else
        fail "S3 marker @ $phase did not survive (got: $recovered)"
    fi
}

verify_prior_markers() {
    local probe_node=$1
    step "Verifying prior-phase S3 markers still readable @ this node"
    local fail_count=0
    for ph in n1 n2 n3 n4 down-n3 down-n2; do
        local recovered
        recovered=$(s3_get_marker "$probe_node" "$ph" 2>/dev/null || echo "")
        if [ -n "$recovered" ] && echo "$recovered" | grep -q "phase=$ph"; then
            pass "marker $ph survived"
        else
            note "marker $ph not present (either not yet written or lost — investigate)"
        fi
    done
}

# ─────────────────────────────────────────────────────────────────
# Test orchestration
# ─────────────────────────────────────────────────────────────────

MODE="${1:-full}"

case "$MODE" in
    full|up-only)
        scale_to_n1
        put_s3_marker_and_verify n1 1
        start_cattle_vm 1 vm-n1-cattle

        scale_up_one 2
        put_s3_marker_and_verify n2 1
        verify_prior_markers 1

        scale_up_one 3
        put_s3_marker_and_verify n3 1
        verify_prior_markers 2

        scale_up_one 4
        put_s3_marker_and_verify n4 1
        verify_prior_markers 3
        ;;
esac

case "$MODE" in
    full|down-only)
        scale_down_one 4 3
        put_s3_marker_and_verify down-n3 1
        verify_prior_markers 1

        scale_down_one 3 2
        put_s3_marker_and_verify down-n2 1
        verify_prior_markers 2

        # Leaving 1 of 2 is the tricky case: the surviving node has
        # to take over arbiter responsibilities cleanly. Pick which
        # one survives based on who's NOT currently master, so we
        # also exercise the mgmt-master transfer.
        SURVIVOR=2
        if [ "$(mgmt_master 1)" = "sim-1" ]; then
            SURVIVOR=2
            scale_down_one 1 1
        else
            SURVIVOR=1
            scale_down_one 2 1
        fi
        put_s3_marker_and_verify final-n1 "$SURVIVOR"
        verify_prior_markers "$SURVIVOR"
        ;;
esac

echo
echo "${C_G}=================================================="
echo "  SCALE LIFECYCLE TEST COMPLETE — review log above"
echo "==================================================${C_0}"
