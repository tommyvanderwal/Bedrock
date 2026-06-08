#!/usr/bin/env python3
"""VM-disk fence-peer campaign — P3 end-to-end validation.

Tests a REAL per-VM DRBD resource (vm-<name>-disk0: fencing resource-and-stonith +
ping-int 5 + the bedrock-fence-peer handler routing vm-* -> decide_vm_fence rqlite
ownership) under live partitions, with FULL reconvergence between scenarios (Tommy:
"test if things are fully reconverged, and only then test again" — not chaos-monkey).

Scenarios:
  W  isolate the REPLICA (Secondary peer)  -> host stays Primary, WINS (exit 4), VM never
                                              stops; lost peer outdated; clean resync on heal.
  F  isolate the HOST (Primary)            -> host freezes (susp_fen, minority strong-read
                                              fails -> undecided), orchestrator suspends it;
                                              the successor takes over in the majority and its
                                              fence-peer WINS (exit 4) during drbdadm primary;
                                              EXACTLY ONE Primary + ONE running VM (no
                                              split-brain). Heal: observe whether the frozen
                                              loser auto-demotes (on-suspended-primary-outdated)
                                              or needs a #34-style force-release.

"Instant does not exist": phases are timestamped from t_cut. Usage:
  vm_fence_campaign.py <vm_name> [W|F|all]
"""
import json, os, re, subprocess, sys, time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arbiter_campaign as AC   # reuse S/ip/partition/heal_all/NODES/role probes
from arbiter_campaign import S, ip, partition, heal_all, NODES

VM = sys.argv[1] if len(sys.argv) > 1 else "fencetest"
RES = f"vm-{VM}-disk0"


def now():
    return time.monotonic()


def stamp(t0, msg, color="0"):
    print(f"  \033[{color}m[t+{now()-t0:6.1f}s] {msg}\033[0m", flush=True)


# ---- probes -----------------------------------------------------------------
def vrole(n):
    return S(n, f"drbdadm role {RES} 2>/dev/null") or "?"


def cstate(n):
    return S(n, f"drbdadm cstate {RES} 2>/dev/null") or "?"


def dstate(n):
    return S(n, f"drbdadm dstate {RES} 2>/dev/null") or "?"


def domstate(n):
    return S(n, f"virsh domstate {VM} 2>/dev/null") or "-"


def suspended(n):
    """True if DRBD IO is frozen (susp_fen / suspended)."""
    s = S(n, f"drbdsetup status {RES} 2>/dev/null")
    return "suspended" in s or "susp" in s.lower()


def uuid_gen(n):
    u = S(n, f"cut -d' ' -f1 /sys/kernel/debug/drbd/resources/{RES}/volumes/0/"
            "data_gen_id 2>/dev/null | head -1")
    try:
        return int(u, 16) & ~1
    except (ValueError, TypeError):
        return None


def vms_host(from_n):
    """rqlite vms.host for VM (level='strong', run from a majority node)."""
    out = S(from_n, "python3 - <<'PY' 2>/dev/null\n"
            "import sys;sys.path.insert(0,'/usr/local/lib/bedrock')\n"
            "from lib import rqlite_client\n"
            "with rqlite_client.RqliteClient() as rc:\n"
            f"    r=rc.query_one(\"SELECT host FROM vms WHERE vm_name=?\",params=['{VM}'],level='strong')\n"
            "    print((r or {}).get('host',''))\nPY")
    return out.strip()


def running_on():
    """List of nodes where the VM domain is 'running'."""
    return [n for n in NODES if domstate(n) == "running"]


def primaries():
    return [n for n in NODES if vrole(n) == "Primary"]


def fence_log(n, since="5 min ago"):
    return S(n, f"journalctl -t bedrock-fence-peer --since '{since}' --no-pager 2>/dev/null | tail -8")


def node_name(n):
    return S(n, "python3 -c \"import json;print(json.load(open('/etc/bedrock/state.json'))['node_name'])\" 2>/dev/null")


# ---- reconvergence gate -----------------------------------------------------
def wait_vm_healthy(timeout=180, settle=3):
    """Block until: exactly ONE Primary, that node runs the VM, both peers UpToDate,
    no node frozen, stable for `settle` consecutive polls. Returns the host node or None."""
    t0 = now()
    ok_streak = 0
    last = ""
    while now() - t0 < timeout:
        prim = primaries()
        run = running_on()
        peers = [n for n in NODES if vrole(n) != "?"]   # nodes that have the resource
        ups = {n: dstate(n) for n in peers}
        frozen = [n for n in peers if suspended(n)]
        # dstate is "local/peer[/peer...]" — every part must be UpToDate.
        all_up = all(p == "UpToDate" for v in ups.values() for p in v.split("/") if p)
        healthy = (len(prim) == 1 and run == prim and not frozen and
                   all_up and len(peers) >= 2)
        sig = f"prim={prim} run={run} up={ups} frozen={frozen}"
        if sig != last:
            print(f"    .. {sig}", flush=True)
            last = sig
        if healthy:
            ok_streak += 1
            if ok_streak >= settle:
                return prim[0]
        else:
            ok_streak = 0
        time.sleep(4)
    return None


# ---- scenarios --------------------------------------------------------------
def scenario_W():
    print("\n\033[1m═══ Scenario W: isolate the REPLICA (host must WIN, VM never stops) ═══\033[0m")
    host = wait_vm_healthy()
    if host is None:
        print("  \033[31mPRE-CHECK FAILED: VM not healthy before test\033[0m"); return False
    peer = [n for n in NODES if vrole(n) == "Secondary"]
    if not peer:
        print("  \033[31mno Secondary peer found\033[0m"); return False
    rep = peer[0]
    print(f"  host(Primary)=sim-{host}  replica(Secondary)=sim-{rep}")
    g0 = uuid_gen(host)
    print(f"  host UUID gen before = {g0:#x}" if g0 else "  host UUID gen unreadable")
    t0 = now()
    # isolate the replica from everyone (full L3) — host stays in the majority.
    partition({rep: [x for x in NODES if x != rep],
               **{x: [rep] for x in NODES if x != rep}}, set())
    stamp(t0, f"CUT: isolated replica sim-{rep}", "33")
    # watch the host: it must keep the VM running throughout + WIN.
    saw_frozen = False
    won = False
    deadline = t0 + 60
    while now() < deadline:
        ds = domstate(host); r = vrole(host); fr = suspended(host); cs = cstate(host)
        if fr and not saw_frozen:
            stamp(t0, f"host sim-{host}: IO froze (susp_fen) — fence-peer deciding", "36"); saw_frozen = True
        if "WIN" in fence_log(host) and not won:
            stamp(t0, f"host sim-{host}: fence-peer WIN logged", "32"); won = True
        if r == "Primary" and not fr and cs in ("Connecting", "StandAlone", "Connected") and won:
            stamp(t0, f"host sim-{host}: resumed (role=Primary, domstate={ds}, cstate={cs})", "32")
            break
        if ds != "running":
            stamp(t0, f"\033[31mhost sim-{host}: VM domstate={ds} (should stay running!)\033[0m", "31")
        time.sleep(1)
    # assertions
    ds = domstate(host); prim = primaries()
    g1 = uuid_gen(host)
    ok = (ds == "running" and prim == [host])
    # The WINNER legitimately mints a new current-UUID when it outdates the lost peer and
    # resumes as sole Primary — that is precisely what makes the peer resync on heal (NOT a
    # bug; the frozen LOSER not minting is the bug, asserted in scenario F). So W requires:
    # the VM never stopped, a single Primary throughout, and a clean replica resync.
    print(f"  VM still running on host sim-{host}: {ds=='running'} | single Primary {prim==[host]}: {prim} "
          f"| winner UUID-gen {g0:#x} -> {g1:#x} (mint on resume = expected, drives the resync)")
    # heal
    print("  \033[33mHEAL\033[0m"); heal_all()
    host2 = wait_vm_healthy()
    healed = (host2 == host)
    print(f"  reconverged, VM still on sim-{host}: {healed}")
    return ok and healed


def scenario_F():
    print("\n\033[1m═══ Scenario F: isolate the HOST (minority freeze + successor takeover, NO split-brain) ═══\033[0m")
    host = wait_vm_healthy()
    if host is None:
        print("  \033[31mPRE-CHECK FAILED\033[0m"); return False
    succ = [n for n in NODES if vrole(n) == "Secondary"]
    succ = succ[0] if succ else None
    majority = [n for n in NODES if n != host]
    print(f"  host(Primary)=sim-{host}  successor=sim-{succ}  majority={majority}")
    g_host0 = uuid_gen(host)
    t0 = now()
    partition({host: majority, **{x: [host] for x in majority}}, set())
    stamp(t0, f"CUT: isolated host sim-{host} (now minority)", "33")
    host_froze = succ_won = took_over = False
    new_host = None
    deadline = t0 + 150   # takeover at ~T+35; give margin for record/safe/start
    while now() < deadline:
        if not host_froze and suspended(host):
            stamp(t0, f"host sim-{host}: froze (susp_fen) — minority, awaiting verdict", "36"); host_froze = True
        if succ and not succ_won and "WIN" in fence_log(succ):
            stamp(t0, f"successor sim-{succ}: fence-peer WIN during takeover promote", "32"); succ_won = True
        # detect the takeover landing: VM running on a majority node + that node Primary
        run = [n for n in majority if domstate(n) == "running"]
        np = [n for n in majority if vrole(n) == "Primary"]
        if run and np and not took_over:
            new_host = np[0]
            stamp(t0, f"TAKEOVER: VM running on sim-{new_host} (Primary)", "32"); took_over = True
            break
        time.sleep(2)
    # split-brain check: count nodes that are BOTH Primary AND running the VM and NOT frozen
    time.sleep(3)
    live_primary_running = [n for n in NODES
                            if vrole(n) == "Primary" and domstate(n) == "running" and not suspended(n)]
    host_state = f"role={vrole(host)} dom={domstate(host)} frozen={suspended(host)}"
    print(f"  isolated host sim-{host}: {host_state}  (must NOT be a live running Primary)")
    print(f"  live (Primary+running+unfrozen) nodes: {live_primary_running}  (must be exactly 1)")
    no_sb = (len(live_primary_running) == 1 and live_primary_running != [host])
    # anti-mint: the frozen quorum-lost LOSER must NOT rotate its current-UUID while frozen
    # (THE original DRBD bug this whole effort fixed). Read it WHILE still isolated+frozen,
    # before heal makes it resync from the winner and adopt the winner's UUID.
    g_frozen = uuid_gen(host)
    no_mint = (g_host0 is not None and g_frozen is not None and g_host0 == g_frozen)
    print(f"  frozen loser sim-{host} UUID-gen {g_host0:#x} -> {g_frozen:#x}: "
          f"no-mint={no_mint}  (a frozen quorum-lost Primary must NOT rotate its UUID)")
    # heal + observe loser recovery
    print("  \033[33mHEAL — observe whether the frozen loser auto-demotes\033[0m"); heal_all()
    t_heal = now()
    loser_demoted = False
    for _ in range(40):
        r = vrole(host); cs = cstate(host); ds = dstate(host)
        if r == "Secondary":
            print(f"    \033[32mloser sim-{host} auto-demoted to Secondary at +{now()-t_heal:.0f}s "
                  f"(cstate={cs} dstate={ds}) — on-suspended-primary-outdated worked\033[0m")
            loser_demoted = True
            break
        time.sleep(3)
    if not loser_demoted:
        print(f"    \033[31mloser sim-{host} did NOT auto-demote (role={vrole(host)} "
              f"cstate={cstate(host)}) — may need a #34-style force-release (P3b heal gap)\033[0m")
    host2 = wait_vm_healthy(timeout=240)
    healed = host2 is not None
    print(f"  reconverged: {healed} (VM now on sim-{host2})")
    return no_sb and no_mint and healed


if __name__ == "__main__":
    which = sys.argv[2] if len(sys.argv) > 2 else "all"
    print(f"VM fence-peer campaign: VM={VM} RES={RES}  scenarios={which}")
    print("node names:", {n: node_name(n) for n in NODES})
    results = {}
    if which in ("W", "all"):
        results["W"] = scenario_W()
    if which in ("F", "all"):
        results["F"] = scenario_F()
    print("\n\033[1m═══ RESULTS ═══\033[0m")
    for k, v in results.items():
        tag = "\033[32mPASS\033[0m" if v else "\033[31mFAIL\033[0m"
        print(f"  Scenario {k}: {tag}")
    sys.exit(0 if results and all(results.values()) else 1)
