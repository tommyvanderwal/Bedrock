#!/bin/bash
# Validate the fence-peer-as-arbiter design on STOCK (unpatched) DRBD 9.3.2.
# A controllable stub fence-peer handler returns win(4)/lose(6) per a verdict file.
# Proves: lose -> master outdates SELF, stays frozen, NEVER mints; win -> master
# outdates PEERS, regains quorum, writes (legit single mint).  Usage: fence_validate.sh
set -u
IPS=(192.168.2.30 192.168.2.27 192.168.2.28 192.168.2.29)
HOSTS=(bedrock-305eec bedrock-56f13b bedrock-853bdf bedrock-9fa125)
RES=bugtest; MINOR=25; PORT=7790; BACKLINK=/dev/drbd-bugtest-backing
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
EV=/home/tommy/projects/Bedrock/docs/bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/evidence/fence-validate
mkdir -p "$EV"
S(){ local n=$1; shift; $SSH root@"${IPS[$n]}" "$@"; }
ALL(){ for n in 0 1 2 3; do S "$n" "$@"; done; }
gi(){ S "$1" "drbdadm get-gi $RES/0 2>/dev/null" | cut -d: -f1; }
say(){ echo -e "\n\033[1;36m== $* ==\033[0m"; }

HANDLER='#!/bin/bash
echo "$(date "+%H:%M:%S.%3N") FENCE-PEER res=$DRBD_RESOURCE peer=$DRBD_PEER_NODE_ID cstate=$DRBD_CSTATE u2d=$UP_TO_DATE_NODES verdict=$(cat /tmp/fence-verdict 2>/dev/null)" >> /tmp/fence.log
case "$(cat /tmp/fence-verdict 2>/dev/null)" in
  win)  exit 4 ;;   # P_OUTDATED  -> outdate the lost peer (I win, continue)
  lose) exit 6 ;;   # P_PRIMARY   -> outdate myself (I yield)
  *)    exit 1 ;;   # undecided   -> leave IO frozen
esac'

gen_res(){ cat <<EOF
resource $RES {
  options { quorum all; on-no-quorum suspend-io; auto-promote no; on-suspended-primary-outdated force-secondary; }
  net { protocol C; fencing resource-only; }
  handlers { fence-peer "/usr/local/bin/test-fence-peer"; }
  volume 0 { device /dev/drbd$MINOR; disk $BACKLINK; meta-disk internal; }
  on ${HOSTS[0]} { node-id 0; address ${IPS[0]}:$PORT; }
  on ${HOSTS[1]} { node-id 1; address ${IPS[1]}:$PORT; }
  on ${HOSTS[2]} { node-id 2; address ${IPS[2]}:$PORT; }
  on ${HOSTS[3]} { node-id 3; address ${IPS[3]}:$PORT; }
  connection-mesh { hosts ${HOSTS[0]} ${HOSTS[1]} ${HOSTS[2]} ${HOSTS[3]}; }
}
EOF
}

partition(){ for n in 0 1 2 3; do S "$n" "iptables-save > /tmp/fv-ipt.bak"; done
  for n in 0 1; do S "$n" "iptables -I INPUT 1 -s ${IPS[2]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[2]} -j DROP; iptables -I INPUT 1 -s ${IPS[3]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[3]} -j DROP"; done
  for n in 2 3; do S "$n" "iptables -I INPUT 1 -s ${IPS[0]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[0]} -j DROP; iptables -I INPUT 1 -s ${IPS[1]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[1]} -j DROP"; done; }
heal(){ for n in 0 1 2 3; do S "$n" "iptables-restore < /tmp/fv-ipt.bak 2>/dev/null; true"; done; }
reset_clean(){ ALL "drbdadm down $RES 2>/dev/null; true"; ALL "drbdadm create-md --force $RES >/dev/null 2>&1; drbdadm up $RES"; sleep 3; S 0 "drbdadm new-current-uuid --clear-bitmap $RES/0 >/dev/null 2>&1"; sleep 4; }

say "deploy stub handler + fencing .res on all sims, re-up"
for n in 0 1 2 3; do
  printf '%s\n' "$HANDLER" | $SSH root@"${IPS[$n]}" "cat > /usr/local/bin/test-fence-peer && chmod +x /usr/local/bin/test-fence-peer; echo win > /tmp/fence-verdict; : > /tmp/fence.log"
  gen_res | $SSH root@"${IPS[$n]}" "cat > /etc/drbd.d/$RES.res"
done
reset_clean
say "confirm fencing active"
S 0 "drbdsetup show $RES | grep -iE 'fencing|fence-peer' ; drbdadm status $RES | head -2"

run_case(){ # run_case <verdict> <label>
  local V=$1 L=$2
  say "CASE $L  (verdict=$V)"
  ALL "drbdadm secondary $RES 2>/dev/null; dmesg -C; : > /tmp/fence.log"
  S 0 "echo $V > /tmp/fence-verdict"; for n in 1 2 3; do S "$n" "echo $V > /tmp/fence-verdict"; done
  S 0 "drbdadm primary $RES; dd if=/dev/urandom of=/dev/drbd$MINOR bs=1M count=16 oflag=direct status=none 2>/dev/null; sync"
  local C; C=$(gi 0); echo "C (pre) = $C"
  partition
  sleep 14   # let detection (~6-10s) + fence-peer fire
  local L1; L1=$(gi 0)
  echo "sim-1 current after partition+fence: $L1  rotated=$([ "$C" != "$L1" ] && echo YES || echo NO)"
  echo "--- sim-1 status ---"; S 0 "drbdadm status $RES 2>&1 | head -8" | tee "$EV/$L-status.txt"
  echo "--- sim-1 disk state (outdated self?) ---"; S 0 "drbdadm dstate $RES 2>&1"
  echo "--- fence-peer handler log (sim-1) ---"; S 0 "cat /tmp/fence.log 2>/dev/null" | tee "$EV/$L-fencelog.txt"
  echo "--- sim-1 dmesg (mint? outdate? quorum?) ---"; S 0 "dmesg -T | grep -iE 'new current UUID|fence|outdate|quorum\\(|susp-io|pdsk' | tail -14" | tee "$EV/$L-dmesg.txt"
  echo "MINT_LINES=$(S 0 "dmesg -T | grep -c 'new current UUID'")" | tee "$EV/$L-summary.txt"
  heal; sleep 12
  for i in $(seq 1 12); do S 0 "drbdadm status $RES" 2>/dev/null | grep -qE "Inconsistent|Sync" || break; sleep 6; done
  echo "--- after heal: sim-1 status ---"; S 0 "drbdadm status $RES 2>&1 | head -6"
  reset_clean
}

run_case lose lose-case
run_case win  win-case
echo "ALL FENCE CASES DONE"
