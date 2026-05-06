#!/usr/bin/env python3
"""Focused residual sweep: Monte anchors + hypothesis bucket (post #2805).

Default schedule (400 iterations, RESIDUAL_SEED):
  - 100× strong anchor Monte iter 87 (3-survivor symmetric fails), victim fixed 2, micro-jitter
  - 100× strong anchor Monte iter 148, victim fixed 3, micro-jitter
  - 25× each weak anchors (Monte iters 48, 81, 133, 139), fixed victims, micro-jitter
  - 100× hypothesis-driven (kill-timing grid, long client read timeout, extra read rounds)

Env:
  RESIDUAL_SEED   seed for jitter + hypothesis picks (default 2805991)
  RESIDUAL_DRY_RUN=1  print schedule counts and exit
  MONTE_INITIAL_RESET / RESET same semantics as sweep_4node_monte200.py (iter 1 only if set)
  SWEEP_* optional tuning — see sweep_common.py (repro timeout, PUT timeout, readiness waits).

Usage:
  cd installer/lib/rustfs-patches && PYTHONUNBUFFERED=1 python3 sweep_4node_residual_focus.py
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
STAMP = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
CSV_PATH = OUTDIR / f"sweep-4node-residual-{STAMP}.csv"
LOG_PATH = OUTDIR / f"sweep-4node-residual-{STAMP}.log"

HOT_RE = re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE = re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASE_RE = re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

MAX_INFRA_RETRIES = 4

# Monte CSV anchors (validate-2805 …151017.csv): knobs + fixed VICTIM_IDX from that row.
STRONG87 = {
    "anchor": "monte87",
    "bucket": "strong",
    "victim": 2,
    "hot": 16,
    "writers": 28,
    "payload": 16,
    "kill": 0.45,
    "rounds": 3,
    "settle": 10,
    "pop_par": 6,
    "post_pop": 6,
}
STRONG148 = {
    "anchor": "monte148",
    "bucket": "strong",
    "victim": 3,
    "hot": 16,
    "writers": 24,
    "payload": 16,
    "kill": 0.57,
    "rounds": 1,
    "settle": 8,
    "pop_par": 4,
    "post_pop": 8,
}
WEAK48 = {
    "anchor": "monte48",
    "bucket": "weak",
    "victim": 3,
    "hot": 18,
    "writers": 40,
    "payload": 16,
    "kill": 0.62,
    "rounds": 3,
    "settle": 8,
    "pop_par": 6,
    "post_pop": 6,
}
WEAK81 = {
    "anchor": "monte81",
    "bucket": "weak",
    "victim": 0,
    "hot": 10,
    "writers": 28,
    "payload": 8,
    "kill": 0.49,
    "rounds": 1,
    "settle": 12,
    "pop_par": 6,
    "post_pop": 10,
}
WEAK133 = {
    "anchor": "monte133",
    "bucket": "weak",
    "victim": 0,
    "hot": 16,
    "writers": 24,
    "payload": 8,
    "kill": 0.43,
    "rounds": 3,
    "settle": 10,
    "pop_par": 8,
    "post_pop": 8,
}
WEAK139 = {
    "anchor": "monte139",
    "bucket": "weak",
    "victim": 2,
    "hot": 10,
    "writers": 24,
    "payload": 20,
    "kill": 0.47,
    "rounds": 2,
    "settle": 10,
    "pop_par": 8,
    "post_pop": 8,
}


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
    """All nodes must answer: victim handles populate PUTs."""
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


def variant_id(v: dict) -> str:
    return (
        f"h{v['hot']}w{v['writers']}p{v['payload']}"
        f"k{v['kill']:.2f}r{v['rounds']}s{v['settle']}"
    )


def micro_jitter(base: dict, rng: random.Random) -> dict:
    v = dict(base)
    dk = rng.choice([-0.04, -0.02, 0.0, 0.02, 0.04])
    v["kill"] = round(max(0.38, min(0.88, base["kill"] + dk)), 2)
    v["settle"] = max(6, min(16, base["settle"] + rng.choice([-2, 0, 2])))
    v["pop_par"] = max(4, min(8, base["pop_par"] + rng.choice([-2, 0, 2])))
    v["post_pop"] = max(5, min(12, base["post_pop"] + rng.choice([-2, 0, 2])))
    v["id"] = variant_id(v)
    return v


def build_hypothesis_specs(rng: random.Random) -> list[dict]:
    """Variants for hypothesis bucket; each dict includes hypothesis_tag and monte anchor name."""

    def tag_copy(src: dict, hypothesis_tag: str) -> dict:
        v = dict(src)
        v["hypothesis_tag"] = hypothesis_tag
        return v

    specs: list[dict] = []

    # H1: cancellation-timing pressure — tight kill grid on strong bases (34 runs)
    kills_tight = [0.41, 0.43, 0.45, 0.47, 0.49, 0.51, 0.53]
    for _ in range(34):
        src = STRONG87 if rng.random() < 0.5 else STRONG148
        v = tag_copy(src, "hypo_kill_grid")
        v["kill"] = rng.choice(kills_tight)
        v["settle"] = max(6, min(14, src["settle"] + rng.choice([-2, 0, 2])))
        v["pop_par"] = src["pop_par"]
        v["post_pop"] = src["post_pop"]
        v["id"] = variant_id(v)
        specs.append(v)

    # H2: distinguish slow-lock vs CLI timeout (33 runs)
    for _ in range(33):
        src = STRONG87 if rng.random() < 0.5 else STRONG148
        v = micro_jitter(tag_copy(src, "hypo_long_client_read"), rng)
        v["read_timeout"] = 15
        v["read_timeout_grace"] = 8
        specs.append(v)

    # H3: more read sampling / settle variants (33 runs)
    for _ in range(33):
        if rng.random() < 0.5:
            src = dict(STRONG87)
            src["rounds"] = 4
            src["settle"] = max(6, STRONG87["settle"] - 2)
        else:
            src = dict(STRONG148)
            src["rounds"] = 2
            src["settle"] = max(6, STRONG148["settle"] + rng.choice([0, 2]))
        src["hypothesis_tag"] = "hypo_extra_rounds"
        v = micro_jitter(src, rng)
        specs.append(v)

    return specs


def build_schedule(rng: random.Random) -> list[tuple[str, dict]]:
    """Ordered list of (schedule_bucket, variant_with_anchor_metadata)."""
    sched: list[tuple[str, dict]] = []

    def add_repeated(base: dict, n: int, bucket: str) -> None:
        for _ in range(n):
            v = micro_jitter(base, rng)
            v["anchor"] = base["anchor"]
            v["bucket"] = bucket
            v["victim"] = base["victim"]
            sched.append((bucket, v))

    add_repeated(STRONG87, 100, "strong")
    add_repeated(STRONG148, 100, "strong")
    add_repeated(WEAK48, 25, "weak")
    add_repeated(WEAK81, 25, "weak")
    add_repeated(WEAK133, 25, "weak")
    add_repeated(WEAK139, 25, "weak")

    for v in build_hypothesis_specs(rng):
        v["bucket"] = "hypothesis"
        sched.append(("hypothesis", v))

    return sched


def run_one(gi: int, v: dict) -> dict:
    victim_idx = int(v["victim"])
    bucket = f"leak-residual-{STAMP}-{gi}"
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
            "READ_TIMEOUT": str(v.get("read_timeout", 9)),
            "READ_TIMEOUT_GRACE": str(v.get("read_timeout_grace", 4)),
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
        "schedule_bucket": v.get("bucket", ""),
        "hypothesis_tag": v.get("hypothesis_tag", ""),
        "anchor": v.get("anchor", ""),
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
        "read_timeout": v.get("read_timeout", 9),
        "read_timeout_grace": v.get("read_timeout_grace", 4),
        "duration_s": dt,
        "exit_code": rc,
        "baseline_fail": base_fail,
        "hot_fail": hot_fail,
        "cold_fail": cold_fail,
        "reproduced_strict": strict,
        "reproduced_hot_any": anyh,
        "raw_tail": raw_tail,
    }


def main() -> None:
    seed = int(os.environ.get("RESIDUAL_SEED", "2805991"))
    rng = random.Random(seed)
    schedule = build_schedule(rng)
    total = len(schedule)

    if os.environ.get("RESIDUAL_DRY_RUN", "") == "1":
        c = defaultdict(int)
        for bucket, v in schedule:
            c[bucket] += 1
        print(f"RESIDUAL_DRY_RUN seed={seed} total={total}")
        for k in sorted(c.keys()):
            print(f"  {k}: {c[k]}")
        return

    fields = [
        "iter",
        "schedule_bucket",
        "hypothesis_tag",
        "anchor",
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
        "read_timeout",
        "read_timeout_grace",
        "duration_s",
        "exit_code",
        "baseline_fail",
        "hot_fail",
        "cold_fail",
        "reproduced_strict",
        "reproduced_hot_any",
        "raw_tail",
    ]

    by_bucket = defaultdict(lambda: {"n": 0, "strict": 0})
    by_hypo = defaultdict(lambda: {"n": 0, "strict": 0})
    strict_total = 0
    any_total = 0
    bad = 0

    hdr = (
        f"START residual_focus seed={seed} total={total} csv={CSV_PATH} log={LOG_PATH}\n"
        f"MONTE_INITIAL_RESET={os.environ.get('MONTE_INITIAL_RESET', '')!r}\n"
    )
    print(hdr, flush=True)

    with CSV_PATH.open("w", newline="") as fc, LOG_PATH.open("w") as fl:
        w = csv.DictWriter(fc, fieldnames=fields)
        w.writeheader()
        fc.flush()
        fl.write(hdr)
        fl.flush()

        for gi, (bucket, v) in enumerate(schedule, start=1):
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
                    "schedule_bucket": bucket,
                    "hypothesis_tag": v.get("hypothesis_tag", ""),
                    "anchor": v.get("anchor", ""),
                    "variant_id": v["id"],
                    "victim_idx": int(v["victim"]),
                    "hot_keys": v["hot"],
                    "writers_per_key": v["writers"],
                    "payload_mib": v["payload"],
                    "kill_delay_s": v["kill"],
                    "read_rounds": v["rounds"],
                    "settle_s": v["settle"],
                    "populate_parallel": v["pop_par"],
                    "post_populate_settle": v["post_pop"],
                    "read_timeout": v.get("read_timeout", 9),
                    "read_timeout_grace": v.get("read_timeout_grace", 4),
                    "duration_s": 0,
                    "exit_code": 255,
                    "baseline_fail": -1,
                    "hot_fail": -1,
                    "cold_fail": -1,
                    "reproduced_strict": 0,
                    "reproduced_hot_any": 0,
                    "raw_tail": "cluster_not_ready_after_retries",
                }

            if row["exit_code"] != 0 or row["hot_fail"] == -1 or row["cold_fail"] == -1:
                bad += 1

            w.writerow(row)
            fc.flush()

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

            bk = row["schedule_bucket"]
            by_bucket[bk]["n"] += 1
            by_bucket[bk]["strict"] += int(row["reproduced_strict"])
            if bk == "hypothesis" and row["hypothesis_tag"]:
                by_hypo[row["hypothesis_tag"]]["n"] += 1
                by_hypo[row["hypothesis_tag"]]["strict"] += int(row["reproduced_strict"])

            strict_total += int(row["reproduced_strict"])
            any_total += int(row["reproduced_hot_any"])

            line = (
                f"[{gi}/{total}] {bucket} {row['variant_id']} v={row['victim_idx']} "
                f"hotf={row['hot_fail']} coldf={row['cold_fail']} "
                f"strict_total={strict_total} rc={row['exit_code']} dur={row['duration_s']}s bad={bad}"
            )
            print(line, flush=True)
            fl.write(line + "\n")
            fl.flush()

    summary_lines = [
        "",
        f"DONE seed={seed} iterations={total} strict_hits={strict_total} any_hits={any_total} bad_rows={bad}",
        f"CSV: {CSV_PATH}",
        f"LOG: {LOG_PATH}",
        "",
        "Per schedule_bucket:",
    ]
    for bk in sorted(by_bucket.keys()):
        s = by_bucket[bk]
        rate = 100.0 * s["strict"] / s["n"] if s["n"] else 0.0
        summary_lines.append(f"  {bk}: n={s['n']} strict={s['strict']} ({rate:.1f}%)")

    summary_lines.append("")
    summary_lines.append("Hypothesis tags (hypothesis bucket only):")
    for tag in sorted(by_hypo.keys()):
        s = by_hypo[tag]
        rate = 100.0 * s["strict"] / s["n"] if s["n"] else 0.0
        summary_lines.append(f"  {tag}: n={s['n']} strict={s['strict']} ({rate:.1f}%)")

    summary = "\n".join(summary_lines)
    print(summary, flush=True)
    with LOG_PATH.open("a") as fl:
        fl.write(summary + "\n")


if __name__ == "__main__":
    main()
