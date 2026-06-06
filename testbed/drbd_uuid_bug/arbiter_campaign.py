#!/usr/bin/env python3
"""Arbiter fence-peer partition campaign — P2 end-to-end validation.

Tests the REAL `cluster` arbiter resource (quorum all + on-no-quorum suspend-io +
fencing resource-only + the real /usr/local/lib/bedrock/bedrock-fence-peer handler
reading the real netd-written /run/bedrock/fence-verdict.json) under live partitions.

THE bug being proven fixed: a quorum-lost FROZEN old-Primary must NEVER mint a new
current-UUID (a "sibling") — the old resume-io path did, causing false split-brain on
heal. The fence-peer fix makes the loser exit 6 (outdate self, stay frozen) and the
winner exit 4 (outdate peer, regain quorum). We assert the frozen Primary's UUID
*generation* (bit 0 masked — that bit is the role flag, cleared legitimately on demote)
is byte-for-byte unchanged across the freeze, and the heal resyncs clean (no split-brain).

Scenarios (each x N):
  A  isolate one Secondary           -> Primary stays leader, WINS (exit 4), regains quorum
  B  isolate the Primary (minority)  -> Primary LOSES (exit 6), freezes, force-secondary, no mint
  C  2v2 even split, Primary's side loses (witness pivotal) -> same LOSE/freeze, no mint

"Instant does not exist": every phase is timestamped (cut -> DRBD peer-loss -> fence-peer
fire -> decision -> force-secondary / new-primary -> quorum regain -> heal/resync) and the
deltas from t_cut are reported. Usage: arbiter_campaign.py [A|B|C|all] [iterations]
"""
import json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from spawn import get_mgmt_ip  # noqa: E402

NODES = [1, 2, 3, 4]
RES = "cluster"
WPORT = 12321
EVID = Path("/home/tommy/projects/Bedrock/docs/bug-reports-upstream/"
            "drbd-quorum-lost-primary-uuid-rotation/evidence/arbiter-campaign")
EVID.mkdir(parents=True, exist_ok=True)

_ip = {}
def ip(n):
    if n not in _ip:
        _ip[n] = get_mgmt_ip(n)
        if not _ip[n]:
            raise SystemExit(f"cannot resolve sim-{n} mgmt IP")
    return _ip[n]

SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
       "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR",
       "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/cm-arb-%h",
       "-o", "ControlPersist=120"]

def S(n, cmd, timeout=30):
    try:
        r = subprocess.run(SSH + [f"root@{ip(n)}", cmd], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"

def witness_ip():
    return subprocess.run(["hostname", "-I"], capture_output=True, text=True
                          ).stdout.split()[0]

# ---- state probes -----------------------------------------------------------
def uuid_raw(n):
    return S(n, f"cut -d' ' -f1 /sys/kernel/debug/drbd/resources/{RES}/volumes/0/"
                "data_gen_id 2>/dev/null | head -1")

def uuid_gen(n):
    """current-UUID with bit 0 (the role flag) masked off -> the generation identity."""
    u = uuid_raw(n)
    try:
        return int(u, 16) & ~1
    except (ValueError, TypeError):
        return None

def role(n):
    return S(n, f"drbdadm role {RES} 2>/dev/null") or "?"

def has254(n):
    return S(n, "ip -o addr show lo 2>/dev/null | grep -c '\\.254/32'") == "1"

def quorum(n):
    return S(n, f"drbdsetup status {RES} 2>/dev/null | grep -oE 'quorum:[a-z]+' | head -1")

def verdict(n):
    raw = S(n, "cat /run/bedrock/fence-verdict.json 2>/dev/null")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None

def addrs(n):
    out = S(n, "ip -o -4 addr show | grep -oE 'inet (192\\.168\\.2\\.[0-9]+|"
               "169\\.254\\.[0-9.]+|100\\.83\\.252\\.[0-9]+)' | awk '{print $2}' | "
               "grep -v '\\.254$' | sort -u")
    return [a for a in out.splitlines() if a.strip()]

# ---- partition / heal -------------------------------------------------------
def partition(groups_block, also_block_witness):
    """groups_block: dict node -> list of enemy nodes whose every addr to DROP.
    also_block_witness: set of nodes that additionally drop the witness UDP/TCP port."""
    W = witness_ip()
    enemy_addrs = {n: addrs(n) for n in NODES}
    for n, enemies in groups_block.items():
        cmd = ""
        for e in enemies:
            for a in enemy_addrs[e]:
                cmd += (f"iptables -I INPUT 1 -s {a} -j DROP; "
                        f"iptables -I OUTPUT 1 -d {a} -j DROP; ")
        if n in also_block_witness:
            for proto in ("tcp", "udp"):
                cmd += (f"iptables -I OUTPUT 1 -d {W} -p {proto} --dport {WPORT} -j DROP; "
                        f"iptables -I INPUT 1 -s {W} -p {proto} --sport {WPORT} -j DROP; ")
        if cmd:
            S(n, cmd)

def heal_all():
    for n in NODES:
        S(n, "iptables -F INPUT; iptables -F OUTPUT; iptables -P INPUT ACCEPT; "
             "iptables -P OUTPUT ACCEPT; true")

# ---- timing capture (events2 + journalctl) ----------------------------------
def start_events2(n, secs):
    # stdbuf -oL: events2 full-buffers stdout to a file, and `timeout`'s SIGTERM kills it
    # before the buffer flushes -> empty log. Line-buffering flushes each event immediately.
    # No --now: stream the change events (with --timestamps) over the whole window.
    # setsid + </dev/null: fully detach so the capture survives this ssh session closing
    # (a plain `( ... & )` gets SIGHUP on channel close -> empty log).
    S(n, f"pkill -f 'events2 --timestamps {RES}' 2>/dev/null; "
         f"setsid bash -c 'timeout {secs} stdbuf -oL drbdsetup events2 --timestamps {RES} "
         f"> /tmp/ev-{RES}.log 2>&1' </dev/null >/dev/null 2>&1 & echo started", timeout=10)

def fetch_events2(n):
    return S(n, f"cat /tmp/ev-{RES}.log 2>/dev/null")

# events2 --timestamps emits tz-aware ISO8601 in the SIM's tz (UTC, '+00:00').
ISO = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:]+\.\d+(?:[+-]\d\d:\d\d|Z)?)")
def parse_ts(line):
    m = ISO.match(line.strip())
    if not m:
        return None
    s = m.group(1).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None

FENCE_TS = re.compile(r"^(\d+\.\d+)\s")
def parse_fence(n, since_epoch):
    """First fence-peer fire + decision timestamps (epoch) from the real handler's syslog.
    Returns (fire_epoch|None, decision_epoch|None, exit_code|None)."""
    raw = S(n, f"journalctl -t bedrock-fence-peer -o short-unix "
               f"--since '@{int(since_epoch)}' --no-pager 2>/dev/null")
    fire = decision = code = None
    for line in raw.splitlines():
        m = FENCE_TS.match(line)
        if not m:
            continue
        ts = float(m.group(1))
        if ("deciding" in line or "asking bedrock-d" in line) and fire is None:
            fire = ts
        for kw, c in (("-> WIN", 4), ("-> LOSE", 6), ("-> UNDECIDED", 1)):
            if kw in line and decision is None:
                decision, code = ts, c
    return fire, decision, code

# ---- health gate ------------------------------------------------------------
def wait_healthy(timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        n254 = sum(1 for n in NODES if has254(n))
        ups = {n: S(n, f"drbdsetup status {RES} 2>/dev/null | grep -cE 'peer-disk:UpToDate'")
               for n in NODES}
        upok = all(ups[n] == "3" for n in NODES)
        if n254 == 1 and upok:
            return True
        time.sleep(4)
    return False

# ---- scenario runner --------------------------------------------------------
def find_master():
    for n in NODES:
        if has254(n):
            return n
    return None

def run_scenario(tag, iteration):
    """tag in {A,B,C}. Returns a result dict (timeline + assertions)."""
    print(f"\n\033[1;36m===== Scenario {tag} iteration {iteration} =====\033[0m")
    if not wait_healthy():
        print("  \033[31mcluster not healthy pre-test; aborting iteration\033[0m")
        return {"tag": tag, "iter": iteration, "error": "unhealthy-pre"}
    M = find_master()
    print(f"  master (Primary/.254) = sim-{M}")
    others = [n for n in NODES if n != M]

    if tag == "A":
        victim = others[0]                       # isolate one Secondary
        block = {victim: [n for n in NODES if n != victim]}
        for n in NODES:
            if n != victim:
                block.setdefault(n, []).append(victim)
        witness_block = {victim}
        lose_side, frozen_node, expect = [victim], None, "win"   # Primary M wins; victim is a Secondary
        desc = f"isolate Secondary sim-{victim}; Primary sim-{M} should WIN (exit 4)"
    elif tag == "B":
        block = {M: others}                      # isolate the Primary alone
        for n in others:
            block.setdefault(n, []).append(M)
        witness_block = {M}
        lose_side, frozen_node, expect = [M], M, "lose"
        desc = f"isolate Primary sim-{M} (minority, no witness); should LOSE (exit 6), freeze, no mint"
    elif tag == "C":
        lose = [M, others[0]]                     # Primary + 1 follower, NO witness
        win = others[1:]                          # other 2 + witness
        block = {}
        for n in lose:
            block.setdefault(n, []).extend(win)
        for n in win:
            block.setdefault(n, []).extend(lose)
        witness_block = set(lose)                 # losing side also cut from witness
        lose_side, frozen_node, expect = lose, M, "lose"
        desc = f"2v2: lose={lose} (no witness) vs win={win}+witness; Primary sim-{M} should LOSE, freeze, no mint"
    else:
        raise ValueError(tag)

    print(f"  {desc}")
    gen_before = {n: uuid_gen(n) for n in NODES}
    print("  uuid-gen before: " + "  ".join(
        f"sim-{n}={gen_before[n]:#018x}" if gen_before[n] else f"sim-{n}=?" for n in NODES))

    # clear the kernel log on every node so the post-heal split-brain check AND the dmesg
    # transition-timing parse see ONLY this iteration (dmesg is a ring buffer across runs).
    for n in NODES:
        S(n, "dmesg -C 2>/dev/null; true")
    time.sleep(0.5)

    t_cut = time.time()
    partition(block, witness_block)
    # M's seconds-since-boot at ~cut, to align its dmesg [monotonic] timestamps to t_cut.
    try:
        cut_uptime = float(S(M, "cut -d' ' -f1 /proc/uptime"))
    except (ValueError, TypeError):
        cut_uptime = None
    print(f"  \033[33mCUT at {time.strftime('%H:%M:%S', time.localtime(t_cut))}\033[0m")

    # observe ~60s: a full failover needs the loser to stand down (~23s) AND the winning
    # side to elect+promote a new master (~10-25s more). Sample the frozen node's uuid
    # every 2s throughout to prove it never mints mid-flight.
    timeline = {"cut": t_cut}
    gen_during = {}
    deadline = t_cut + 60
    while time.time() < deadline:
        time.sleep(2.0)
        if frozen_node:
            g = uuid_gen(frozen_node)
            if g is not None:
                gen_during[round(time.time() - t_cut, 1)] = f"{g:#018x}"

    # fence decisions across ALL nodes: the old Primary fires it on peer-loss; on B/C the
    # new Primary on the winning side also fires it when it promotes + loses the cut peer.
    fence = {}
    for n in NODES:
        fire, dec, code = parse_fence(n, t_cut - 2)
        if fire or dec:
            fence[n] = {
                "fire_+s": round(fire - t_cut, 2) if fire else None,
                "decision_+s": round(dec - t_cut, 2) if dec else None,
                "exit": code,
                "verdict": {4: "WIN", 6: "LOSE", 1: "UNDECIDED"}.get(code),
            }
            print(f"    sim-{n} fence-peer: fire@+{fence[n]['fire_+s']}s -> "
                  f"{fence[n]['verdict']} (exit {code}) @+{fence[n]['decision_+s']}s")
    timeline["fence"] = {f"sim-{n}": fence.get(n) for n in NODES}

    gen_after_cut = {n: uuid_gen(n) for n in NODES}
    n254_now = [n for n in NODES if has254(n)]
    roles_now = {n: role(n) for n in NODES}
    print(f"  .254 now on: {['sim-%d' % n for n in n254_now]}   roles: "
          + " ".join(f"{n}:{roles_now[n][:4]}" for n in NODES))

    # ---- assertion: frozen old-Primary did NOT mint a sibling ----
    mint = None
    if frozen_node:
        b, a = gen_before[frozen_node], gen_after_cut[frozen_node]
        mint = (b is not None and a is not None and b != a)
        vmsg = "MINTED A SIBLING (BUG!)" if mint else "generation unchanged (no sibling) OK"
        print(f"  \033[{'31' if mint else '32'}m  frozen sim-{frozen_node}: "
              f"gen {b:#018x} -> {a:#018x}  => {vmsg}\033[0m")

    # DRBD-side transition timing from M's dmesg (cleared at cut; [monotonic] timestamps
    # aligned to cut_uptime). Reliable, no backgrounded stream to buffer/lose.
    dm = S(M, "dmesg 2>/dev/null")
    def dmesg_first(*needles):
        for line in dm.splitlines():
            m = re.match(r"\[\s*(\d+\.\d+)\]", line)
            if m and all(x in line for x in needles):
                return float(m.group(1))
        return None
    t_peer_lost = (dmesg_first("conn(", "Connected ->") or dmesg_first("Connection closed")
                   or dmesg_first("PingAck did not arrive"))
    t_qno = dmesg_first("quorum( yes -> no")
    t_qyes = dmesg_first("quorum( no -> yes")              # regain (winner side / scenario A)
    t_sec = dmesg_first("role( Primary -> Secondary")       # M's own demote (force-secondary)

    def d(t):
        return None if (t is None or cut_uptime is None) else round(t - cut_uptime, 2)
    timeline.update({
        "drbd_peer_lost_+s": d(t_peer_lost), "quorum_lost_+s": d(t_qno),
        "quorum_regain_+s": d(t_qyes), "old_primary_force_secondary_+s": d(t_sec),
    })
    print(f"  timing (delta from cut): peer-lost={timeline['drbd_peer_lost_+s']}  "
          f"quorum-lost={timeline['quorum_lost_+s']}  quorum-regain={timeline['quorum_regain_+s']}  "
          f"force-secondary={timeline['old_primary_force_secondary_+s']}")

    # ---- heal & verify clean resync ----
    print("  \033[33mHEAL (flush iptables)\033[0m")
    heal_all()
    t_heal = time.time()
    resync_done = None
    split_brain = False
    for _ in range(40):
        time.sleep(5)
        st = S(M, f"drbdsetup status {RES} 2>/dev/null")
        sb = S(M, "dmesg | grep -ciE 'split-brain|Split-Brain'")
        if sb and sb.isdigit() and int(sb) > 0:
            split_brain = True
        if "Inconsistent" not in st and "Sync" not in st and st.count("peer-disk:UpToDate") == 3:
            resync_done = round(time.time() - t_heal, 1)
            break
    gen_after_heal = {n: uuid_gen(n) for n in NODES}
    n254_heal = sum(1 for n in NODES if has254(n))
    print(f"  post-heal: resync_done=+{resync_done}s  split_brain={split_brain}  .254_holders={n254_heal}")

    result = {
        "tag": tag, "iter": iteration, "desc": desc, "master": M,
        "expect": expect, "frozen_node": frozen_node,
        "gen_before": {n: (f"{gen_before[n]:#018x}" if gen_before[n] else None) for n in NODES},
        "gen_after_cut": {n: (f"{gen_after_cut[n]:#018x}" if gen_after_cut[n] else None) for n in NODES},
        "gen_after_heal": {n: (f"{gen_after_heal[n]:#018x}" if gen_after_heal[n] else None) for n in NODES},
        "gen_during_samples": gen_during,
        "frozen_minted_sibling": mint,
        "timeline": timeline,
        "n254_after_cut": [f"sim-{n}" for n in n254_now],
        "roles_after_cut": {f"sim-{n}": roles_now[n] for n in NODES},
        "resync_done_s": resync_done, "split_brain": split_brain,
        "n254_after_heal": n254_heal,
    }
    # hard fails (safety invariants) vs soft notes (observations worth recording)
    passed = True
    reasons = []   # hard
    notes = []     # soft
    fn_verdict = (fence.get(frozen_node) or {}).get("verdict") if frozen_node else None
    if frozen_node and mint:
        passed = False
        reasons.append("frozen Primary minted a sibling UUID")
    if expect == "lose":
        # LOSE (exit 6) is ideal; UNDECIDED (exit 1) also safe (freeze) -> force-secondary demotes.
        if fn_verdict not in ("LOSE", "UNDECIDED"):
            notes.append(f"sim-{frozen_node} fence verdict was {fn_verdict} (expected LOSE/UNDECIDED)")
        # checked on the DURING-partition snapshot (n254_now), not post-heal where the old
        # master may legitimately reclaim mastership.
        if frozen_node in n254_now:
            passed = False
            reasons.append(f"old Primary sim-{frozen_node} kept .254 during partition (didn't stand down)")
        winners = [n for n in n254_now if n != frozen_node]
        if not winners:
            passed = False
            reasons.append("no new master promoted on the winning side during the partition")
    if expect == "win":
        if (fence.get(M) or {}).get("verdict") != "WIN":
            notes.append(f"master sim-{M} fence verdict was {(fence.get(M) or {}).get('verdict')} (expected WIN)")
        if M not in n254_now:
            passed = False
            reasons.append("winning Primary lost .254")
    if split_brain:
        passed = False
        reasons.append("split-brain on heal")
    if n254_heal != 1:
        passed = False
        reasons.append(f"{n254_heal} .254 holders after heal (want 1)")
    if resync_done is None:
        passed = False
        reasons.append("resync did not complete within window")
    result["passed"] = passed
    result["reasons"] = reasons
    result["notes"] = notes
    color = "32" if passed else "31"
    print(f"  \033[1;{color}m{'PASS' if passed else 'FAIL'} {tag}#{iteration}"
          + (f": {'; '.join(reasons)}" if reasons else "") + "\033[0m"
          + (f"\n    notes: {'; '.join(notes)}" if notes else ""))
    return result


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").upper()
    iters = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    tags = ["A", "B", "C"] if which == "ALL" else [which]
    results = []
    try:
        for tag in tags:
            for it in range(1, iters + 1):
                results.append(run_scenario(tag, it))
                time.sleep(3)
    finally:
        print("\n\033[33mfinal heal + health gate\033[0m")
        heal_all()
        wait_healthy(150)
    out = EVID / f"campaign-{which.lower()}.json"
    out.write_text(json.dumps(results, indent=2))
    npass = sum(1 for r in results if r.get("passed"))
    print(f"\n\033[1m=== {npass}/{len(results)} passed === -> {out}\033[0m")
    for r in results:
        st = "PASS" if r.get("passed") else ("ERR" if r.get("error") else "FAIL")
        print(f"  {r['tag']}#{r.get('iter')}: {st}"
              + (f"  ({'; '.join(r.get('reasons', []))})" if r.get("reasons") else "")
              + (f"  [{r['error']}]" if r.get("error") else ""))
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
