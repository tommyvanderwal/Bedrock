#!/usr/bin/env python3
"""Hotspot confirmation sweep after residual clustering on monte48 / weak anchors.

Targets knob+victim islands that concentrated strict hits in
`sweep-4node-residual-20260506-001544.csv`:

  ~130× monte48 exact knobs (no micro-jitter) + monte48 micro-jitter
  Solid sampling on monte81, monte139; lighter on monte148; monte133 control.

Default ~280 iterations (always >=200). See HOTSPOT_DRY_RUN=1.

Env:
  HOTSPOT_SEED          RNG seed (default 28061402)
  HOTSPOT_DRY_RUN=1     print counts only
  MONTE_INITIAL_RESET   same as other sweep drivers (iter 1 only if set)
  SWEEP_*               sweep_common.py

Usage:
  cd installer/lib/rustfs-patches && PYTHONUNBUFFERED=1 python3 sweep_4node_hotspot_prove.py
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
CSV_PATH = OUTDIR / f"sweep-4node-hotspot-prove-{STAMP}.csv"
LOG_PATH = OUTDIR / f"sweep-4node-hotspot-prove-{STAMP}.log"

HOT_RE = re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE = re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASE_RE = re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

MAX_INFRA_RETRIES = 4

ANCHORS = {
    # Monte CSV anchors + victims from validate-2805 residual design.
    "monte48": {
        "anchor": "monte48",
        "victim": 3,
        "hot": 18,
        "writers": 40,
        "payload": 16,
        "kill": 0.62,
        "rounds": 3,
        "settle": 8,
        "pop_par": 6,
        "post_pop": 6,
    },
    "monte81": {
        "anchor": "monte81",
        "victim": 0,
        "hot": 10,
        "writers": 28,
        "payload": 8,
        "kill": 0.49,
        "rounds": 1,
        "settle": 12,
        "pop_par": 6,
        "post_pop": 10,
    },
    "monte133": {
        "anchor": "monte133",
        "victim": 0,
        "hot": 16,
        "writers": 24,
        "payload": 8,
        "kill": 0.43,
        "rounds": 3,
        "settle": 10,
        "pop_par": 8,
        "post_pop": 8,
    },
    "monte139": {
        "anchor": "monte139",
        "victim": 2,
        "hot": 10,
        "writers": 24,
        "payload": 20,
        "kill": 0.47,
        "rounds": 2,
        "settle": 10,
        "pop_par": 8,
        "post_pop": 8,
    },
    "monte148": {
        "anchor": "monte148",
        "victim": 3,
        "hot": 16,
        "writers": 24,
        "payload": 16,
        "kill": 0.57,
        "rounds": 1,
        "settle": 8,
        "pop_par": 4,
        "post_pop": 8,
    },
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


def exact_variant(base: dict) -> dict:
    v = dict(base)
    v["id"] = variant_id(v)
    return v


def build_schedule(rng: random.Random) -> list[tuple[str, dict]]:
    """(prove_arm, variant dict). prove_arm used for aggregation."""
    sched: list[tuple[str, dict]] = []

    def append_arm(arm: str, base_name: str, variant: dict) -> None:
        v = dict(variant)
        v["anchor"] = ANCHORS[base_name]["anchor"]
        v.setdefault("victim", ANCHORS[base_name]["victim"])
        sched.append((arm, v))

    # monte48: exact knobs (stress reproducibility of hotspot island)
    for _ in range(55):
        b = ANCHORS["monte48"]
        append_arm("monte48_exact", "monte48", exact_variant(b))

    # monte48: micro-jitter (same anchor + victim)
    for _ in range(95):
        append_arm("monte48_jitter", "monte48", micro_jitter(ANCHORS["monte48"], rng))

    for _ in range(48):
        append_arm("monte81_jitter", "monte81", micro_jitter(ANCHORS["monte81"], rng))

    for _ in range(48):
        append_arm("monte139_jitter", "monte139", micro_jitter(ANCHORS["monte139"], rng))

    for _ in range(22):
        append_arm("monte148_jitter", "monte148", micro_jitter(ANCHORS["monte148"], rng))

    for _ in range(22):
        append_arm("monte133_jitter", "monte133", micro_jitter(ANCHORS["monte133"], rng))

    return sched


def run_one(gi: int, prove_arm: str, v: dict) -> dict:
    victim_idx = int(v["victim"])
    bucket = f"leak-hotspot-{STAMP}-{gi}"
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
        "prove_arm": prove_arm,
        "anchor": v["anchor"],
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
    seed = int(os.environ.get("HOTSPOT_SEED", "28061402"))
    rng = random.Random(seed)
    schedule = build_schedule(rng)
    total = len(schedule)

    if os.environ.get("HOTSPOT_DRY_RUN", "") == "1":
        c = defaultdict(int)
        for arm, _ in schedule:
            c[arm] += 1
        print(f"HOTSPOT_DRY_RUN seed={seed} total={total}")
        for k in sorted(c.keys()):
            print(f"  {k}: {c[k]}")
        return

    fields = [
        "iter",
        "prove_arm",
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

    by_arm = defaultdict(lambda: {"n": 0, "strict": 0})
    by_anchor = defaultdict(lambda: {"n": 0, "strict": 0})
    strict_total = 0
    any_total = 0
    bad = 0

    hdr = (
        f"START hotspot_prove seed={seed} total={total} csv={CSV_PATH} log={LOG_PATH}\n"
        f"MONTE_INITIAL_RESET={os.environ.get('MONTE_INITIAL_RESET', '')!r}\n"
    )
    print(hdr, flush=True)

    with CSV_PATH.open("w", newline="") as fc, LOG_PATH.open("w") as fl:
        w = csv.DictWriter(fc, fieldnames=fields)
        w.writeheader()
        fc.flush()
        fl.write(hdr)
        fl.flush()

        for gi, (prove_arm, v) in enumerate(schedule, start=1):
            row = None
            for _ in range(MAX_INFRA_RETRIES):
                if not wait_cluster_ready():
                    time.sleep(8)
                    continue
                row = run_one(gi, prove_arm, v)
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
                    "prove_arm": prove_arm,
                    "anchor": v["anchor"],
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

            arm = row["prove_arm"]
            by_arm[arm]["n"] += 1
            by_arm[arm]["strict"] += int(row["reproduced_strict"])
            an = row["anchor"]
            by_anchor[an]["n"] += 1
            by_anchor[an]["strict"] += int(row["reproduced_strict"])

            strict_total += int(row["reproduced_strict"])
            any_total += int(row["reproduced_hot_any"])

            line = (
                f"[{gi}/{total}] {prove_arm} {row['variant_id']} anchor={row['anchor']} "
                f"v={row['victim_idx']} hotf={row['hot_fail']} coldf={row['cold_fail']} "
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
        "Per prove_arm:",
    ]
    for arm in sorted(by_arm.keys()):
        s = by_arm[arm]
        rate = 100.0 * s["strict"] / s["n"] if s["n"] else 0.0
        summary_lines.append(f"  {arm}: n={s['n']} strict={s['strict']} ({rate:.1f}%)")

    summary_lines.append("")
    summary_lines.append("Per anchor (exact+jitter combined):")
    for an in sorted(by_anchor.keys()):
        s = by_anchor[an]
        rate = 100.0 * s["strict"] / s["n"] if s["n"] else 0.0
        summary_lines.append(f"  {an}: n={s['n']} strict={s['strict']} ({rate:.1f}%)")

    summary = "\n".join(summary_lines)
    print(summary, flush=True)
    with LOG_PATH.open("a") as fl:
        fl.write(summary + "\n")


if __name__ == "__main__":
    main()
