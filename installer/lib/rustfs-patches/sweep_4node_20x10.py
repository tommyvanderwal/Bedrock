#!/usr/bin/env python3
import csv, os, random, re, shlex, subprocess, time
from datetime import datetime
from pathlib import Path

ENDPOINTS=["192.168.2.189","192.168.2.190","192.168.2.191","192.168.2.192"]
REPRO=Path('/home/tommy/pythonprojects/bedrock/installer/lib/rustfs-patches/reproduce-leak.sh')
OUTDIR=Path('/home/tommy/pythonprojects/bedrock/installer/lib/rustfs-patches/sweep-results')
OUTDIR.mkdir(parents=True, exist_ok=True)
STAMP=datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
CSV_PATH=OUTDIR/f'sweep-4node-20x10-{STAMP}.csv'
LOG_PATH=OUTDIR/f'sweep-4node-20x10-{STAMP}.log'

HOT_RE=re.compile(r"HOT \(contended\): .* fail: (\d+)")
COLD_RE=re.compile(r"COLD \(control\):\s+.* fail: (\d+)")
BASE_RE=re.compile(r"hot baseline: (\d+) ok / (\d+) fail")

# High-hit center: hot=14 writers=32 payload=16MiB kill=0.6
VARIANTS=[
 {'id':'v01','hot':14,'writers':32,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v02','hot':12,'writers':32,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v03','hot':16,'writers':32,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v04','hot':14,'writers':28,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v05','hot':14,'writers':36,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v06','hot':14,'writers':32,'payload':12,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v07','hot':14,'writers':32,'payload':24,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v08','hot':14,'writers':32,'payload':16,'kill':0.45,'rounds':2,'settle':10},
 {'id':'v09','hot':14,'writers':32,'payload':16,'kill':0.75,'rounds':2,'settle':10},
 {'id':'v10','hot':12,'writers':28,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v11','hot':16,'writers':36,'payload':16,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v12','hot':12,'writers':32,'payload':24,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v13','hot':16,'writers':32,'payload':12,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v14','hot':14,'writers':28,'payload':24,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v15','hot':14,'writers':36,'payload':12,'kill':0.60,'rounds':2,'settle':10},
 {'id':'v16','hot':12,'writers':32,'payload':16,'kill':0.45,'rounds':2,'settle':10},
 {'id':'v17','hot':16,'writers':32,'payload':16,'kill':0.75,'rounds':2,'settle':10},
 {'id':'v18','hot':14,'writers':32,'payload':24,'kill':0.45,'rounds':2,'settle':10},
 {'id':'v19','hot':14,'writers':32,'payload':12,'kill':0.75,'rounds':2,'settle':10},
 {'id':'v20','hot':14,'writers':32,'payload':16,'kill':0.60,'rounds':1,'settle':12},
]

TOTAL_ROUNDS=10
MAX_INFRA_RETRIES=3


def restart_victim(ip:str):
    subprocess.run(['ssh','-o','StrictHostKeyChecking=no',f'root@{ip}',
                    'systemctl restart rustfs; sleep 1; systemctl is-active rustfs >/dev/null'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

def wait_cluster_ready(profile:str='rustfs', timeout_s:int=180) -> bool:
    deadline=time.time()+timeout_s
    while time.time() < deadline:
        all_ok=True
        for ep in ENDPOINTS:
            cmd=(
                f"timeout 8 aws --profile {shlex.quote(profile)} "
                f"--endpoint-url http://{ep}:9000 "
                f"--cli-read-timeout 3 --cli-connect-timeout 2 "
                "s3api list-buckets >/dev/null 2>&1"
            )
            rc=subprocess.run(['bash','-lc',cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
            if rc != 0:
                all_ok=False
                break
        if all_ok:
            return True
        time.sleep(5)
    return False

def is_infra_failure(out:str, rc:int, base_fail:int, hot_fail:int, cold_fail:int) -> bool:
    if rc in (255,):
        return True
    if 'populate_put_one exhausted retries' in out:
        return True
    if 'No space left on device' in out or 'Disk full' in out:
        return True
    if 'baseline failed -- aborting' in out:
        return True
    # Treat parse-miss with nonzero exit as infra/noise rather than signal.
    if rc != 0 and hot_fail == -1 and cold_fail == -1 and base_fail == -1:
        return True
    return False

def cleanup_bucket(bucket:str, profile:str='rustfs'):
    # Keep dataset quality high over long runs by avoiding disk accumulation.
    for ep in ENDPOINTS:
        cmd=(
            f"timeout 20 aws --profile {shlex.quote(profile)} "
            f"--endpoint-url http://{ep}:9000 "
            f"s3 rb s3://{bucket} --force >/dev/null 2>&1"
        )
        rc=subprocess.run(['bash','-lc',cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
        if rc == 0:
            return


def run_one(global_iter:int, round_idx:int, variant:dict):
    victim_idx=global_iter % len(ENDPOINTS)
    env=os.environ.copy()
    bucket_name=f"leak-repro-20x10-{variant['id']}-{round_idx}-{global_iter}"
    env.update({
        'ENDPOINTS_STR':' '.join(ENDPOINTS),
        'VICTIM_IDX':str(victim_idx),
        'HOT_KEYS':str(variant['hot']),
        'WRITERS_PER_KEY':str(variant['writers']),
        'COLD_KEYS':'8',
        'READ_ROUNDS':str(variant['rounds']),
        'PAYLOAD_BYTES':str(variant['payload']*1024*1024),
        'KILL_DELAY':str(variant['kill']),
        'SETTLE':str(variant['settle']),
        'READ_TIMEOUT':'9',
        'READ_TIMEOUT_GRACE':'4',
        'PUT_TIMEOUT':'120',
        'POPULATE_PARALLEL':'6',
        'POST_POPULATE_SETTLE':'8',
        'RESET':'1',
        'RESET_WAIT':'30',
        'HOT_FAIL_FAST_EXIT':'3',
        'BUCKET':bucket_name,
        'PROFILE':'rustfs',
        'STORAGE_CLASS':'REDUCED_REDUNDANCY',
    })

    t0=time.time()
    proc=subprocess.run(['bash',str(REPRO)],env=env,capture_output=True,text=True,timeout=900)
    dt=round(time.time()-t0,2)
    out=proc.stdout+'\n'+proc.stderr
    mh=HOT_RE.search(out); mc=COLD_RE.search(out); mb=BASE_RE.search(out)
    hot_fail=int(mh.group(1)) if mh else -1
    cold_fail=int(mc.group(1)) if mc else -1
    base_fail=int(mb.group(2)) if mb else -1
    strict=int(hot_fail>0 and cold_fail==0)
    anyh=int(hot_fail>0)

    cleanup_bucket(bucket_name, profile=env['PROFILE'])
    restart_victim(ENDPOINTS[victim_idx])

    return {
      'iter':global_iter,'round':round_idx,'variant_id':variant['id'],'victim_idx':victim_idx,
      'hot_keys':variant['hot'],'writers_per_key':variant['writers'],'payload_mib':variant['payload'],'kill_delay_s':variant['kill'],
      'read_rounds':variant['rounds'],'settle_s':variant['settle'],'duration_s':dt,'exit_code':proc.returncode,
      'baseline_fail':base_fail,'hot_fail':hot_fail,'cold_fail':cold_fail,
      'reproduced_strict':strict,'reproduced_hot_any':anyh,
      'raw_tail':'\\n'.join(out.strip().splitlines()[-14:])
    }


def main():
    fields=['iter','round','variant_id','victim_idx','hot_keys','writers_per_key','payload_mib','kill_delay_s','read_rounds','settle_s','duration_s','exit_code','baseline_fail','hot_fail','cold_fail','reproduced_strict','reproduced_hot_any','raw_tail']
    strict=anyh=0
    gi=0
    with CSV_PATH.open('w',newline='') as fc, LOG_PATH.open('w') as fl:
        w=csv.DictWriter(fc,fieldnames=fields); w.writeheader(); fc.flush()
        print(f"START total={TOTAL_ROUNDS*len(VARIANTS)} csv={CSV_PATH} log={LOG_PATH}", flush=True)
        fl.write(f"START total={TOTAL_ROUNDS*len(VARIANTS)} csv={CSV_PATH} log={LOG_PATH}\n"); fl.flush()
        for rnd in range(1,TOTAL_ROUNDS+1):
            order=VARIANTS[:]
            random.shuffle(order)
            for v in order:
                gi += 1
                row=None
                infra_retries=0
                while infra_retries < MAX_INFRA_RETRIES:
                    if not wait_cluster_ready(timeout_s=180):
                        infra_retries += 1
                        continue
                    try:
                        row=run_one(gi,rnd,v)
                    except subprocess.TimeoutExpired:
                        row={
                          'iter':gi,'round':rnd,'variant_id':v['id'],'victim_idx':gi%4,
                          'hot_keys':v['hot'],'writers_per_key':v['writers'],'payload_mib':v['payload'],'kill_delay_s':v['kill'],
                          'read_rounds':v['rounds'],'settle_s':v['settle'],'duration_s':900,'exit_code':124,
                          'baseline_fail':-1,'hot_fail':-1,'cold_fail':-1,'reproduced_strict':0,'reproduced_hot_any':0,'raw_tail':'timeout'
                        }
                    if is_infra_failure(
                        row['raw_tail'], row['exit_code'], row['baseline_fail'], row['hot_fail'], row['cold_fail']
                    ):
                        infra_retries += 1
                        time.sleep(6)
                        continue
                    break
                if row is None:
                    row={
                      'iter':gi,'round':rnd,'variant_id':v['id'],'victim_idx':gi%4,
                      'hot_keys':v['hot'],'writers_per_key':v['writers'],'payload_mib':v['payload'],'kill_delay_s':v['kill'],
                      'read_rounds':v['rounds'],'settle_s':v['settle'],'duration_s':0,'exit_code':255,
                      'baseline_fail':-1,'hot_fail':-1,'cold_fail':-1,'reproduced_strict':0,'reproduced_hot_any':0,'raw_tail':'cluster_not_ready_after_retries'
                    }
                w.writerow(row); fc.flush()
                strict += row['reproduced_strict']; anyh += row['reproduced_hot_any']
                line=(f"[{gi}/200] r{rnd} {row['variant_id']} v={row['victim_idx']} hot={row['hot_keys']} w={row['writers_per_key']} "
                      f"p={row['payload_mib']}MiB k={row['kill_delay_s']} rr={row['read_rounds']} hotf={row['hot_fail']} coldf={row['cold_fail']} "
                      f"strict={strict} any={anyh} rc={row['exit_code']} dur={row['duration_s']}s")
                print(line,flush=True); fl.write(line+'\n'); fl.flush()
    print(f"DONE iterations=200 strict_hits={strict} any_hits={anyh}")
    print(f"CSV: {CSV_PATH}")
    print(f"LOG: {LOG_PATH}")

if __name__=='__main__':
    main()
