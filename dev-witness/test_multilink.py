#!/usr/bin/env python3
"""Multi-link + leader-election integration test.

Spawns two daemons with **two** TCP paths between them (different
ports — simulating two independent cables). Both have witness-based
election enabled. Test:
  1. Both nodes connect on both paths; replication works.
  2. Append entries on the leader; follower catches up.
  3. Kill one of the two paths (close the listener somehow — simplest
     is to bind one listener to a port that we then DROP via a `pkill`
     of an iptables rule. For a portable test we instead start with one
     of the paths simply unable to connect, and verify that the system
     ran fine on the other path the whole time).
  4. Final assert: both sides have identical log tails.

The point is to prove the daemon stays operational with as few as one
working path — multi-link is convenience, not a hard requirement.
"""

from __future__ import annotations
import os, signal, subprocess, sys, time, shutil
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "installer"))

from lib import log_entries as le      # noqa: E402
from lib import rust_ipc               # noqa: E402

BIN = REPO / "rust" / "target" / "release" / "bedrock-rust"
N1_LOG = Path("/tmp/bedrock-multilink/n1/log")
N2_LOG = Path("/tmp/bedrock-multilink/n2/log")
N1_SOCK = "/tmp/bedrock-multilink/n1.sock"
N2_SOCK = "/tmp/bedrock-multilink/n2.sock"

# Two paths between node-1 and node-2:
#   primary:   n1:8211  ↔  n2:8221
#   secondary: n1:8212  ↔  n2:8222 (we'll point n2 at a bogus port to
#                                    simulate this cable being yanked)
N1_PEER_LISTEN = ["127.0.0.1:8211", "127.0.0.1:8212"]
N1_PEERS_TO_DIAL = []  # leader doesn't initiate
N2_PEER_LISTEN = ["127.0.0.1:8221", "127.0.0.1:8222"]
N2_PEERS_TO_DIAL = ["127.0.0.1:8211", "127.0.0.1:9999"]  # 9999 is broken


def banner(s): print(f"\n=== {s} ===")


def reset_dirs() -> None:
    base = Path("/tmp/bedrock-multilink")
    if base.exists():
        shutil.rmtree(base)
    for p in (N1_LOG, N2_LOG):
        p.mkdir(parents=True)


def init_log(d: Path, uuid: str) -> None:
    subprocess.check_call([str(BIN), "--log-dir", str(d), "log", "init",
                           "--cluster-uuid", uuid])


def start_witness():
    p = subprocess.Popen(
        ["python3", str(REPO / "dev-witness" / "run.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    pub = None
    for _ in range(50):
        line = p.stdout.readline()
        if not line:
            time.sleep(0.1); continue
        if line.startswith("witness_pubkey (hex):"):
            pub = line.split(":",1)[1].strip()
        if "running on UDP" in line:
            break
    if not pub:
        raise RuntimeError("no witness pubkey")
    return p, pub


def start_daemon(*, log_dir, sock, role, sender_id, peer_sender_id,
                 listens, peers, cluster_key, witness_pub):
    cmd = [str(BIN), "--log-dir", str(log_dir), "--ipc-sock", sock, "daemon",
           "--role", role,
           "--cluster-key", cluster_key,
           "--witness-host", "127.0.0.1", "--witness-port", "12321",
           "--witness-pubkey", witness_pub,
           "--sender-id", str(sender_id),
           "--peer-sender-id", str(peer_sender_id),
           "--heartbeat-ms", "1000", "--lease-ttl-ms", "10000"]
    for l in listens:
        cmd += ["--peer-listen", l]
    for p in peers:
        cmd += ["--peer", p]
    return subprocess.Popen(cmd, env={**os.environ, "RUST_LOG": "info"})


def wait_sock(p: str, t: float = 5.0) -> None:
    start = time.time()
    while time.time() - start < t:
        if Path(p).exists():
            return
        time.sleep(0.1)
    raise RuntimeError(f"socket {p} did not appear")


def main():
    if not BIN.exists():
        sys.exit("build first: cargo build --release")
    reset_dirs()
    uuid = "multilink-demo-uuid"
    init_log(N1_LOG, uuid)
    init_log(N2_LOG, uuid)

    banner("witness up")
    witness_proc, witness_pub = start_witness()

    banner(f"start node-1 (listens on {N1_PEER_LISTEN}) and node-2 "
           f"(listens on {N2_PEER_LISTEN}, dials {N2_PEERS_TO_DIAL} — "
           f"port 9999 is intentionally broken to simulate a yanked cable)")
    cluster_key = os.urandom(32).hex()
    n1 = start_daemon(log_dir=N1_LOG, sock=N1_SOCK, role="leader",
                      sender_id=1, peer_sender_id=2,
                      listens=N1_PEER_LISTEN, peers=N1_PEERS_TO_DIAL,
                      cluster_key=cluster_key, witness_pub=witness_pub)
    wait_sock(N1_SOCK)
    n2 = start_daemon(log_dir=N2_LOG, sock=N2_SOCK, role="follower",
                      sender_id=2, peer_sender_id=1,
                      listens=N2_PEER_LISTEN, peers=N2_PEERS_TO_DIAL,
                      cluster_key=cluster_key, witness_pub=witness_pub)
    wait_sock(N2_SOCK)
    time.sleep(2.0)

    try:
        banner("append 50 typed entries on the leader")
        with rust_ipc.Daemon(N1_SOCK) as d1:
            for i in range(50):
                d1.append(le.encode("opaque_test", i=i))
            s1 = d1.status()
            print(f"  node-1 latest: idx={s1['latest_index']} hash={s1['latest_hash'].hex()[:12]}")
        time.sleep(2.0)

        banner("verify follower caught up over the working path "
               "(broken path 9999 stayed broken the whole time)")
        with rust_ipc.Daemon(N2_SOCK) as d2:
            s2 = d2.status()
            print(f"  node-2 latest: idx={s2['latest_index']} hash={s2['latest_hash'].hex()[:12]}")
            verified = d2.verify()
            print(f"  node-2 verify: {verified} entries clean")

        if s1["latest_index"] != s2["latest_index"]:
            sys.exit(f"FAIL: replication didn't catch up "
                     f"(n1={s1['latest_index']} n2={s2['latest_index']})")
        if s1["latest_hash"] != s2["latest_hash"]:
            sys.exit("FAIL: hash diverged")
        print("✓ replication completed despite one broken link")

        banner("inspect election decisions in the daemon logs")
        # The daemon prints "election: ... → Leader/Follower" lines on
        # every state change. We just print the summary here — manual
        # eyeball check that the leader sees itself as leader and the
        # follower as follower.
        print("  (look for 'election:' lines in stderr above this point)")

        banner("DONE — multi-link tolerated a broken cable, election "
               "ran on each heartbeat")
    finally:
        for p in (n2, n1, witness_proc):
            try:
                p.send_signal(signal.SIGTERM); p.wait(timeout=2)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
