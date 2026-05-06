#!/usr/bin/env python3
import csv
import os
import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from sweep_common import run_repro_script

_SCRIPT_DIR = Path(__file__).resolve().parent
ENDPOINTS = ["192.168.2.189", "192.168.2.190", "192.168.2.191"]
REPRO = _SCRIPT_DIR / "reproduce-leak.sh"
OUTDIR = _SCRIPT_DIR / "sweep-results"
OUTDIR.mkdir(parents=True, exist_ok=True)
STAMP = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
CSV_PATH = OUTDIR / f'sweep-3node-{STAMP}.csv'
LOG_PATH = OUTDIR / f'sweep-3node-{STAMP}.log'

ITERATIONS = int(os.environ.get('ITERATIONS', '520'))
RESET_EVERY = int(os.environ.get('RESET_EVERY', '25'))

HOT_CHOICES = [int(x) for x in os.environ.get('HOT_CHOICES', '6,8,10,12').split(',') if x]
WRITER_CHOICES = [int(x) for x in os.environ.get('WRITER_CHOICES', '8,12,16,20,24').split(',') if x]
PAYLOAD_MIB_CHOICES = [int(x) for x in os.environ.get('PAYLOAD_MIB_CHOICES', '8,16,32,64').split(',') if x]
KILL_DELAY_CHOICES = [float(x) for x in os.environ.get('KILL_DELAY_CHOICES', '0.25,0.40,0.60,0.85,1.10,1.40,1.80,2.30,3.00').split(',') if x]

HOT_RE = re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE = re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASELINE_RE = re.compile(r"hot baseline: (\d+) ok / (\d+) fail")


def restart_victim(ip: str):
    subprocess.run([
        'ssh', '-o', 'StrictHostKeyChecking=no', f'root@{ip}',
        'systemctl restart rustfs; sleep 1; systemctl is-active rustfs >/dev/null'
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def run_once(i: int):
    victim_idx = i % len(ENDPOINTS)
    hot = random.choice(HOT_CHOICES)
    writers = random.choice(WRITER_CHOICES)
    payload_mib = random.choice(PAYLOAD_MIB_CHOICES)
    kill_delay = random.choice(KILL_DELAY_CHOICES)

    reset = 1 if (i == 1 or i % RESET_EVERY == 0) else 0

    cold_keys = os.environ.get('COLD_KEYS', '4')
    read_rounds = os.environ.get('READ_ROUNDS', '1')
    settle = os.environ.get('SETTLE', '6')
    read_timeout = os.environ.get('READ_TIMEOUT', '20')
    put_timeout = os.environ.get('PUT_TIMEOUT', '90')
    populate_parallel = os.environ.get('POPULATE_PARALLEL', '6')
    post_populate_settle = os.environ.get('POST_POPULATE_SETTLE', '3')
    reset_wait = os.environ.get('RESET_WAIT', '6')

    env = os.environ.copy()
    env.update({
        'ENDPOINTS_STR': ' '.join(ENDPOINTS),
        'VICTIM_IDX': str(victim_idx),
        'HOT_KEYS': str(hot),
        'WRITERS_PER_KEY': str(writers),
        'COLD_KEYS': cold_keys,
        'READ_ROUNDS': read_rounds,
        'PAYLOAD_BYTES': str(payload_mib * 1024 * 1024),
        'KILL_DELAY': str(kill_delay),
        'SETTLE': settle,
        'READ_TIMEOUT': read_timeout,
        'PUT_TIMEOUT': put_timeout,
        'POPULATE_PARALLEL': populate_parallel,
        'POST_POPULATE_SETTLE': post_populate_settle,
        'RESET': str(reset),
        'RESET_WAIT': reset_wait,
        'BUCKET': f'leak-repro-sweep-{(i % 7) + 1}',
        'PROFILE': 'rustfs',
    })

    t0 = time.time()
    rc, out = run_repro_script(REPRO, env, int(os.environ.get("SWEEP_REPRO_TIMEOUT", "420")))
    dt = round(time.time() - t0, 2)

    m_hot = HOT_RE.search(out)
    m_cold = COLD_RE.search(out)
    m_base = BASELINE_RE.search(out)

    hot_fail = int(m_hot.group(1)) if m_hot else -1
    cold_fail = int(m_cold.group(1)) if m_cold else -1
    base_fail = int(m_base.group(2)) if m_base else -1

    reproduced_strict = int(hot_fail > 0 and cold_fail == 0)
    reproduced_hot_any = int(hot_fail > 0)

    restart_victim(ENDPOINTS[victim_idx])

    return {
        'iter': i,
        'victim_idx': victim_idx,
        'victim_ip': ENDPOINTS[victim_idx],
        'reset': reset,
        'hot_keys': hot,
        'writers_per_key': writers,
        'payload_mib': payload_mib,
        'kill_delay_s': kill_delay,
        'duration_s': dt,
        'exit_code': rc,
        'baseline_fail': base_fail,
        'hot_fail': hot_fail,
        'cold_fail': cold_fail,
        'reproduced_strict': reproduced_strict,
        'reproduced_hot_any': reproduced_hot_any,
        'note': 'ok' if rc == 0 else 'nonzero_exit',
        'raw_tail': '\n'.join(out.strip().splitlines()[-10:]),
    }


def main():
    random.seed()
    fields = [
        'iter', 'victim_idx', 'victim_ip', 'reset', 'hot_keys', 'writers_per_key',
        'payload_mib', 'kill_delay_s', 'duration_s', 'exit_code', 'baseline_fail',
        'hot_fail', 'cold_fail', 'reproduced_strict', 'reproduced_hot_any', 'note', 'raw_tail'
    ]
    strict_hits = 0
    any_hits = 0

    with CSV_PATH.open('w', newline='') as f_csv, LOG_PATH.open('w') as f_log:
        w = csv.DictWriter(f_csv, fieldnames=fields)
        w.writeheader()

        for i in range(1, ITERATIONS + 1):
            row = run_once(i)
            w.writerow(row)
            f_csv.flush()

            strict_hits += row['reproduced_strict']
            any_hits += row['reproduced_hot_any']
            line = (
                f"[{i}/{ITERATIONS}] victim={row['victim_idx']} hot={row['hot_keys']} writers={row['writers_per_key']} "
                f"payload={row['payload_mib']}MiB kill={row['kill_delay_s']}s "
                f"base_fail={row['baseline_fail']} hot_fail={row['hot_fail']} cold_fail={row['cold_fail']} "
                f"strict_hits={strict_hits} any_hits={any_hits} rc={row['exit_code']} dur={row['duration_s']}s"
            )
            print(line, flush=True)
            f_log.write(line + '\n')
            f_log.flush()

    print(f"DONE iterations={ITERATIONS} strict_hits={strict_hits} any_hits={any_hits}")
    print(f"CSV: {CSV_PATH}")
    print(f"LOG: {LOG_PATH}")


if __name__ == '__main__':
    main()
