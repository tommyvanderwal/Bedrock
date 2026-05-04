#!/usr/bin/env python3
"""20 runs of profile c02 (stock 20/20 scenario) against current cluster image."""
import csv, os, re, shlex, subprocess, time
from datetime import datetime
from pathlib import Path

ENDPOINTS = ["192.168.2.189", "192.168.2.190", "192.168.2.191", "192.168.2.192"]
REPRO = Path(__file__).resolve().parent / "reproduce-leak.sh"
OUTDIR = Path(__file__).resolve().parent / "sweep-results"
OUTDIR.mkdir(parents=True, exist_ok=True)
STAMP = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
CSV_PATH = OUTDIR / f"sweep-4node-c02-beta1-20x-{STAMP}.csv"

HOT_RE = re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE = re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASE_RE = re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

VARIANT = {
    "id": "c02",
    "hot": 16,
    "writers": 36,
    "payload": 16,
    "kill": 0.60,
    "rounds": 2,
    "settle": 10,
}
REPEATS = 20
MAX_INFRA_RETRIES = 3


def restart_victim(ip: str) -> None:
    subprocess.run(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            f"root@{ip}",
            "systemctl restart rustfs; sleep 1; systemctl is-active rustfs >/dev/null",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def wait_cluster_ready(profile: str = "rustfs", timeout_s: int = 240) -> bool:
    """EC cluster may briefly return 503 on one peer after coordinated restarts."""
    need = max(1, len(ENDPOINTS) - 1)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok_n = 0
        for ep in ENDPOINTS:
            cmd = (
                f"timeout 12 aws --profile {shlex.quote(profile)} "
                f"--endpoint-url http://{ep}:9000 "
                f"--cli-read-timeout 5 --cli-connect-timeout 3 "
                "s3api list-buckets >/dev/null 2>&1"
            )
            if subprocess.run(["bash", "-lc", cmd], capture_output=True).returncode == 0:
                ok_n += 1
        if ok_n >= need:
            return True
        time.sleep(6)
    return False


def cleanup_bucket(bucket: str, profile: str = "rustfs") -> None:
    for ep in ENDPOINTS:
        cmd = (
            f"timeout 25 aws --profile {shlex.quote(profile)} "
            f"--endpoint-url http://{ep}:9000 "
            f"s3 rb s3://{bucket} --force >/dev/null 2>&1"
        )
        if subprocess.run(["bash", "-lc", cmd], capture_output=True).returncode == 0:
            return


def is_infra_failure(out: str, rc: int, base_fail: int, hot_fail: int, cold_fail: int) -> bool:
    if rc in (255,):
        return True
    if "populate_put_one exhausted retries" in out:
        return True
    if "No space left on device" in out or "Disk full" in out:
        return True
    if "baseline failed -- aborting" in out:
        return True
    if rc != 0 and hot_fail == -1 and cold_fail == -1 and base_fail == -1:
        return True
    return False


def run_one(iter_idx: int, rep: int) -> dict:
    victim_idx = (iter_idx - 1) % len(ENDPOINTS)
    bucket = f"leak-c02-beta1-{rep}-{iter_idx}"
    env = os.environ.copy()
    env.update(
        {
            "ENDPOINTS_STR": " ".join(ENDPOINTS),
            "VICTIM_IDX": str(victim_idx),
            "HOT_KEYS": str(VARIANT["hot"]),
            "WRITERS_PER_KEY": str(VARIANT["writers"]),
            "COLD_KEYS": "8",
            "READ_ROUNDS": str(VARIANT["rounds"]),
            "PAYLOAD_BYTES": str(VARIANT["payload"] * 1024 * 1024),
            "KILL_DELAY": str(VARIANT["kill"]),
            "SETTLE": str(VARIANT["settle"]),
            "READ_TIMEOUT": "9",
            "READ_TIMEOUT_GRACE": "4",
            "PUT_TIMEOUT": "120",
            "POPULATE_PARALLEL": "6",
            "POST_POPULATE_SETTLE": "8",
            # Full-cluster RESET once per suite: 20× stop/start cycles reliably
            # return 503 on CreateBucket (erasure re-form) and invalidate the run.
            "RESET": "1" if rep == 1 else "0",
            "RESET_WAIT": "240" if rep == 1 else "60",
            "HOT_FAIL_FAST_EXIT": "3",
            "BUCKET": bucket,
            "PROFILE": "rustfs",
            "STORAGE_CLASS": "REDUCED_REDUNDANCY",
        }
    )
    t0 = time.time()
    proc = subprocess.run(
        ["bash", str(REPRO)],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    dt = round(time.time() - t0, 2)
    out = proc.stdout + "\n" + proc.stderr
    mh, mc, mb = HOT_RE.search(out), COLD_RE.search(out), BASE_RE.search(out)
    hot_fail = int(mh.group(1)) if mh else -1
    cold_fail = int(mc.group(1)) if mc else -1
    base_fail = int(mb.group(2)) if mb else -1
    strict = int(hot_fail > 0 and cold_fail == 0)
    cleanup_bucket(bucket, profile=env["PROFILE"])
    restart_victim(ENDPOINTS[victim_idx])
    raw_tail = "\n".join(out.strip().splitlines()[-14:])
    return {
        "iter": iter_idx,
        "rep": rep,
        "variant_id": VARIANT["id"],
        "victim_idx": victim_idx,
        "hot_keys": VARIANT["hot"],
        "writers_per_key": VARIANT["writers"],
        "payload_mib": VARIANT["payload"],
        "kill_delay_s": VARIANT["kill"],
        "duration_s": dt,
        "exit_code": proc.returncode,
        "baseline_fail": base_fail,
        "hot_fail": hot_fail,
        "cold_fail": cold_fail,
        "reproduced_strict": strict,
        "raw_tail": raw_tail,
    }


def main() -> None:
    print(
        "START c02 x20 (RESET=1 on rep 1 only; rep 2..20 RESET=0) "
        "RustFS image on nodes should be docker.io/rustfs/rustfs:1.0.0-beta.1",
        flush=True,
    )
    fields = [
        "iter",
        "rep",
        "variant_id",
        "victim_idx",
        "hot_keys",
        "writers_per_key",
        "payload_mib",
        "kill_delay_s",
        "duration_s",
        "exit_code",
        "baseline_fail",
        "hot_fail",
        "cold_fail",
        "reproduced_strict",
        "raw_tail",
    ]
    rows = []
    strict_total = 0
    for rep in range(1, REPEATS + 1):
        row = None
        for _ in range(MAX_INFRA_RETRIES):
            if not wait_cluster_ready():
                time.sleep(8)
                continue
            try:
                row = run_one(rep, rep)
            except subprocess.TimeoutExpired:
                row = {
                    "iter": rep,
                    "rep": rep,
                    "variant_id": VARIANT["id"],
                    "victim_idx": (rep - 1) % 4,
                    "hot_keys": VARIANT["hot"],
                    "writers_per_key": VARIANT["writers"],
                    "payload_mib": VARIANT["payload"],
                    "kill_delay_s": VARIANT["kill"],
                    "duration_s": 900,
                    "exit_code": 124,
                    "baseline_fail": -1,
                    "hot_fail": -1,
                    "cold_fail": -1,
                    "reproduced_strict": 0,
                    "raw_tail": "timeout",
                }
            if is_infra_failure(
                row.get("raw_tail", ""),
                row["exit_code"],
                row["baseline_fail"],
                row["hot_fail"],
                row["cold_fail"],
            ):
                time.sleep(8)
                continue
            break
        assert row is not None
        rows.append(row)
        strict_total += row["reproduced_strict"]
        print(
            f"[{rep}/{REPEATS}] v={row['victim_idx']} hotf={row['hot_fail']} coldf={row['cold_fail']} "
            f"strict_total={strict_total} rc={row['exit_code']} dur={row['duration_s']}s",
            flush=True,
        )

    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    bad = sum(
        1
        for r in rows
        if r["exit_code"] != 0 or r["hot_fail"] == -1 or r["cold_fail"] == -1
    )
    print(f"DONE strict={strict_total}/{REPEATS} bad={bad} csv={CSV_PATH}", flush=True)


if __name__ == "__main__":
    main()
