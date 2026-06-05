#!/usr/bin/env bash
# Death-oracle partition tests (docs/witness-death-oracle.md). Run on a
# healthy 4-node DRBD-promoted cluster with the Echo witness up.
#
#   A. Isolate a NON-master node → it must DEFER (death-oracle: it sees the
#      master's slot FRESH+HOSTING on the witness) → stays follower, no
#      takeover, master keeps .254. Restore → rejoins.
#   B. Isolate the MASTER → the 3-node majority (node-majority bound) takes
#      over; the isolated master self-demotes (clears HOSTING, releases .254).
#      Exactly one .254 holder throughout. Reset the master to recover.
#
# Uses the proven isolation: iptables+ebtables drop on br0 except the
# workstation, plus link-down of the mesh/DRBD NICs ONLY (enp2s0..enp5s0),
# KEEPING enp1s0/br0 so SSH + witness survive (lesson_iptables_bridge_multicast).
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
role(){ S "$1" 'cat /etc/bedrock/state.json 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get(\"role\",\"\"))" 2>/dev/null'; }
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
for i in 1 2 3 4; do [ "$i" = "$M" ] || NONM=${NONM:-$i}; done
note "non-master pick = sim-$NONM"

step "A. Isolate NON-master sim-$NONM → must DEFER (death-oracle), master keeps .254"
isolate "$NONM" >/dev/null; A0=$(date +%s)
note "waiting 45s..."; sleep 45
# master unchanged, still exactly one .254 holder (= sim-$M)
mnow=$(master_idx)
[ "$mnow" = "$M" ] && pass "master still sim-$M (.254 unmoved)" || fail "master moved to sim-$mnow (expected unmoved)"
# the isolated node must NOT host .254 and must NOT have promoted
iso254=$(has254 "$NONM")
[ "$iso254" = "0" ] && pass "isolated sim-$NONM hosts no .254 (deferred, no takeover)" || fail "isolated sim-$NONM grabbed .254 — split-brain!"
S "$NONM" 'journalctl -u bedrock-d --no-pager --since "50 seconds ago" 2>/dev/null | grep -iE "following|FOLLOWER|defer|REFUSED — slot.*fresh + HOSTING" | tail -3'
restore "$NONM" >/dev/null; note "restored sim-$NONM; 30s to rejoin"; sleep 30
[ "$(master_idx)" = "$M" ] && pass "post-restore: master still sim-$M, no steal" || note "master now sim-$(master_idx)"

step "B. Isolate MASTER sim-$M → majority takes over; isolated master self-demotes"
isolate "$M" >/dev/null; B0=$(date +%s)
note "waiting 75s for majority takeover + master self-demote..."; sleep 75
newm=$(master_idx)
if [ -n "$newm" ] && [ "$newm" != "$M" ]; then
  pass "new master = sim-$newm (majority took over via node-majority bound)"
else
  fail "no new .254 host (got '$newm'); takeover did not complete"
fi
# the old master must have released .254 (self-demote) — count total .254 holders
cnt=0; for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && cnt=$((cnt+1)); done
[ "$cnt" = "1" ] && pass "exactly one .254 holder (no dual-.254 split-brain)" || fail "$cnt nodes host .254 (expected 1)"
S "$M" 'journalctl -u bedrock-d --no-pager --since "80 seconds ago" 2>/dev/null | grep -iE "self-demote|NoQuorum|releasing|hosting=0|release .254|demot" | tail -4'
note "resetting sim-$M to recover (was isolated)"; virsh reset bedrock-sim-$M >/dev/null 2>&1

echo; [ "$RC" = 0 ] && echo "${C_G}━━ death-oracle partition tests PASS ━━${C_0}" || echo "${C_R}━━ FAILURES ━━${C_0}"
exit $RC
