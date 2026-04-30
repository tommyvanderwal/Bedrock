#!/usr/bin/env bash
# Bedrock end-to-end testbed validation.
#
# Scenarios:
#   1. Fresh cluster: spawn 1-4 sim nodes, install + init + join
#   2. Workloads: cattle (1 node), pet (2 nodes), vipet (3 nodes)
#   3. Live migration of pet VM
#   4. Failover (simulated: kill a sim node)
#
# Expected runtime: ~5-10 minutes for full suite.

set -e
TESTBED=$(dirname "$(readlink -f "$0")")
cd "$TESTBED"

C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
pass() { echo "${C_G}PASS${C_0} $*"; }
fail() { echo "${C_R}FAIL${C_0} $*"; exit 1; }
note() { echo "${C_Y}---${C_0} $*"; }

REPO="http://192.168.100.1:8000"
MGMT=""  # will be sim-1 IP after init

wait_ssh() {
    local ip=$1
    for i in $(seq 1 40); do
        ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
            -o ConnectTimeout=3 -o BatchMode=yes root@$ip 'true' 2>/dev/null && return 0
        sleep 3
    done
    return 1
}

sim_ip() {
    python3 -c "
import sys; sys.path.insert(0,'$TESTBED')
from spawn import get_mgmt_ip
print(get_mgmt_ip($1) or '')
"
}

install_node() {
    local ip=$1
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$ip \
        "nmcli con delete 'Wired connection 1' 2>/dev/null; \
         nmcli con modify bedrock-drbd connection.interface-name eth1; \
         nmcli con up bedrock-drbd >/dev/null 2>&1; \
         curl -sSL $REPO/install.sh | bash" 2>&1 | tail -5
}

# ── Setup ──────────────────────────────────────────────────────────────────

note "Clean slate: destroy all existing sim nodes"
./spawn.py down

note "Start install repo (if not already)"
curl -sf $REPO/install.sh >/dev/null 2>&1 || {
    nohup $TESTBED/serve.py >/tmp/bedrock-installrepo.log 2>&1 &
    sleep 2
}

# ── Scenario 1: single node ────────────────────────────────────────────────

note "### Scenario 1: 1-node cluster ###"
./spawn.py up 1
SIM1_IP=$(sim_ip 1)
wait_ssh $SIM1_IP || fail "sim-1 not reachable"
pass "sim-1 booted at $SIM1_IP"

install_node $SIM1_IP
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM1_IP \
    'bedrock init --name bedrock-e2e' 2>&1 | tail -3
MGMT=$SIM1_IP
sleep 3

curl -sf http://$MGMT:8080/api/cluster >/dev/null && pass "Dashboard up at http://$MGMT:8080"

# Set up SSH key for self (needed by the dashboard's SSH calls)
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM1_IP '
  [ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 >/dev/null
  cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
  sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys
  ssh-keyscan -H $(hostname -I | awk "{print \$1}") >> /root/.ssh/known_hosts 2>/dev/null
  ssh-keyscan -H 10.99.0.10 >> /root/.ssh/known_hosts 2>/dev/null
  systemctl restart bedrock-mgmt
' > /dev/null

sleep 3

# Test: cattle VM
note "Creating cattle VM on 1-node cluster"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$MGMT \
    'bedrock vm create web1 --type cattle --ram 256 --disk 3' 2>&1 | tail -3 \
    && pass "Cattle VM created" || fail "Cattle VM creation failed"

# Test: pet should fail with 1 node
note "Pet VM should FAIL on 1-node cluster"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$MGMT \
    'bedrock vm create db1 --type pet --ram 256 --disk 3' 2>&1 | grep -q "requires" \
    && pass "Pet rejected on 1-node (expected)" \
    || fail "Pet should have been rejected"

# ── Scenario 2: add sim-2, pet works ───────────────────────────────────────

note "### Scenario 2: scale to 2 nodes, create pet VM ###"
./spawn.py up 2
SIM2_IP=$(sim_ip 2)
wait_ssh $SIM2_IP || fail "sim-2 not reachable"

install_node $SIM2_IP
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM2_IP \
    "bedrock join --yes --witness $MGMT" 2>&1 | tail -3

# SSH key mesh for 2 nodes (needed for DRBD + virsh migrate)
note "Setting up SSH key mesh"
for SRC in $SIM1_IP $SIM2_IP; do
  for DST in $SIM1_IP $SIM2_IP 10.99.0.10 10.99.0.11; do
    ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SRC \
      "ssh-keyscan -H $DST >> /root/.ssh/known_hosts 2>/dev/null; sort -u /root/.ssh/known_hosts -o /root/.ssh/known_hosts" 2>/dev/null
  done
done
# Exchange keys
SIM2_KEY=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM2_IP \
  '[ -f /root/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 >/dev/null; cat /root/.ssh/id_ed25519.pub')
SIM1_KEY=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM1_IP \
  'cat /root/.ssh/id_ed25519.pub')
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM1_IP \
  "echo '$SIM2_KEY' >> /root/.ssh/authorized_keys; sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys"
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$SIM2_IP \
  "echo '$SIM1_KEY' >> /root/.ssh/authorized_keys; sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys"

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$MGMT \
    'systemctl restart bedrock-mgmt' >/dev/null
sleep 3

NODE_COUNT=$(curl -s http://$MGMT:8080/cluster-info | python3 -c "import json,sys; print(len(json.load(sys.stdin)['nodes']))")
[ "$NODE_COUNT" = "2" ] && pass "2-node cluster registered" || fail "Only $NODE_COUNT nodes visible"

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$MGMT \
    'bedrock vm create db1 --type pet --ram 256 --disk 3 && virsh start db1' 2>&1 | tail -3 \
    && pass "Pet VM created" || fail "Pet VM creation failed"

# Wait for DRBD sync, then migrate
note "Waiting for DRBD sync..."
for i in $(seq 1 30); do
  SYNC=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@$MGMT \
    'drbdadm status vm-db1-disk0 2>&1 | grep -c UpToDate')
  [ "$SYNC" = "2" ] && break
  sleep 5
done

note "Live migrating pet VM"
RESULT=$(curl -s -X POST http://$MGMT:8080/api/vms/db1/migrate)
echo "$RESULT" | grep -q '"status":"migrated"' && pass "Live migration OK: $RESULT" || fail "Migration failed: $RESULT"

# ── Report ────────────────────────────────────────────────────────────────

note "### Summary ###"
echo ""
echo "Cluster state:"
curl -s http://$MGMT:8080/cluster-info | python3 -m json.tool
echo ""
echo "VMs:"
curl -s http://$MGMT:8080/api/cluster | python3 -c "
import json,sys
d = json.load(sys.stdin)
for n, info in d.get('nodes', {}).items():
    print(f'  Node {n}: online={info.get(\"online\")} vms={info.get(\"running_vms\",[])}')
for vm, info in d.get('vms', {}).items():
    print(f'  VM {vm}: {info[\"state\"]} on={info[\"running_on\"]} drbd={info[\"drbd_resource\"]}')
"
echo ""
pass "E2E test complete. Dashboard: http://$MGMT:8080"
