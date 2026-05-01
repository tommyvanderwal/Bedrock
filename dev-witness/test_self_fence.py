#!/usr/bin/env python3
"""Phase 4 self-fence smoke test.

Spins up a witness + one daemon, waits for the daemon to register,
SIGTERMs the witness, and verifies the daemon enters self-fence
(exits within ttl_ms + heartbeat_ms once the witness is gone).

The daemon's stderr is captured; we expect to see the
`lease: TTL exhausted; entering self-fence` message followed by
process exit.
"""
from __future__ import annotations
import os, signal, subprocess, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "rust" / "target" / "release" / "bedrock-rust"

LOG = Path("/tmp/bedrock-fence/log")
SOCK = "/tmp/bedrock-fence/n.sock"

def main():
    if not BIN.exists():
        sys.exit("build the daemon first")
    if Path("/tmp/bedrock-fence").exists():
        import shutil; shutil.rmtree("/tmp/bedrock-fence")
    LOG.mkdir(parents=True)
    subprocess.check_call([str(BIN), "--log-dir", str(LOG), "log", "init"])

    witness = subprocess.Popen(
        ["python3", str(REPO / "dev-witness" / "run.py")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    pub = None
    for _ in range(50):
        line = witness.stdout.readline()
        if not line:
            time.sleep(0.1); continue
        if line.startswith("witness_pubkey (hex):"):
            pub = line.split(":",1)[1].strip()
        if "running on UDP" in line:
            break
    assert pub, "witness pubkey not found"

    cluster_key = os.urandom(32).hex()
    daemon = subprocess.Popen([
        str(BIN), "--log-dir", str(LOG), "--ipc-sock", SOCK, "daemon",
        "--peer-listen", "127.0.0.1:8301", "--role", "standalone",
        "--cluster-key", cluster_key,
        "--witness-host", "127.0.0.1", "--witness-port", "12321",
        "--witness-pubkey", pub,
        "--sender-id", "5",
        "--heartbeat-ms", "500", "--lease-ttl-ms", "3000",
    ], stderr=subprocess.PIPE, text=True, env={**os.environ, "RUST_LOG": "info"})

    # Wait for the daemon to do at least one successful heartbeat, then kill the witness.
    print("Waiting 2s for daemon to register at the witness...")
    time.sleep(2)
    print("Killing the witness — daemon should self-fence within ttl_ms + heartbeat_ms")
    witness.send_signal(signal.SIGTERM); witness.wait(timeout=2)

    # Daemon should exit within ttl_ms (3000) + a couple of heartbeats.
    try:
        daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        sys.exit("FAIL: daemon did not self-fence within 10s of witness loss")

    stderr = daemon.stderr.read()
    print(stderr)
    if "self-fence" not in stderr or "TTL exhausted" not in stderr:
        sys.exit("FAIL: daemon exited but didn't log self-fence")
    if daemon.returncode != 2:
        sys.exit(f"FAIL: expected exit code 2 (self-fence dev-mode), got {daemon.returncode}")
    print(f"\n✓ daemon self-fenced cleanly (exit={daemon.returncode})")
    if Path("/run/bedrock-rust.fence").exists():
        print(f"✓ fence marker written at /run/bedrock-rust.fence")

if __name__ == "__main__":
    main()
