#!/usr/bin/env python3
"""RCA: which demote primitive is FAST on a frozen (suspended:quorum) DRBD Primary?

Isolate the current master (-> frozen loser), then time each candidate primitive on it,
ONE per run (pass which as argv[1]), so the slow ones don't mask each other. Heals after.
Usage: demote_timing.py <outdate|secondary_force|fuser|umount|secondary_plain>
"""
import sys, time, subprocess
sys.path.insert(0, ".")
from arbiter_campaign import S, ip, addrs, NODES, witness_ip, find_master, heal_all, RES

WPORT = 12321

def isolate(m):
    W = witness_ip()
    others = [n for n in NODES if n != m]
    enemy = {n: addrs(n) for n in NODES}
    cmd = ""
    for e in others:
        for a in enemy[e]:
            cmd += f"iptables -I INPUT 1 -s {a} -j DROP; iptables -I OUTPUT 1 -d {a} -j DROP; "
    for proto in ("tcp", "udp"):
        cmd += (f"iptables -I OUTPUT 1 -d {W} -p {proto} --dport {WPORT} -j DROP; "
                f"iptables -I INPUT 1 -s {W} -p {proto} --sport {WPORT} -j DROP; ")
    S(m, cmd)
    # block the others from m too
    for n in others:
        c = ""
        for a in enemy[m]:
            c += f"iptables -I INPUT 1 -s {a} -j DROP; iptables -I OUTPUT 1 -d {a} -j DROP; "
        S(n, c)

def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "secondary_force"
    m = find_master()
    if m is None:
        print("no master"); return
    minor = S(m, f"drbdadm sh-dev {RES}").strip().rsplit("drbd", 1)[-1] or "?"
    print(f"master = sim-{m}, drbd minor = {minor}, primitive = {which}")
    # stop bedrock-d's own converge from racing our manual demote during this probe
    S(m, "systemctl stop bedrock-d")
    isolate(m)
    print("isolated; waiting 12s for suspended:quorum freeze ...")
    time.sleep(12)
    susp = S(m, f"drbdsetup status {RES} 2>/dev/null | grep -oE 'suspended:[a-z]+' | head -1")
    role = S(m, f"drbdadm role {RES}")
    print(f"pre: role={role} {susp}")

    cmds = {
        "outdate":          f"drbdsetup outdate {minor}",
        "secondary_force":  f"drbdsetup secondary {RES} --force=yes",
        "secondary_plain":  f"drbdadm secondary {RES}",
        "fuser":            "fuser -k -KILL -m /var/lib/bedrock/cluster",
        "umount":           "umount -l /var/lib/bedrock/cluster",
    }
    cmd = cmds[which]
    print(f"--- timing: {cmd} ---")
    t0 = time.time()
    out = S(m, f"timeout 60 {cmd} 2>&1; echo RC=$?", timeout=70)
    dt = time.time() - t0
    print(f"took {dt:.1f}s -> {out.strip()[:300]}")
    post_role = S(m, f"drbdadm role {RES}")
    post_susp = S(m, f"drbdsetup status {RES} 2>/dev/null | grep -oE 'suspended:[a-z]+|role:[A-Za-z]+' | paste -sd,")
    print(f"post: role={post_role} status={post_susp}")

    print("healing ...")
    heal_all()
    S(m, "systemctl start bedrock-d")
    print("done (bedrock-d restarted on master; cluster will reconverge)")

if __name__ == "__main__":
    main()
