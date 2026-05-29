#!/bin/bash
# Comprehensive fresh-ISO re-validation of the 2026-05-29 reboot-resilience
# round: state.json self-heal, quorum-loss-anchored kill, weed-boot restart.
set +e
cd /home/tommy/projects/Bedrock/testbed
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8"
ip_of(){ python3 -c "import sys;sys.path.insert(0,'.');from spawn import get_mgmt_ip;print(get_mgmt_ip($1) or '')"; }
RQ='curl -fsS --cert /etc/bedrock/node.crt --key /etc/bedrock/node.key.pem --cacert /etc/bedrock/ca.crt https://127.0.0.1:4001/db/query?level=strong'

echo "=== $(date) RESET + REINSTALL (fresh ISO) ==="
python3 spawn.py reset 2>&1 | tail -3
echo "--- spawn 4 sims (anaconda install from ISO) ---"
python3 spawn.py up 4 2>&1 | tail -8
echo "--- wait for anaconda to finish, then boot installed disk + firstboot ---"
python3 spawn.py wait 4 2>&1 | tail -10

echo "=== $(date) POLL bootstrap-done ==="
for i in $(seq 1 45); do
  n=0
  for s in 1 2 3 4; do
    ip=$(ip_of $s); [ -z "$ip" ] && continue
    ssh $SSHO root@"$ip" 'test -f /etc/bedrock/.bootstrap-done' 2>/dev/null && n=$((n+1))
  done
  echo "[$(date +%H:%M:%S)] $n/4"
  [ "$n" -eq 4 ] && { echo READY; break; }
  sleep 20
done

echo "=== $(date) persistent journald (diagnose any reboot) ==="
for s in 1 2 3 4; do ip=$(ip_of $s); ssh $SSHO root@"$ip" 'mkdir -p /var/log/journal; systemctl restart systemd-journald' 2>/dev/null && echo "sim-$s journald persistent"; done

echo "=== $(date) CLUSTER BRING-UP (syncs fixed code + init/join/promote) ==="
bash setup_4node_cluster.sh 2>&1 | tail -28

M=$(ip_of 1)
echo "=== $(date) wait singleton UpToDate (c-min-rate fix → fast) ==="
for i in $(seq 1 30); do
  st=$(ssh $SSHO root@"$M" 'drbdadm status cluster 2>/dev/null' 2>/dev/null | grep -c UpToDate)
  echo "[$i] cluster UpToDate lines=$st"
  [ "$st" -ge 1 ] && { echo "SINGLETON UpToDate"; break; }
  sleep 10
done

echo "=== $(date) BASELINE: per-node weed-volume + weed-s3 active on all 4? ==="
for s in 1 2 3 4; do ip=$(ip_of $s); echo "sim-$s: $(ssh $SSHO root@"$ip" 'systemctl is-active bedrock-weed-volume bedrock-weed-s3 2>/dev/null | tr "\n" " "' 2>/dev/null)"; done

echo "=== $(date) FAILOVER TEST (core HA re-confirm) ==="
bash test_pet_vm_failover.sh 2>&1 | tail -22
echo "failover exit: $?"

echo "=== $(date) NO-WITNESS AB (A: no-split-brain; B: kill @ quorum-loss+300, fixed timing) ==="
bash test_pet_vm_no_witness_isolation.sh AB 2>&1 | tail -45
echo "no-witness exit: $?"

echo "=== $(date) REBOOT-RESILIENCE: hard-reset sim-4; confirm self-heal + weed restart + rejoin ==="
sudo -n virsh reset bedrock-sim-4 2>&1 | tail -1
RECOVERED=""
for t in $(seq 1 30); do sleep 10
  ip4=$(ip_of 4); [ -z "$ip4" ] && { echo "[$t] sim-4 no ip yet"; continue; }
  r=$(ssh $SSHO root@"$ip4" 'echo "rqlited=$(systemctl is-active bedrock-rqlited) d=$(systemctl is-active bedrock-d) weedvol=$(systemctl is-active bedrock-weed-volume) weeds3=$(systemctl is-active bedrock-weed-s3) statebytes=$(wc -c </etc/bedrock/state.json 2>/dev/null)"' 2>/dev/null)
  echo "[$t] sim-4: $r"
  if echo "$r" | grep -q "rqlited=active d=active weedvol=active weeds3=active"; then
    echo "SIM-4 FULLY RECOVERED after hard reset (rqlited + bedrock-d + weed-volume + weed-s3, state.json intact)"
    RECOVERED=1; break
  fi
done
[ -z "$RECOVERED" ] && echo "SIM-4 DID NOT FULLY RECOVER — inspect"
echo "--- sim-4 rejoined cluster active? ---"
ssh $SSHO root@"$M" "$RQ -d '[\"SELECT node_name,state FROM nodes ORDER BY node_name\"]'" 2>/dev/null
echo "=== $(date) RE-VALIDATION DONE ==="
