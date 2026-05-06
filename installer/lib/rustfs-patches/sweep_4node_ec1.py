#!/usr/bin/env python3
import csv, os, random, re, subprocess, time
from datetime import datetime
from pathlib import Path

from sweep_common import run_repro_script

_SCRIPT_DIR = Path(__file__).resolve().parent
ENDPOINTS=["192.168.2.189","192.168.2.190","192.168.2.191","192.168.2.192"]
REPRO=_SCRIPT_DIR / "reproduce-leak.sh"
OUTDIR=_SCRIPT_DIR / "sweep-results"
OUTDIR.mkdir(parents=True, exist_ok=True)
STAMP=datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
CSV_PATH=OUTDIR/f'sweep-4node-ec1-{STAMP}.csv'
LOG_PATH=OUTDIR/f'sweep-4node-ec1-{STAMP}.log'

ITERATIONS=int(os.environ.get('ITERATIONS','260'))
RESET_EVERY=int(os.environ.get('RESET_EVERY','20'))

HOT_CHOICES=[int(x) for x in os.environ.get("HOT_CHOICES","10,14,20").split(",") if x]
WRITER_CHOICES=[int(x) for x in os.environ.get("WRITER_CHOICES","12,18,24,32").split(",") if x]
PAYLOAD_MIB_CHOICES=[int(x) for x in os.environ.get("PAYLOAD_MIB_CHOICES","16,32,64,100").split(",") if x]
KILL_DELAY_CHOICES=[float(x) for x in os.environ.get("KILL_DELAY_CHOICES","0.2,0.3,0.35,0.45,0.6,0.8,1.0,1.2").split(",") if x]

HOT_RE=re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE=re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASELINE_RE=re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

def restart_victim(ip):
    subprocess.run(['ssh','-o','StrictHostKeyChecking=no',f'root@{ip}','systemctl restart rustfs; sleep 1; systemctl is-active rustfs >/dev/null'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)

def run_once(i):
    victim=i % len(ENDPOINTS)
    hot=random.choice(HOT_CHOICES)
    writers=random.choice(WRITER_CHOICES)
    payload=random.choice(PAYLOAD_MIB_CHOICES)
    kill=random.choice(KILL_DELAY_CHOICES)
    reset=1 if (i==1 or i % RESET_EVERY==0) else 0
    env=os.environ.copy()
    env.update({
        'ENDPOINTS_STR':' '.join(ENDPOINTS),
        'VICTIM_IDX':str(victim),
        'HOT_KEYS':str(hot),
        'WRITERS_PER_KEY':str(writers),
        'COLD_KEYS':os.environ.get('COLD_KEYS','8'),
        'READ_ROUNDS':os.environ.get('READ_ROUNDS','2'),
        'PAYLOAD_BYTES':str(payload*1024*1024),
        'KILL_DELAY':str(kill),
        'SETTLE':os.environ.get('SETTLE','10'),
        'READ_TIMEOUT':os.environ.get('READ_TIMEOUT','20'),
        'PUT_TIMEOUT':os.environ.get('PUT_TIMEOUT','120'),
        'POPULATE_PARALLEL':os.environ.get('POPULATE_PARALLEL','6'),
        'POST_POPULATE_SETTLE':os.environ.get('POST_POPULATE_SETTLE','3'),
        'RESET':str(reset),
        'RESET_WAIT':os.environ.get('RESET_WAIT','8'),
        'BUCKET':f'leak-repro-4ec1-{(i%9)+1}',
        'PROFILE':'rustfs',
        'STORAGE_CLASS':'REDUCED_REDUNDANCY',
    })
    t0=time.time()
    rc, out = run_repro_script(REPRO, env, int(os.environ.get("SWEEP_REPRO_TIMEOUT", "700")))
    dt=round(time.time()-t0,2)
    m_hot=HOT_RE.search(out); m_cold=COLD_RE.search(out); m_base=BASELINE_RE.search(out)
    hot_fail=int(m_hot.group(1)) if m_hot else -1
    cold_fail=int(m_cold.group(1)) if m_cold else -1
    base_fail=int(m_base.group(2)) if m_base else -1
    strict=int(hot_fail>0 and cold_fail==0)
    anyh=int(hot_fail>0)
    restart_victim(ENDPOINTS[victim])
    return {
      'iter':i,'victim_idx':victim,'victim_ip':ENDPOINTS[victim],'reset':reset,
      'hot_keys':hot,'writers_per_key':writers,'payload_mib':payload,'kill_delay_s':kill,
      'duration_s':dt,'exit_code':rc,'baseline_fail':base_fail,'hot_fail':hot_fail,'cold_fail':cold_fail,
      'reproduced_strict':strict,'reproduced_hot_any':anyh,'raw_tail':'\\n'.join(out.strip().splitlines()[-10:])
    }

def main():
    fields=['iter','victim_idx','victim_ip','reset','hot_keys','writers_per_key','payload_mib','kill_delay_s','duration_s','exit_code','baseline_fail','hot_fail','cold_fail','reproduced_strict','reproduced_hot_any','raw_tail']
    strict=anyh=0
    with CSV_PATH.open('w',newline='') as fc, LOG_PATH.open('w') as fl:
      w=csv.DictWriter(fc,fieldnames=fields); w.writeheader()
      for i in range(1,ITERATIONS+1):
        row=run_once(i)
        w.writerow(row); fc.flush()
        strict += row['reproduced_strict']; anyh += row['reproduced_hot_any']
        line=f"[{i}/{ITERATIONS}] v={row['victim_idx']} hot={row['hot_keys']} w={row['writers_per_key']} p={row['payload_mib']}MiB k={row['kill_delay_s']} base={row['baseline_fail']} hotf={row['hot_fail']} coldf={row['cold_fail']} strict={strict} any={anyh} rc={row['exit_code']} dur={row['duration_s']}s"
        print(line,flush=True); fl.write(line+'\n'); fl.flush()
    print(f"DONE iterations={ITERATIONS} strict_hits={strict} any_hits={anyh}")
    print(f"CSV: {CSV_PATH}")
    print(f"LOG: {LOG_PATH}")

if __name__=='__main__':
    main()
