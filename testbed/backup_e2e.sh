#!/bin/bash
# Full backup/restore e2e on a fresh cluster: install -> cluster -> SeaweedFS
# health -> pet VM -> local-S3 kopia target -> backup -> incremental -> restore.
set +e
cd /home/tommy/projects/Bedrock/testbed
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=10"
ip_of(){ python3 -c "import sys;sys.path.insert(0,'.');from spawn import get_mgmt_ip;print(get_mgmt_ip($1) or '')"; }
RQ='curl -fsS --cert /etc/bedrock/node.crt --key /etc/bedrock/node.key.pem --cacert /etc/bedrock/ca.crt https://127.0.0.1:4001/db/query?level=strong'

echo "=== $(date) RESET + INSTALL ==="
python3 spawn.py reset 2>&1 | tail -2
python3 spawn.py up 4 2>&1 | tail -3
python3 spawn.py wait 4 2>&1 | tail -6
echo "=== $(date) POLL bootstrap-done ==="
for i in $(seq 1 45); do n=0; for s in 1 2 3 4; do ip=$(ip_of $s); [ -z "$ip" ] && continue; ssh $SSHO root@"$ip" 'test -f /var/lib/bedrock-install/.bootstrap-done' 2>/dev/null && n=$((n+1)); done; echo "[$(date +%H:%M:%S)] $n/4"; [ "$n" -eq 4 ] && break; sleep 20; done
for s in 1 2 3 4; do ip=$(ip_of $s); ssh $SSHO root@"$ip" 'mkdir -p /var/log/journal; systemctl restart systemd-journald' 2>/dev/null; done
echo "=== $(date) CLUSTER BRING-UP ==="
bash setup_4node_cluster.sh 2>&1 | tail -10
M=$(ip_of 1)
echo "=== $(date) wait singleton UpToDate (the cluster resource lives on a node subset, so poll all 4) ==="
for i in $(seq 1 30); do mx=0; for s in 1 2 3 4; do ip=$(ip_of $s); [ -z "$ip" ] && continue; st=$(ssh $SSHO root@"$ip" 'drbdadm status cluster 2>/dev/null' 2>/dev/null | grep -c "disk:UpToDate"); [ "${st:-0}" -gt "$mx" ] && mx=$st; done; echo "[$i] cluster max disk:UpToDate across nodes=$mx"; [ "$mx" -ge 1 ] && break; sleep 10; done

echo "=== $(date) SeaweedFS convergence: re-render + restart weed on all nodes ==="
for s in 1 2 3 4; do ip=$(ip_of $s); echo "sim-$s:"; ssh $SSHO root@"$ip" 'python3 -c "import sys;sys.path.insert(0,\"/usr/local/lib/bedrock\");from lib import seaweedfs; seaweedfs.promote_to_master_volume_host()" 2>&1 | tail -1; grep MASTER_PEERS /etc/bedrock/seaweedfs.env' 2>/dev/null; done
sleep 10
LO=$(ssh $SSHO root@"$M" 'python3 -c "import json;print(json.load(open(\"/etc/bedrock/state.json\"))[\"loopback_ip\"])"' 2>/dev/null); VIP="${LO%.*}.254"
echo "=== SeaweedFS health GATE (wait for filer + >=3 volume servers) ==="
HEALTHY=0
for i in $(seq 1 12); do
  chk=$(ssh $SSHO root@"$M" "echo 'cluster.check' | weed shell -master=$LO:9333 2>&1 | grep -iE 'volume servers|filers'" 2>/dev/null)
  nfiler=$(echo "$chk" | grep -oE '[0-9]+ filers' | grep -oE '[0-9]+'); nvs=$(echo "$chk" | grep -oE '[0-9]+ volume servers' | grep -oE '[0-9]+')
  echo "[$i] filers=${nfiler:-0} volume_servers=${nvs:-0}"
  [ "${nfiler:-0}" -ge 1 ] && [ "${nvs:-0}" -ge 3 ] && { HEALTHY=1; echo "SeaweedFS HEALTHY"; break; }
  # nudge convergence again (weed-only restart; no bedrock-d/master churn)
  for s in 1 2 3 4; do ip=$(ip_of $s); ssh $SSHO root@"$ip" 'python3 -c "import sys;sys.path.insert(0,\"/usr/local/lib/bedrock\");from lib import seaweedfs; seaweedfs.promote_to_master_volume_host()" 2>/dev/null' 2>/dev/null; done
  sleep 15
done
[ "$HEALTHY" = 1 ] || echo "!! SeaweedFS NOT healthy after gate — backup likely to fail"

echo "=== $(date) create pet VM kbtest ==="
ssh $SSHO root@"$M" 'bedrock vm create kbtest --type pet --ram 512 --disk 1 2>&1 | tail -2' 2>/dev/null
for t in $(seq 1 24); do sleep 5; r=$(ssh $SSHO root@"$M" "$RQ -d '[\"SELECT state,host FROM vms WHERE vm_name=\\\"kbtest\\\"\"]'" 2>/dev/null | python3 -c "import sys,json;v=json.load(sys.stdin)['results'][0].get('values');print(v[0] if v else 'none')" 2>/dev/null); up=$(ssh $SSHO root@"$M" 'drbdadm status vm-kbtest-disk0 2>/dev/null|grep -c UpToDate' 2>/dev/null); echo "[$t] vm=$r UpToDate=$up"; echo "$r" | grep -q running && [ "${up:-0}" -ge 2 ] && break; done
HOME_NODE=$(ssh $SSHO root@"$M" "$RQ -d '[\"SELECT host FROM vms WHERE vm_name=\\\"kbtest\\\"\"]'" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['results'][0]['values'][0][0])" 2>/dev/null)
echo "kbtest home=$HOME_NODE"

echo "=== $(date) bucket + backup target ($VIP:8333) ==="
ssh $SSHO root@"$M" "python3 -c \"import sys;sys.path.insert(0,'/usr/local/lib/bedrock');from lib import seaweedfs;print('bucket:',seaweedfs.ensure_bucket('bedrock-backups'))\"" 2>/dev/null
ssh $SSHO root@"$M" 'set -e
AK=$(python3 -c "import json;print(json.load(open(\"/etc/bedrock/seaweedfs-s3.json\"))[\"identities\"][0][\"credentials\"][0][\"accessKey\"])")
SK=$(python3 -c "import json;print(json.load(open(\"/etc/bedrock/seaweedfs-s3.json\"))[\"identities\"][0][\"credentials\"][0][\"secretKey\"])")
curl -fsS -X POST http://127.0.0.1:8001/api/backup/targets -H "Content-Type: application/json" -d "{\"target_id\":\"local\",\"kind\":\"kopia-s3\",\"s3_endpoint\":\"'"$VIP"':8333\",\"s3_bucket\":\"bedrock-backups\",\"s3_region\":\"us-east-1\",\"s3_disable_tls\":true,\"s3_access_key\":\"$AK\",\"s3_secret_key\":\"$SK\",\"encryption_password\":\"bedrock-test-pw\"}" 2>&1 | head -c 300' 2>/dev/null; echo

bk(){ ssh $SSHO root@"$M" 'curl -fsS -X POST http://127.0.0.1:8001/api/vms/kbtest/backup -H "Content-Type: application/json" -d "{\"target_id\":\"local\"}"' 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('operation_id',''))" 2>/dev/null; }
# The FIRST full backup streams the whole disk to kopia/SeaweedFS and can
# legitimately take 10-15 min (esp. if it races SeaweedFS leadership
# settling); poll up to ~16 min. Incrementals dedup and finish in seconds.
pollop(){ for t in $(seq 1 160); do sleep 6; s=$(ssh $SSHO root@"$M" "$RQ -d '[\"SELECT state,error FROM operations WHERE id=$1\"]'" 2>/dev/null | python3 -c "import sys,json;v=json.load(sys.stdin)['results'][0].get('values');print(v[0] if v else 'none')" 2>/dev/null); echo "  op$1 [$t]: $(echo "$s"|head -c 150)"; echo "$s"|grep -qE 'completed|failed' && break; done; }
opstate(){ ssh $SSHO root@"$M" "$RQ -d '[\"SELECT state FROM operations WHERE id=$1\"]'" 2>/dev/null | python3 -c "import sys,json;v=json.load(sys.stdin)['results'][0].get('values');print(v[0][0] if v else 'none')" 2>/dev/null; }
echo "=== $(date) BACKUP #1 (full) ==="; O1=$(bk); echo "op=$O1"; pollop "$O1"
echo "=== $(date) BACKUP #2 (incremental) ==="; O2=$(bk); echo "op=$O2"; pollop "$O2"
echo "=== recorded backups (vms[].backups via API — the real store) ==="
ssh $SSHO root@"$M" 'curl -fsS http://127.0.0.1:8001/api/vms/kbtest/backups 2>/dev/null' 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);[print('  ',b.get('label'),b.get('kopia_snapshot_id'),'bytes_added=%s dur=%.1fs'%(b.get('bytes_added'),b.get('duration_s') or 0)) for b in d.get('backups',[])] or print('  (none recorded)')" 2>/dev/null
echo "=== kopia sources (disks + metadata) ==="
ssh $SSHO root@"$M" 'export KOPIA_PASSWORD="$(cat /etc/bedrock/backup.key)"; set -a; . /etc/bedrock/backup-credentials/local.env 2>/dev/null; set +a; kopia --config-file=/etc/bedrock/kopia/local.config snapshot list --all 2>/dev/null | grep -oE "[^ ]+:kbtest:[^ ]+" | sort -u' 2>/dev/null

echo "=== $(date) RESTORE (newest backup -> poweroff -> restore -> start -> HA) ==="
SNAP=$(ssh $SSHO root@"$M" 'curl -fsS http://127.0.0.1:8001/api/vms/kbtest/backups 2>/dev/null' 2>/dev/null | python3 -c "import sys,json;b=json.load(sys.stdin).get('backups',[]);print(b[0].get('kopia_snapshot_id','') if b else '')" 2>/dev/null)
echo "restoring from newest snapshot=$SNAP"
RO=$(ssh $SSHO root@"$M" "curl -fsS -X POST http://127.0.0.1:8001/api/vms/kbtest/restore -H 'Content-Type: application/json' -d '{\"target_id\":\"local\",\"kopia_snapshot_id\":\"$SNAP\"}'" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('operation_id',''))" 2>/dev/null)
echo "restore op=$RO"; pollop "$RO"
RST=$(opstate "$RO"); echo "restore op final state=$RST"
echo "=== post-restore: restore op COMPLETED + VM running + DRBD UpToDate 2-way (HA)? ==="
OK=0
for t in $(seq 1 30); do sleep 6; r=$(ssh $SSHO root@"$M" "$RQ -d '[\"SELECT state,host FROM vms WHERE vm_name=\\\"kbtest\\\"\"]'" 2>/dev/null | python3 -c "import sys,json;v=json.load(sys.stdin)['results'][0].get('values');print(v[0] if v else 'none')" 2>/dev/null); up=$(ssh $SSHO root@"$M" 'drbdadm status vm-kbtest-disk0 2>/dev/null|grep -c UpToDate' 2>/dev/null); echo "[$t] vm=$r UpToDate=$up"; [ "$RST" = completed ] && echo "$r"|grep -q running && [ "${up:-0}" -ge 2 ] && { echo "✓ RESTORE VALIDATED: op completed, VM running, HA (UpToDate=$up)"; OK=1; break; }; done
[ "$OK" = 1 ] || echo "✗ RESTORE NOT VALIDATED (op=$RST)"
echo "=== last_backup_error / last_restore_error (via API) ==="
ssh $SSHO root@"$M" 'curl -fsS http://127.0.0.1:8001/api/vms/kbtest/backups 2>/dev/null' 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print('  last_backup_error:',d.get('last_backup_error'));print('  last_restore:',d.get('last_restore'));print('  last_restore_error:',d.get('last_restore_error'))" 2>/dev/null
echo; echo "=== $(date) E2E DONE ==="
