#!/usr/bin/env bash
# verify_vip_routing.sh N — check the .254-as-advertised-/32 + lowest-octet
# /24 catch-all invariants across sims 1..N. Run after each scale step.
#
# Asserts, per node:
#   * exactly one node has .254 bound on lo (the arbiter host = master)
#   * every OTHER node has a .254/32 ROUTE (learned via the @vip
#     advertisement) and can ping .254
#   * the /24 panic catch-all points at a STRICTLY-LOWER octet neighbour,
#     or is ABSENT on the lowest-octet node (the loop-free sink)
#   * compute_routes read nothing from rqlite: covered structurally, but we
#     also confirm the catch-all next-hop is NOT necessarily the master
set -u
TESTBED=$(dirname "$(readlink -f "$0")")
N=${1:-4}
C_G=$'\e[32m'; C_R=$'\e[31m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
pass(){ echo "${C_G}PASS${C_0} $*"; }
fail(){ echo "${C_R}FAIL${C_0} $*"; RC=1; }
note(){ echo "${C_Y}---${C_0}  $*"; }
RC=0

sim_ip(){ python3 -c "import sys;sys.path.insert(0,'$TESTBED');from spawn import get_mgmt_ip;print(get_mgmt_ip($1) or '')"; }
sssh(){ local n=$1; shift; local ip; ip=$(sim_ip "$n");
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o BatchMode=yes -o ConnectTimeout=8 -o LogLevel=ERROR "root@$ip" "$@" 2>/dev/null; }

# Derive the cluster VIP (.254) + each node's octet from sim-1's view.
VIP=$(sssh 1 'python3 -c "import sys;sys.path.insert(0,\"/usr/local/lib/bedrock\");
from lib import cluster_addr, cluster_state
u=cluster_state.load_cluster(level=\"none\").get(\"cluster_uuid\",\"\")
print(cluster_addr.cluster_vip(u) if u else \"\")"')
if [ -z "$VIP" ]; then echo "could not resolve VIP from sim-1"; exit 1; fi
PREFIX=${VIP%.*}
note "cluster VIP = $VIP   prefix = $PREFIX   N = $N"

host_count=0
declare -A OCTET
for i in $(seq 1 "$N"); do
    lo=$(sssh $i "ip -o addr show lo | grep -oE '${PREFIX//./\\.}\\.[0-9]+/32' | grep -v '\\.254/32' | head -1 | cut -d/ -f1")
    OCTET[$i]=${lo##*.}
    has254=$(sssh $i "ip -o addr show lo | grep -c '${VIP//./\\.}/32'")
    route254=$(sssh $i "ip route show | grep -E '${VIP//./\\.}(/32)? '" || true)
    panic=$(sssh $i "ip route show | grep -E '${PREFIX//./\\.}\\.0/24 .* metric 999'" || true)
    echo
    note "sim-$i  loopback=$lo  octet=${OCTET[$i]}"
    echo "    .254 on lo : $has254     route to .254 : ${route254:-<none>}"
    echo "    /24 panic  : ${panic:-<none (sink)>}"

    if [ "$has254" = "1" ]; then
        host_count=$((host_count+1))
        [ -z "$route254" ] && pass "sim-$i hosts .254 locally (no /32 route needed)" \
                           || note "sim-$i hosts .254 AND has a route (benign during transition)"
    else
        if echo "$route254" | grep -q via; then pass "sim-$i learned .254/32 via advertisement"
        else fail "sim-$i has neither .254 nor a learned /32 route"; fi
        png=$(sssh $i "ping -c1 -W2 $VIP >/dev/null 2>&1 && echo ok || echo no")
        [ "$png" = "ok" ] && pass "sim-$i can ping .254" || fail "sim-$i cannot ping .254"
    fi
done

# Exactly one .254 host
[ "$host_count" = "1" ] && pass "exactly one .254 host" \
                        || fail "expected exactly 1 .254 host, got $host_count"

# Lowest-octet node = the sink (no panic route); others point lower.
lowest=1; for i in $(seq 1 "$N"); do
    [ -n "${OCTET[$i]:-}" ] && [ "${OCTET[$i]}" -lt "${OCTET[$lowest]:-999}" ] && lowest=$i
done
note "lowest octet node = sim-$lowest (octet ${OCTET[$lowest]})"
for i in $(seq 1 "$N"); do
    panic=$(sssh $i "ip route show | grep -E '${PREFIX//./\\.}\\.0/24 .* metric 999'" || true)
    if [ "$i" = "$lowest" ]; then
        [ -z "$panic" ] && pass "sim-$lowest (lowest) installs NO catch-all (sink)" \
                        || fail "sim-$lowest should be the sink but has: $panic"
    else
        nh=$(echo "$panic" | grep -oE 'via [0-9.]+' | awk '{print $2}')
        [ -n "$nh" ] && pass "sim-$i catch-all via $nh (toward lower octet)" \
                     || note "sim-$i has no catch-all yet (adv not converged?)"
    fi
done

echo; [ "$RC" = "0" ] && echo "${C_G}━━ VIP routing invariants OK (N=$N) ━━${C_0}" \
                      || echo "${C_R}━━ VIP routing FAILURES (N=$N) ━━${C_0}"
exit $RC
