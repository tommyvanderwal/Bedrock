"""Helpers shared by RustFS leak sweep drivers.

- ``run_repro_script``: runs reproduce-leak.sh with stdout/stderr on disk instead of
  subprocess PIPEs (avoids deadlock when the script is chatty under load).
- ``wait_min_nodes_ready``: configurable S3 readiness polling.

Environment (optional, read by callers):
  SWEEP_REPRO_TIMEOUT — seconds for each reproduce subprocess (default 600).
  SWEEP_PUT_TIMEOUT — PUT_TIMEOUT passed into reproduce-leak.sh (default 90).
  SWEEP_CLUSTER_WAIT_S — max seconds waiting before starting an iteration (default 240).
  SWEEP_POST_RESTART_WAIT_S — max seconds waiting after victim restart (default 90).
"""
from __future__ import annotations

import os
from typing import Optional
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def repro_timeout_s() -> int:
    return int(os.environ.get("SWEEP_REPRO_TIMEOUT", "600"))


def put_timeout_s() -> str:
    return os.environ.get("SWEEP_PUT_TIMEOUT", "90")


def aws_list_buckets_ok(
    ep: str,
    profile: str,
    outer_timeout: int = 12,
    read_t: int = 5,
    connect_t: int = 3,
) -> bool:
    cmd = (
        f"timeout {outer_timeout} aws --profile {shlex.quote(profile)} "
        f"--endpoint-url http://{ep}:9000 "
        f"--cli-read-timeout {read_t} --cli-connect-timeout {connect_t} "
        "s3api list-buckets >/dev/null 2>&1"
    )
    return subprocess.run(["bash", "-lc", cmd], capture_output=True).returncode == 0


def wait_min_nodes_ready(
    endpoints: list[str],
    profile: str = "rustfs",
    min_ok: Optional[int] = None,
    timeout_s: int = 240,
    poll_s: float = 3.0,
    *,
    aws_outer_timeout: int = 12,
    aws_read_t: int = 5,
    aws_connect_t: int = 3,
) -> bool:
    if min_ok is None:
        min_ok = len(endpoints)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok_n = sum(
            1
            for ep in endpoints
            if aws_list_buckets_ok(ep, profile, aws_outer_timeout, aws_read_t, aws_connect_t)
        )
        if ok_n >= min_ok:
            return True
        time.sleep(poll_s)
    return False


def run_repro_script(
    repro_path: Path, env: dict[str, str], timeout_s: Optional[int] = None
) -> tuple[int, str]:
    """Run reproduce-leak.sh; return (exit_code, combined stdout+stderr text).

    On wall-clock timeout, returns (124, partial captured output).
    """
    if timeout_s is None:
        timeout_s = repro_timeout_s()
    td = tempfile.mkdtemp(prefix="sweep-repro-")
    outp = Path(td) / "stdout.txt"
    errp = Path(td) / "stderr.txt"
    try:
        with open(outp, "w", encoding="utf-8", errors="replace") as fo, open(
            errp, "w", encoding="utf-8", errors="replace"
        ) as fe:
            proc = subprocess.run(
                ["bash", str(repro_path)],
                env=env,
                stdout=fo,
                stderr=fe,
                timeout=timeout_s,
            )
        combined = outp.read_text(encoding="utf-8", errors="replace") + "\n" + errp.read_text(
            encoding="utf-8", errors="replace"
        )
        return proc.returncode, combined
    except subprocess.TimeoutExpired:
        combined = ""
        try:
            combined = outp.read_text(encoding="utf-8", errors="replace") + "\n" + errp.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            pass
        return 124, combined if combined.strip() else "timeout (no output captured)\n"
    finally:
        shutil.rmtree(td, ignore_errors=True)
