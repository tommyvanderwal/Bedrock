#!/usr/bin/env bash
# Death-oracle scenario C — the MASTER-CLAIMS-WITNESS pivotal path
# (docs/witness-death-oracle.md, the other half of test_death_oracle_partitions.sh).
#
# Scenarios A/B cover: A) a lone non-master defers, B) an isolated master is
# taken over by the node-majority. This covers the THIRD branch: the master is
# pushed into a MINORITY of nodes but is still PIVOTAL, so it CLAIMS the witness
# to keep its own side alive — "master has first chance to claim witnesses... if
# it almost has the votes already."
#
# Setup: N=4, W=1 → total=401, majority=201. Isolate TWO non-master nodes. The
# master keeps ONE follower → 200 node votes, deficit=1 (0<1<50 → pivotal) → the
# master claims the single witness → 200+1=201 = majority → master KEEPS .254.
# The two isolated nodes are each alone (100 votes) → far below majority → they
# defer, host no .254. Exactly one .254 holder throughout — no split-brain.
#
# Same proven isolation as A/B: drop br0 except the workstation + down the
# mesh/DRBD NICs (enp2s0..enp5s0), keep enp1s0/br0 so SSH+witness survive.
set -u
TESTBED=$(dirname "$(readlink -f "$0")"); cd "$TESTBED"
C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'
pass(){ echo "${C_G}PASS${C_0} $*"; }
fail(){ echo "${C_R}FAIL${C_0} $*"; RC=1; }
note(){ echo "${C_Y}---${C_0} $*"; }
step(){ echo; echo "${C_B}### $* ###${C_0}"; }
RC=0
WS_IP=$(hostname -I | awk '{print $1}')
simip(){ python3 -c "import sys;sys.path.insert(0,'.');from spawn import get_mgmt_ip;print(get_mgmt_ip($1) or '')"; }
S(){ local n=$1; shift; ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "root@$(simip $n)" "$@" 2>/dev/null; }
has254(){ S "$1" 'ip -o addr show lo 2>/dev/null | grep -c "\.254/32"'; }
master_idx(){ for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && { echo $i; return; }; done; echo ""; }

isolate(){ local n=$1
  S "$n" "bash -c '
    iptables -I OUTPUT -o br0 -j DROP; iptables -I INPUT -i br0 -j DROP
    iptables -I OUTPUT -o br0 -d $WS_IP -j ACCEPT; iptables -I INPUT -i br0 -s $WS_IP -j ACCEPT
    ebtables -F INPUT 2>/dev/null
    ebtables -A INPUT -p IPv4 --ip-src $WS_IP -j ACCEPT 2>/dev/null
    ebtables -A INPUT -p ARP -j ACCEPT 2>/dev/null
    ebtables -A INPUT -p IPv4 -j DROP 2>/dev/null
    for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic down 2>/dev/null; done
    echo isolated'"; }
restore(){ local n=$1
  S "$n" "bash -c '
    for nic in enp2s0 enp3s0 enp4s0 enp5s0; do ip link set \$nic up 2>/dev/null; done
    ebtables -F INPUT 2>/dev/null
    iptables -F INPUT; iptables -F OUTPUT; iptables -P INPUT ACCEPT; iptables -P OUTPUT ACCEPT
    echo restored'"; }

step "Pre: confirm healthy + exactly one .254 host"
M=$(master_idx); [ -z "$M" ] && { fail "no .254 host — cluster not healthy"; exit 1; }
pass "master = sim-$M (.254 host)"
# pick the TWO non-master nodes with the highest indices to isolate
NM=(); for i in 4 3 2 1; do [ "$i" = "$M" ] || NM+=("$i"); done
ISO1=${NM[0]}; ISO2=${NM[1]}; KEEP=${NM[2]}
note "isolating non-masters sim-$ISO1 + sim-$ISO2; master keeps sim-$KEEP → 200 votes (deficit=1, pivotal)"

step "C. Isolate TWO non-masters → master is pivotal, CLAIMS witness, keeps .254"
isolate "$ISO1" >/dev/null; isolate "$ISO2" >/dev/null
note "waiting 50s for the master to claim the witness and hold quorum..."; sleep 50
# the master must STILL host .254 (it claimed the witness to reach 201)
mnow=$(master_idx)
[ "$mnow" = "$M" ] && pass "master still sim-$M (.254 held via witness claim)" || fail "master moved/lost (got '$mnow', expected sim-$M) — claim path failed"
# the two isolated singletons must host NO .254
for x in "$ISO1" "$ISO2"; do
  h=$(has254 "$x")
  [ "$h" = "0" ] && pass "isolated sim-$x hosts no .254 (deferred)" || fail "isolated sim-$x grabbed .254 — split-brain!"
done
# exactly one .254 holder total
cnt=0; for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && cnt=$((cnt+1)); done
[ "$cnt" = "1" ] && pass "exactly one .254 holder (no dual-.254 split-brain)" || fail "$cnt nodes host .254 (expected 1)"
# evidence of the pivotal witness claim in the master's log
note "master's witness-claim evidence:"
S "$M" 'journalctl -u bedrock-d --no-pager --since "60 seconds ago" 2>/dev/null | grep -iE "claim|pivotal|votes=201/201|201/201|quorum" | tail -4'

step "Restore both isolated nodes → cluster heals, master releases its claim"
restore "$ISO1" >/dev/null; restore "$ISO2" >/dev/null
note "waiting 40s to rejoin..."; sleep 40
cnt=0; for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && cnt=$((cnt+1)); done
[ "$cnt" = "1" ] && pass "post-restore: exactly one .254 holder (heal clean)" || fail "post-restore: $cnt .254 holders (expected 1)"
[ "$(master_idx)" = "$M" ] && pass "post-restore: master still sim-$M (no needless churn)" || note "master now sim-$(master_idx)"
# the master should have RELEASED the claim once node-majority returned (tag back to HOSTING-only)
note "master's claim-release evidence (node-majority restored):"
S "$M" 'journalctl -u bedrock-d --no-pager --since "45 seconds ago" 2>/dev/null | grep -iE "releas|node-majority|claim|leader" | tail -3'

echo; [ "$RC" = 0 ] && echo "${C_G}━━ death-oracle witness-claim test PASS ━━${C_0}" || echo "${C_R}━━ FAILURES ━━${C_0}"
exit $RC
