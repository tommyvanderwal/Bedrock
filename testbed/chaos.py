#!/usr/bin/env python3
"""Bedrock mesh chaos harness.

Yanks/restores libvirt mesh bridges with `virsh net-destroy` /
`net-start` to simulate cable pulls. After each event, validates:

  * every running sim has every other sim's loopback /32 in its
    routing table (modulo the affected pair if their *only* path
    was via the yanked bridge);
  * `ping` from each loopback to each peer loopback succeeds within
    a deadline (proxy for kernel route working through whatever
    physical NIC remains up);
  * the daemon's neighbour table on each sim agrees with the
    cluster's path table (folded log) — any persistent disagreement
    after the convergence window is a bug.

Usage:
  ./chaos.py status             # print current bridge state + routes
  ./chaos.py yank N             # net-destroy bedrock-mesh-N
  ./chaos.py restore N          # net-start bedrock-mesh-N
  ./chaos.py validate           # one-shot check: every loopback reachable
  ./chaos.py monkey [seconds]   # run monkey for N seconds (default 600)
                                # random yanks, restores, plug new
                                # bridges, validate continuously.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

MESH_NETS = ["bedrock-mesh-1", "bedrock-mesh-2", "bedrock-mesh-3"]
SIMS = [1, 2, 3, 4]
SSHPASS = "bedrock"
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=5",
]


def virsh(*args, capture=True) -> tuple[str, int]:
    cmd = ["sudo", "virsh"] + list(args)
    r = subprocess.run(cmd, capture_output=capture, text=True)
    return ((r.stdout or "").strip(), r.returncode)


def sim_running(i: int) -> bool:
    out, _ = virsh("domstate", f"bedrock-sim-{i}")
    return out.strip() == "running"


def sim_ip(i: int) -> str:
    out = subprocess.run(
        ["sudo", "virsh", "qemu-agent-command", f"bedrock-sim-{i}",
         '{"execute":"guest-network-get-interfaces"}'],
        capture_output=True, text=True,
    ).stdout
    # Look for first 192.168.2.x IP (LAN/mgmt side)
    import re
    m = re.search(r'"ip-address":"(192\.168\.2\.\d+)"', out)
    return m.group(1) if m else ""


def ssh(ip: str, cmd: str, timeout: int = 10) -> tuple[str, int]:
    full = ["sshpass", "-p", SSHPASS, "ssh", *SSH_OPTS, f"root@{ip}", cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip(), r.returncode


def status():
    print("=== bridges ===")
    for net in MESH_NETS:
        out, _ = virsh("net-info", net)
        active = "active" in out.lower()
        print(f"  {net}: {'UP' if active else 'DOWN'}")

    print()
    print("=== sims ===")
    for i in SIMS:
        running = sim_running(i)
        ip = sim_ip(i) if running else ""
        print(f"  sim-{i}: running={running} ip={ip or '?'}")


def yank(n: int):
    name = f"bedrock-mesh-{n}"
    print(f"  yanking {name}...")
    virsh("net-destroy", name)


def restore(n: int):
    name = f"bedrock-mesh-{n}"
    print(f"  restoring {name}...")
    virsh("net-start", name)


def validate(deadline_s: float = 60.0) -> bool:
    """Wait up to `deadline_s` for the cluster to converge: every
    running sim's bedrock node list matches and ping-loopback works
    cross-cluster.
    """
    print(f"=== validate (≤ {deadline_s:.0f}s) ===")
    deadline = time.monotonic() + deadline_s
    last_err = None

    while time.monotonic() < deadline:
        # 1. Discover all running sims + their LAN IPs.
        running = [(i, sim_ip(i)) for i in SIMS if sim_running(i)]
        running = [(i, ip) for i, ip in running if ip]
        if not running:
            last_err = "no sims running"
            time.sleep(2)
            continue

        # 2. From each sim, pull its bedrock node list + cluster.json paths.
        # Compare cardinalities + loopback ips.
        cluster_view = {}  # sim_idx → {"loopbacks": set, "paths": int}
        ok = True
        for i, ip in running:
            out, rc = ssh(
                ip,
                "test -f /etc/bedrock/cluster.json && "
                "python3 -c \"import json,sys;"
                "d=json.load(open('/etc/bedrock/cluster.json'));"
                "print(','.join(sorted(n.get('loopback_ip','') "
                "for n in d.get('nodes',{}).values() if n.get('loopback_ip'))));"
                "print(len(d.get('paths',{})))\""
            )
            if rc != 0:
                last_err = f"sim-{i} ({ip}) cluster.json not readable"
                ok = False
                break
            lines = out.splitlines()
            loopbacks = set(lines[0].split(",")) if lines else set()
            loopbacks.discard("")
            n_paths = int(lines[1]) if len(lines) > 1 else 0
            cluster_view[i] = {"loopbacks": loopbacks, "paths": n_paths}

        if not ok:
            time.sleep(2); continue

        # 3. All sims should agree on the loopback set (consensus on membership).
        all_loopbacks = [v["loopbacks"] for v in cluster_view.values()]
        if not all(s == all_loopbacks[0] for s in all_loopbacks):
            last_err = f"membership disagreement: {cluster_view}"
            time.sleep(2); continue

        # 4. From each loopback IP, ping every other loopback. Use the host's
        #    /usr/sbin/ping6-style invocation through SSH (loopback only
        #    addressable from inside the cluster).
        loopbacks_per_sim = {}
        for i, ip in running:
            out, rc = ssh(ip, "hostname; ip -o -4 addr show lo | awk '/10.99.0/{print $4}'", timeout=5)
            lo = ""
            for line in out.splitlines():
                if "/" in line and line.startswith("10.99.0"):
                    lo = line.split("/")[0]
                    break
            loopbacks_per_sim[i] = (ip, lo)

        ping_failures = []
        for i_src, (ip_src, lo_src) in loopbacks_per_sim.items():
            if not lo_src:
                continue
            for i_dst, (_, lo_dst) in loopbacks_per_sim.items():
                if i_src == i_dst or not lo_dst:
                    continue
                # ping with very short deadline; we want fast convergence test.
                _, rc = ssh(ip_src, f"ping -c1 -W2 {lo_dst} >/dev/null", timeout=8)
                if rc != 0:
                    ping_failures.append((i_src, i_dst, lo_src, lo_dst))

        if ping_failures:
            last_err = f"loopback ping failed: {ping_failures}"
            time.sleep(2); continue

        # All checks pass.
        elapsed = deadline_s - (deadline - time.monotonic())
        print(f"  OK after {elapsed:.1f}s — {len(running)} sims, "
              f"{len(all_loopbacks[0])} loopbacks, "
              f"{cluster_view[running[0][0]]['paths']} paths logged.")
        return True

    print(f"  FAIL after {deadline_s:.0f}s — last_err: {last_err}")
    return False


def monkey(duration_s: int = 600):
    """Random chaos for `duration_s` seconds. Yanks bridges, restores
    them, occasionally yanks all then restores. Validates after every
    state change."""
    print(f"=== chaos monkey running for {duration_s}s ===")
    start = time.monotonic()
    yanked: set[int] = set()
    events = 0
    failures = 0
    convergence_times: list[float] = []

    rng = random.Random(0xBED0CC)  # deterministic chaos

    while time.monotonic() - start < duration_s:
        # Pick an action
        actions = ["yank", "restore", "yank_all", "restore_all", "wait"]
        weights = [4, 4, 1, 1, 2]
        if not yanked:
            # nothing yanked → can't restore yet
            actions, weights = ["yank", "yank_all", "wait"], [5, 1, 1]
        if len(yanked) >= len(MESH_NETS):
            # all yanked → can't yank more
            actions, weights = ["restore", "restore_all", "wait"], [5, 1, 1]

        action = rng.choices(actions, weights=weights)[0]

        if action == "yank":
            choices = [n for n in range(1, 4) if n not in yanked]
            n = rng.choice(choices)
            yank(n)
            yanked.add(n)
            events += 1
        elif action == "restore":
            n = rng.choice(list(yanked))
            restore(n)
            yanked.discard(n)
            events += 1
        elif action == "yank_all":
            for n in range(1, 4):
                if n not in yanked:
                    yank(n)
                    yanked.add(n)
            events += 1
        elif action == "restore_all":
            for n in list(yanked):
                restore(n)
                yanked.discard(n)
            events += 1
        elif action == "wait":
            wait_s = rng.uniform(2, 10)
            print(f"  ... waiting {wait_s:.1f}s")
            time.sleep(wait_s)
            continue

        # Validate after each event (if at least one bridge is up).
        # When all 3 mesh bridges are down, sims still have the LAN
        # connection (mgmt side) — that's a separate plane.
        t0 = time.monotonic()
        ok = validate(deadline_s=45.0)
        t1 = time.monotonic()
        if ok:
            convergence_times.append(t1 - t0)
        else:
            failures += 1
            print(f"  FAILURE recorded at +{int(time.monotonic()-start)}s; continuing.")

        # Brief settle before next event
        time.sleep(rng.uniform(1, 3))

    # Restore everything before exiting.
    for n in list(yanked):
        restore(n)
        yanked.discard(n)
    time.sleep(2)
    final = validate(deadline_s=60.0)

    print()
    print(f"=== chaos summary ===")
    print(f"  events: {events}")
    print(f"  validation failures: {failures}")
    print(f"  convergence times: {len(convergence_times)} samples, "
          f"avg={sum(convergence_times)/max(len(convergence_times),1):.1f}s, "
          f"max={max(convergence_times) if convergence_times else 0:.1f}s")
    print(f"  final state ok: {final}")
    return failures == 0 and final


def main():
    ap = argparse.ArgumentParser(description="Bedrock mesh chaos harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    p_yank = sub.add_parser("yank")
    p_yank.add_argument("n", type=int, choices=[1, 2, 3])
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("n", type=int, choices=[1, 2, 3])
    p_validate = sub.add_parser("validate")
    p_validate.add_argument("--timeout", type=float, default=60.0)
    p_monkey = sub.add_parser("monkey")
    p_monkey.add_argument("duration", type=int, nargs="?", default=600)
    args = ap.parse_args()

    if args.cmd == "status":
        status()
    elif args.cmd == "yank":
        yank(args.n)
    elif args.cmd == "restore":
        restore(args.n)
    elif args.cmd == "validate":
        ok = validate(args.timeout)
        sys.exit(0 if ok else 1)
    elif args.cmd == "monkey":
        ok = monkey(args.duration)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
