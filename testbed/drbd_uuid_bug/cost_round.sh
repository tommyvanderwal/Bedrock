#!/bin/bash
# Resync-cost comparison: full failover+heal with resume-io on the minority.
# Captures whether heal is auto/incremental (patched) vs split-brain/manual (stock),
# and the resync volume (DRBD's 'will sync X KB').  Usage: cost_round.sh <label>
cd /home/tommy/projects/Bedrock/testbed/drbd_uuid_bug || exit 1
LABEL=${1:?need label}
EV=/home/tommy/projects/Bedrock/docs/bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/evidence/cost-$LABEL
mkdir -p "$EV"
IP0=192.168.2.30; IP1=192.168.2.27; IP2=192.168.2.28; IP3=192.168.2.29
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
s0(){ $SSH root@$IP0 "$@"; }; s2(){ $SSH root@$IP2 "$@"; }
gi(){ $SSH root@$1 "drbdadm get-gi bugtest/0" 2>/dev/null | cut -d: -f1; }
echo "===== COST ROUND ($LABEL) ====="
bash repro.sh on 0 "drbdadm secondary bugtest 2>/dev/null; true" >/dev/null 2>&1
for n in 0 1 2 3; do bash repro.sh on $n "dmesg -C" >/dev/null 2>&1; done
bash repro.sh promote >/dev/null 2>&1
C0=$(gi $IP0); echo "C0 (common ancestor) = $C0"
bash repro.sh part >/dev/null 2>&1
# wait until frozen (suspended:quorum) + give the lost_contact arm time to settle
for i in 1 2 3 4 5 6 7 8; do s0 "drbdadm status bugtest 2>/dev/null | grep -q suspended:quorum" && break; sleep 2; done
sleep 4
CPRE=$(gi $IP0); echo "before resume-io (frozen, armed): $CPRE  ($(s0 "drbdadm status bugtest 2>/dev/null | grep -oE 'suspended:[a-z]+' | head -1"))"
# resume-io the MINORITY (the bug trigger)
s0 "drbdadm resume-io bugtest" >/dev/null 2>&1; sleep 6
LMIN=$(gi $IP0)
echo "minority sim-1 after resume-io: $LMIN  (rotated: $([ "$C0" != "$LMIN" ] && echo YES || echo NO))"
# failover: majority sim-3 force-promote + write W=256M
s2 "drbdadm primary --force bugtest" >/dev/null 2>&1
s2 "drbdadm resume-io bugtest 2>/dev/null; true" >/dev/null 2>&1
timeout 60 $SSH root@$IP2 "dd if=/dev/urandom of=/dev/drbd25 bs=1M count=256 oflag=direct status=none 2>/dev/null; sync; echo wrote256M" 2>/dev/null
MMAJ=$(gi $IP2); echo "majority sim-3 after promote+write: $MMAJ"
# realistic failover: the witness-denied minority is DEMOTED before heal (pcount<=1)
echo "--- demote minority losers (sim-1,sim-2) before heal ---"
s0 "drbdadm secondary bugtest 2>&1 | tail -1; echo sim1-role=\$(drbdadm role bugtest 2>/dev/null)"
$SSH root@$IP1 "drbdadm secondary bugtest 2>&1 | tail -1" 2>/dev/null
# heal
bash repro.sh heal >/dev/null 2>&1; sleep 15
echo "--- heal state (per node) ---"
for n in 0 1 2 3; do bash repro.sh on $n "drbdadm status bugtest 2>&1 | head -7" > "$EV/heal_status_n$n.txt" 2>&1; done
SB=$(s0 "drbdadm status bugtest 2>&1 | grep -ciE 'StandAlone|split'"); echo "minority StandAlone/split connections: $SB"
# capture resync volume now (auto path)
for n in 0 1 2 3; do $SSH root@$(eval echo \$IP$n) "dmesg -T | grep -oE 'will sync [0-9]+ KB' | tail -2" 2>/dev/null | sed "s/^/n$n: /"; done | tee "$EV/willsync_auto.txt"
# if split-brain, resolve on the minority losers and re-measure
NEEDMANUAL=no
if [ "$SB" -gt 0 ]; then
  NEEDMANUAL=yes
  echo "--- split-brain: resolving via --discard-my-data on minority (sim-1,sim-2) ---"
  for ip in $IP0 $IP1; do $SSH root@$ip "drbdadm secondary bugtest 2>/dev/null; drbdadm disconnect bugtest 2>/dev/null; drbdadm -- --discard-my-data connect bugtest 2>&1 | tail -2" 2>/dev/null; done
  sleep 15
  for n in 0 1 2 3; do $SSH root@$(eval echo \$IP$n) "dmesg -T | grep -oE 'will sync [0-9]+ KB' | tail -2" 2>/dev/null | sed "s/^/n$n: /"; done | tee "$EV/willsync_after_discard.txt"
fi
# wait for UpToDate
for i in $(seq 1 25); do s0 "drbdadm status bugtest" 2>/dev/null | grep -qE "Inconsistent|Sync" || break; sleep 6; done
FINAL=$(s0 "drbdadm status bugtest 2>&1 | grep -c UpToDate")
echo "heal_auto_incremental=$([ "$NEEDMANUAL" = no ] && echo YES || echo NO)  manual_discard_required=$NEEDMANUAL  final_uptodate_disks=$FINAL" | tee "$EV/summary.txt"
echo "C0=$C0 minority_after_resumeio=$LMIN majority=$MMAJ rotated=$([ "$C0" != "$LMIN" ] && echo YES || echo NO)" >> "$EV/summary.txt"
bash repro.sh reset >/dev/null 2>&1
for i in $(seq 1 15); do s0 "drbdadm status bugtest" 2>/dev/null | grep -qE "Inconsistent|Sync" || break; sleep 6; done
echo "DONE ($LABEL)"
