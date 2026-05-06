#!/usr/bin/env python3
"""200-run Monte Carlo sweep: random c02-like knobs, EC:1, 4-node lab.

RESET is off by default: a single iter-1 full-cluster reset was still
leaving CreateBucket on 503 for the rest of the suite. Heal the cluster
(coordinated rustfs restart) before launching, then run with RESET=0.

Set MONTE_INITIAL_RESET=1 to run one RESET=1 on iter 1 only (240s wait).

Optional env (see sweep_common.py): SWEEP_REPRO_TIMEOUT (default 600),
SWEEP_PUT_TIMEOUT (default 90), SWEEP_CLUSTER_WAIT_S (default 240),
SWEEP_POST_RESTART_WAIT_S (default 90).

Usage:
  cd installer/lib/rustfs-patches && PYTHONUNBUFFERED=1 python3 sweep_4node_monte200.py
"""
from __future__ import annotations

import csv
import os
import random
import re
import shlex
import subprocess
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sweep_common import (
    put_timeout_s,
    repro_timeout_s,
    run_repro_script,
    wait_min_nodes_ready,
)

ENDPOINTS = ["192.168.2.189", "192.168.2.190", "192.168.2.191", "192.168.2.192"]
REPRO = Path(__file__).resolve().parent / "reproduce-leak.sh"
OUTDIR = Path(__file__).resolve().parent / "sweep-results"
OUTDIR.mkdir(parents=True, exist_ok=True)
# S3 bucket names: lowercase letters, digits, hyphen only (no uppercase T/Z).
STAMP = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
CSV_PATH = OUTDIR / f"sweep-4node-monte200-{STAMP}.csv"
LOG_PATH = OUTDIR / f"sweep-4node-monte200-{STAMP}.log"

HOT_RE = re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE = re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASE_RE = re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

TOTAL = 200
MAX_INFRA_RETRIES = 4


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


def wait_cluster_ready(profile: str = "rustfs", timeout_s: int | None = None) -> bool:
    """Require every node (including the upcoming victim): populate uses VICTIM_IP."""
    if timeout_s is None:
        timeout_s = int(os.environ.get("SWEEP_CLUSTER_WAIT_S", "240"))
    return wait_min_nodes_ready(
        ENDPOINTS,
        profile=profile,
        min_ok=len(ENDPOINTS),
        timeout_s=timeout_s,
        poll_s=3.0,
    )


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


def sample_variant(rng: random.Random) -> dict:
    """Random knob set around the historical high-hit region."""
    hot = rng.choice([10, 12, 14, 14, 16, 16, 18])
    writers = rng.choice([24, 28, 32, 32, 36, 36, 40])
    payload = rng.choice([8, 12, 16, 16, 20, 24])
    kill = round(rng.uniform(0.38, 0.88), 2)
    rounds = rng.choice([1, 2, 2, 2, 3])
    settle = rng.choice([8, 10, 10, 12, 14])
    pop_par = rng.choice([4, 6, 6, 8])
    post_pop = rng.choice([4, 6, 8, 8, 10])
    vid = f"h{hot}w{writers}p{payload}k{kill:.2f}r{rounds}s{settle}"
    return {
        "id": vid,
        "hot": hot,
        "writers": writers,
        "payload": payload,
        "kill": kill,
        "rounds": rounds,
        "settle": settle,
        "pop_par": pop_par,
        "post_pop": post_pop,
    }


def run_one(gi: int, v: dict) -> dict:
    victim_idx = (gi - 1) % len(ENDPOINTS)
    bucket = f"leak-monte-{STAMP}-{gi}"
    env = os.environ.copy()
    env.update(
        {
            "ENDPOINTS_STR": " ".join(ENDPOINTS),
            "VICTIM_IDX": str(victim_idx),
            "HOT_KEYS": str(v["hot"]),
            "WRITERS_PER_KEY": str(v["writers"]),
            "COLD_KEYS": "8",
            "READ_ROUNDS": str(v["rounds"]),
            "PAYLOAD_BYTES": str(v["payload"] * 1024 * 1024),
            "KILL_DELAY": str(v["kill"]),
            "SETTLE": str(v["settle"]),
            "READ_TIMEOUT": "9",
            "READ_TIMEOUT_GRACE": "4",
            "PUT_TIMEOUT": put_timeout_s(),
            "POPULATE_PARALLEL": str(v["pop_par"]),
            "POST_POPULATE_SETTLE": str(v["post_pop"]),
            "RESET": (
                "1"
                if (gi == 1 and os.environ.get("MONTE_INITIAL_RESET", "") == "1")
                else "0"
            ),
            "RESET_WAIT": (
                "240"
                if (gi == 1 and os.environ.get("MONTE_INITIAL_RESET", "") == "1")
                else "60"
            ),
            "HOT_FAIL_FAST_EXIT": "3",
            "BUCKET": bucket,
            "PROFILE": "rustfs",
            "STORAGE_CLASS": "REDUCED_REDUNDANCY",
        }
    )
    t0 = time.time()
    rc, out = run_repro_script(REPRO, env, repro_timeout_s())
    dt = round(time.time() - t0, 2)
    mh, mc, mb = HOT_RE.search(out), COLD_RE.search(out), BASE_RE.search(out)
    hot_fail = int(mh.group(1)) if mh else -1
    cold_fail = int(mc.group(1)) if mc else -1
    base_fail = int(mb.group(2)) if mb else -1
    strict = int(hot_fail > 0 and cold_fail == 0)
    anyh = int(hot_fail > 0)
    cleanup_bucket(bucket, profile=env["PROFILE"])
    raw_tail = "\n".join(out.strip().splitlines()[-14:])
    return {
        "iter": gi,
        "variant_id": v["id"],
        "victim_idx": victim_idx,
        "hot_keys": v["hot"],
        "writers_per_key": v["writers"],
        "payload_mib": v["payload"],
        "kill_delay_s": v["kill"],
        "read_rounds": v["rounds"],
        "settle_s": v["settle"],
        "populate_parallel": v["pop_par"],
        "post_populate_settle": v["post_pop"],
        "duration_s": dt,
        "exit_code": rc,
        "baseline_fail": base_fail,
        "hot_fail": hot_fail,
        "cold_fail": cold_fail,
        "reproduced_strict": strict,
        "reproduced_hot_any": anyh,
        "raw_tail": raw_tail,
    }


def hotspot_key(row: dict) -> tuple:
    """0.05s kill bins so unrelated floats still aggregate."""
    kill_b = round(float(row["kill_delay_s"]) / 0.05) * 0.05
    return (
        int(row["hot_keys"]),
        int(row["writers_per_key"]),
        int(row["payload_mib"]),
        kill_b,
        int(row["read_rounds"]),
    )


def hotspot_key_hwp(row: dict) -> tuple:
    return (
        int(row["hot_keys"]),
        int(row["writers_per_key"]),
        int(row["payload_mib"]),
    )


def main() -> None:
    seed = int(os.environ.get("MONTE_SEED", str(int(time.time()))))
    rng = random.Random(seed)
    fields = [
        "iter",
        "variant_id",
        "victim_idx",
        "hot_keys",
        "writers_per_key",
        "payload_mib",
        "kill_delay_s",
        "read_rounds",
        "settle_s",
        "populate_parallel",
        "post_populate_settle",
        "duration_s",
        "exit_code",
        "baseline_fail",
        "hot_fail",
        "cold_fail",
        "reproduced_strict",
        "reproduced_hot_any",
        "raw_tail",
    ]

    agg = defaultdict(lambda: {"n": 0, "strict": 0, "any": 0})
    agg_hwp = defaultdict(lambda: {"n": 0, "strict": 0, "any": 0})
    victim_agg = defaultdict(lambda: {"n": 0, "strict": 0})

    strict_total = 0
    any_total = 0
    bad = 0

    with CSV_PATH.open("w", newline="") as fc, LOG_PATH.open("w") as fl:
        w = csv.DictWriter(fc, fieldnames=fields)
        w.writeheader()
        fc.flush()
        hdr = (
            f"START monte200 seed={seed} total={TOTAL} csv={CSV_PATH} log={LOG_PATH}\n"
            f"MONTE_INITIAL_RESET={os.environ.get('MONTE_INITIAL_RESET', '')!r} "
            "(empty => RESET=0 all iters); gate=all nodes list-buckets\n"
        )
        print(hdr, flush=True)
        fl.write(hdr)
        fl.flush()

        for gi in range(1, TOTAL + 1):
            v = sample_variant(rng)
            row = None
            for _ in range(MAX_INFRA_RETRIES):
                if not wait_cluster_ready():
                    time.sleep(8)
                    continue
                row = run_one(gi, v)
                if is_infra_failure(
                    row["raw_tail"],
                    row["exit_code"],
                    row["baseline_fail"],
                    row["hot_fail"],
                    row["cold_fail"],
                ):
                    time.sleep(8)
                    continue
                break

            if row is None:
                row = {
                    "iter": gi,
                    "variant_id": v["id"],
                    "victim_idx": (gi - 1) % 4,
                    "hot_keys": v["hot"],
                    "writers_per_key": v["writers"],
                    "payload_mib": v["payload"],
                    "kill_delay_s": v["kill"],
                    "read_rounds": v["rounds"],
                    "settle_s": v["settle"],
                    "populate_parallel": v["pop_par"],
                    "post_populate_settle": v["post_pop"],
                    "duration_s": 0,
                    "exit_code": 255,
                    "baseline_fail": -1,
                    "hot_fail": -1,
                    "cold_fail": -1,
                    "reproduced_strict": 0,
                    "reproduced_hot_any": 0,
                    "raw_tail": "cluster_not_ready_after_retries",
                }

            if (
                row["exit_code"] != 0
                or row["hot_fail"] == -1
                or row["cold_fail"] == -1
            ):
                bad += 1

            w.writerow(row)
            fc.flush()

            # Only restart victim after a completed repro — restarting on every
            # infra failure (503 during bucket setup) leaves peers mid-boot and
            # the next CreateBucket fails in ~0.6s for the whole sweep.
            if not is_infra_failure(
                row["raw_tail"],
                row["exit_code"],
                row["baseline_fail"],
                row["hot_fail"],
                row["cold_fail"],
            ):
                restart_victim(ENDPOINTS[int(row["victim_idx"])])
                time.sleep(4)
                wait_min_nodes_ready(
                    ENDPOINTS,
                    profile="rustfs",
                    min_ok=len(ENDPOINTS),
                    timeout_s=int(os.environ.get("SWEEP_POST_RESTART_WAIT_S", "90")),
                    poll_s=2.0,
                )

            hk = hotspot_key(row)
            agg[hk]["n"] += 1
            agg[hk]["strict"] += int(row["reproduced_strict"])
            agg[hk]["any"] += int(row["reproduced_hot_any"])
            hk2 = hotspot_key_hwp(row)
            agg_hwp[hk2]["n"] += 1
            agg_hwp[hk2]["strict"] += int(row["reproduced_strict"])
            agg_hwp[hk2]["any"] += int(row["reproduced_hot_any"])
            vi = int(row["victim_idx"])
            victim_agg[vi]["n"] += 1
            victim_agg[vi]["strict"] += int(row["reproduced_strict"])

            strict_total += int(row["reproduced_strict"])
            any_total += int(row["reproduced_hot_any"])

            line = (
                f"[{gi}/{TOTAL}] {row['variant_id']} v={row['victim_idx']} "
                f"hotf={row['hot_fail']} coldf={row['cold_fail']} "
                f"strict_total={strict_total} any_total={any_total} "
                f"rc={row['exit_code']} dur={row['duration_s']}s bad={bad}"
            )
            print(line, flush=True)
            fl.write(line + "\n")
            fl.flush()

    # Hotspot table: (hot, writers, payload, kill, rounds) with n>=4
    ranked = []
    for k, s in agg.items():
        if s["n"] < 4:
            continue
        ranked.append((s["strict"] / s["n"], s["strict"], s["n"], s["any"], k))
    ranked.sort(reverse=True)

    summary_lines = [
        "",
        f"DONE seed={seed} iterations={TOTAL} strict_hits={strict_total} any_hits={any_total} bad_rows={bad}",
        f"CSV: {CSV_PATH}",
        f"LOG: {LOG_PATH}",
        "",
        "Per-victim strict hits (victim_idx -> .189,.190,.191,.192):",
    ]
    for vi in sorted(victim_agg.keys()):
        s = victim_agg[vi]
        rate = 100.0 * s["strict"] / s["n"] if s["n"] else 0.0
        summary_lines.append(
            f"  victim {vi}  n={s['n']}  strict={s['strict']}  ({rate:.1f}%)"
        )
    summary_lines.append("")
    summary_lines.append(
        "Hotspot combos (hot, writers, payload_MiB, kill, read_rounds) with n>=4, by strict rate:"
    )
    for rate, st, n, anyv, k in ranked[:25]:
        summary_lines.append(
            f"  {k}  n={n}  strict={st}  ({100.0 * rate:.1f}%)  any_hot={anyv}"
        )
    if not ranked:
        summary_lines.append("  (no combo reached n>=4 — widen run or lower threshold)")

    ranked2 = []
    for k, s in agg_hwp.items():
        if s["n"] < 6:
            continue
        ranked2.append((s["strict"] / s["n"], s["strict"], s["n"], s["any"], k))
    ranked2.sort(reverse=True)
    summary_lines.append("")
    summary_lines.append(
        "Coarser hotspots (hot_keys, writers, payload_MiB) with n>=6, by strict rate:"
    )
    for rate, st, n, anyv, k in ranked2[:20]:
        summary_lines.append(
            f"  {k}  n={n}  strict={st}  ({100.0 * rate:.1f}%)  any_hot={anyv}"
        )
    if not ranked2:
        summary_lines.append("  (no HWP triple reached n>=6)")

    summary = "\n".join(summary_lines)
    print(summary, flush=True)
    with LOG_PATH.open("a") as fl:
        fl.write(summary + "\n")


if __name__ == "__main__":
    main()
