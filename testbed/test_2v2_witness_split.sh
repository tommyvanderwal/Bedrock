#!/usr/bin/env bash
# 2v2 witness-pivotal partition (Tommy's test): cut {leader + 1 follower} OFF
# from {the other 2 + the witness}. Leader's side (no witness) can't reach
# quorum → steps DOWN (DRBD frozen on its OLD uuid, .254 released). The other
# side + witness (201 of 401) takes over. Exactly one .254 on the winning side.
#
# The arbiter DRBD + netd are MULTIPATH over every node address (mgmt
# 192.168.2.x, 4 mesh link-locals 169.254.x, loopback 100.83.252.x). A real
# partition must drop ALL of a cross-group peer's addresses — blocking one path
# just fails over. SSH (workstation->node:22) and intra-group links stay up.
set -u
TESTBED=$(dirname "$(readlink -f "$0")"); cd "$TESTBED"
C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_B=$'\e[34m'; C_0=$'\e[0m'
pass(){ echo "${C_G}PASS${C_0} $*"; }; fail(){ echo "${C_R}FAIL${C_0} $*"; RC=1; }
note(){ echo "${C_Y}---${C_0} $*"; }; step(){ echo; echo "${C_B}### $* ###${C_0}"; }
RC=0
WITNESS=$(hostname -I | awk '{print $1}'); WPORT=12321
simip(){ python3 -c "import sys;sys.path.insert(0,'.');from spawn import get_mgmt_ip;print(get_mgmt_ip($1) or '')"; }
S(){ local n=$1; shift; ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "root@$(simip $n)" "$@" 2>/dev/null; }
has254(){ S "$1" 'ip -o addr show lo 2>/dev/null | grep -c "\.254/32"'; }
uuid(){ S "$1" 'cut -d" " -f1 /sys/kernel/debug/drbd/resources/cluster/volumes/0/data_gen_id 2>/dev/null | head -1'; }
# all blockable addresses of node $1 (mgmt + link-locals + loopback, not .254)
addrs(){ S "$1" 'ip -o -4 addr show | grep -oE "inet (192\.168\.2\.[0-9]+|169\.254\.[0-9.]+|100\.83\.252\.[0-9]+)" | awk "{print \$2}" | grep -v "\.254$" | sort -u'; }

M=""; for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && M=$i; done
[ -z "$M" ] && { echo "no leader"; exit 1; }
LOSE=("$M"); WIN=()
for i in 1 2 3 4; do [ "$i" = "$M" ] || { [ ${#LOSE[@]} -lt 2 ] && LOSE+=("$i") || WIN+=("$i"); }; done
step "Pre: leader=sim-$M  losing={${LOSE[*]}} (no witness)  winning={${WIN[*]}}+witness"
note "leader uuid pre = $(uuid $M)"

# Build cross-group DROP rules. For a node, drop every address of each enemy.
partition_node(){ local n=$1; shift; local enemies=("$@"); local cmd=""
  for e in "${enemies[@]}"; do
    for a in $(addrs "$e"); do
      cmd+="iptables -I INPUT -s $a -j DROP; iptables -I OUTPUT -d $a -j DROP; "
    done
  done
  S "$n" "$cmd" >/dev/null
}
step "Partition {${LOSE[*]}} <-> {${WIN[*]}}, and {${LOSE[*]}} <-> witness"
for n in "${LOSE[@]}"; do
  partition_node "$n" "${WIN[@]}"
  S "$n" "iptables -I OUTPUT -d $WITNESS -p tcp --dport $WPORT -j DROP; iptables -I OUTPUT -d $WITNESS -p udp --dport $WPORT -j DROP; iptables -I INPUT -s $WITNESS -p tcp --sport $WPORT -j DROP; iptables -I INPUT -s $WITNESS -p udp --sport $WPORT -j DROP" >/dev/null
done
for n in "${WIN[@]}"; do partition_node "$n" "${LOSE[@]}"; done
note "partitioned at $(date +%T); waiting 85s..."; sleep 85

step "Result"
wc=0; nm=""; for i in "${WIN[@]}"; do [ "$(has254 $i)" = "1" ] && { wc=$((wc+1)); nm=$i; }; done
lc=0; for i in "${LOSE[@]}"; do [ "$(has254 $i)" = "1" ] && lc=$((lc+1)); done
[ "$wc" = "1" ] && pass "winning side took over: new leader sim-$nm (witness side)" || fail "winning side has $wc .254 (expected 1)"
[ "$lc" = "0" ] && pass "losing side {${LOSE[*]}} stepped down (no .254)" || fail "losing side still holds .254 ($lc)"
um=$(uuid $M)
note "leader(sim-$M): uuid=$um role=$(S $M 'drbdadm role cluster') $(S $M 'drbdsetup status cluster 2>/dev/null | grep -oE "suspended:[a-z]+" | head -1')"
for i in 1 2 3 4; do note "sim-$i: .254=$(has254 $i) role=$(S $i 'drbdadm role cluster') uuid=$(uuid $i)"; done

step "Restore"
for i in 1 2 3 4; do S $i 'iptables -F INPUT; iptables -F OUTPUT; iptables -P INPUT ACCEPT; iptables -P OUTPUT ACCEPT' >/dev/null; done
note "flushed; 45s heal"; sleep 45
hc=0; for i in 1 2 3 4; do [ "$(has254 $i)" = "1" ] && hc=$((hc+1)); done
[ "$hc" = "1" ] && pass "post-heal: exactly one .254 holder" || fail "post-heal: $hc .254 holders"
echo; [ "$RC" = 0 ] && echo "${C_G}━━ 2v2 witness-split PASS ━━${C_0}" || echo "${C_R}━━ FAILURES ━━${C_0}"
exit $RC
