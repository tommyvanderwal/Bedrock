#!/usr/bin/env bash
# Reproduce the upstream RustFS WRITERS_WAITING cancellation-safety leak
# on a sim cluster (3-node or 4-node, stock alpha.99).
#
# The leak is at crates/lock/src/fast_lock/shard.rs:193:
#
#   inc_writers_waiting()
#   timeout(remaining, ...wait_for_write()).await   <-- cancellation point
#   dec_writers_waiting()                            <-- skipped on Drop
#
# Triggering it requires per-key write-lock contention so the second-and-
# later concurrent exclusive-lock attempts on a given key actually enter
# the slow-path waiter that owns the .await. A fan-out across distinct
# keys takes the fast path and never increments WRITERS_WAITING, which
# is why the first reproducer (100 PUTs across 100 distinct keys) stopped
# firing reliably.
#
# This script: hot keys with high per-key fan-in, large payloads to
# widen the exclusive-lock holding window, originator-side kill while
# the survivors are mid-.await, then post-settle reads on the contended
# keys via every surviving endpoint.

set -euo pipefail

# ---- knobs (override via env) -----------------------------------------
# Space-separated IPs (nested sim DHCP often differs from doc defaults).
# Example: ENDPOINTS_STR='192.168.2.187 192.168.2.188 192.168.2.185 192.168.2.186'
if [[ -n "${ENDPOINTS_STR:-}" ]]; then
  read -ra ENDPOINTS <<< "${ENDPOINTS_STR}"
else
  ENDPOINTS=(${ENDPOINTS:-192.168.2.183 192.168.2.184 192.168.2.185 192.168.2.186})
fi
VICTIM_IDX=${VICTIM_IDX:-0}             # index into ENDPOINTS[] (0-based)
HOT_KEYS=${HOT_KEYS:-20}                # number of contended keys
WRITERS_PER_KEY=${WRITERS_PER_KEY:-8}   # concurrent overwriters per hot key
COLD_KEYS=${COLD_KEYS:-100}             # control set, never touched in burst
PAYLOAD_BYTES=${PAYLOAD_BYTES:-$((100 * 1024 * 1024))}  # 100 MiB
KILL_DELAY=${KILL_DELAY:-0.8}           # seconds after burst start
SETTLE=${SETTLE:-15}                    # peer-down detection window
READ_ROUNDS=${READ_ROUNDS:-3}           # rounds of hot-key reads per survivor
READ_TIMEOUT=${READ_TIMEOUT:-9}         # per-request wall-clock cap (GETs)
READ_TIMEOUT_GRACE=${READ_TIMEOUT_GRACE:-5}  # extra wall-clock headroom above READ_TIMEOUT
PUT_TIMEOUT=${PUT_TIMEOUT:-600}         # large PUTs need headroom under load
POPULATE_PARALLEL=${POPULATE_PARALLEL:-12} # avoid 120×100MiB thundering herd to one endpoint
POST_POPULATE_SETTLE=${POST_POPULATE_SETTLE:-20} # seconds: let replication quiesce before baseline GETs
RESET_WAIT=${RESET_WAIT:-20}            # seconds to wait after RESET restart
HOT_FAIL_FAST_EXIT=${HOT_FAIL_FAST_EXIT:-0}  # >0: stop hot-read loop after N failures
BUCKET=${BUCKET:-leak-repro}
PROFILE=${PROFILE:-rustfs}
STORAGE_CLASS=${STORAGE_CLASS:-STANDARD}  # STANDARD (EC:2) or REDUCED_REDUNDANCY (EC:1)
RESET=${RESET:-0}                       # 1 = stop+start rustfs on all nodes first
SSH_USER=${SSH_USER:-root}
# -----------------------------------------------------------------------

export AWS_MAX_ATTEMPTS=1
export AWS_RETRY_MODE=standard

NODE_COUNT=${#ENDPOINTS[@]}
if (( NODE_COUNT < 2 )); then
  echo "ERROR: need at least 2 endpoints (got ${NODE_COUNT})" >&2
  exit 1
fi
if (( VICTIM_IDX < 0 || VICTIM_IDX >= NODE_COUNT )); then
  echo "ERROR: VICTIM_IDX ${VICTIM_IDX} out of range for ${NODE_COUNT} endpoints" >&2
  exit 1
fi

VICTIM_IP=${ENDPOINTS[$VICTIM_IDX]}
SURVIVORS=()
for ((i=0; i<NODE_COUNT; i++)); do
  [[ $i != $VICTIM_IDX ]] && SURVIVORS+=("${ENDPOINTS[$i]}")
done

RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
HOT_PREFIX="hot-${RUN_ID}-"
COLD_PREFIX="cold-${RUN_ID}-"
PAYLOAD=/tmp/leak-payload-${RUN_ID}.bin
DRAIN=/tmp/leak-drain-${RUN_ID}.bin

cleanup() { rm -f "$PAYLOAD" "$DRAIN"; }
trap cleanup EXIT

aws_call() {
  local ep=$1; shift
  aws --profile "$PROFILE" --endpoint-url "http://$ep:9000" \
      --cli-read-timeout="$READ_TIMEOUT" --cli-connect-timeout=3 "$@"
}

aws_put_call() {
  local ep=$1; shift
  aws --profile "$PROFILE" --endpoint-url "http://$ep:9000" \
      --cli-read-timeout="$PUT_TIMEOUT" --cli-connect-timeout=10 "$@"
}

# Populate can hit RST/timeouts under parallel load; retries keep baseline trustworthy.
populate_put_one() {
  local key=$1 attempt
  for attempt in 1 2 3 4 5 6; do
    if aws_put_call "$VICTIM_IP" s3api put-object --bucket "$BUCKET" \
        --key "$key" --body "$PAYLOAD" --storage-class "$STORAGE_CLASS" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$attempt"
  done
  echo "  WARN: populate_put_one exhausted retries: $key" >&2
  return 1
}

# GNU timeout(1) runs an executable — it cannot invoke bash functions. Wrap aws in bash -c.
timeout_aws_get_object() {
  local ep=$1 bucket=$2 key=$3 drain=$4
  # Outer wall clock slightly above aws cli-read-timeout to avoid spurious kills at the boundary.
  local outer=$((READ_TIMEOUT + READ_TIMEOUT_GRACE))
  timeout "$outer" bash -c '
    aws --profile "$1" --endpoint-url "http://$2:9000" \
      --cli-read-timeout="$6" --cli-connect-timeout=3 \
      s3api get-object --bucket "$3" --key "$4" "$5" >/dev/null 2>&1
    ' _ "$PROFILE" "$ep" "$bucket" "$key" "$drain" "$READ_TIMEOUT"
}

ssh_node() {
  ssh -o StrictHostKeyChecking=no -o ConnectTimeout=4 \
      -o UserKnownHostsFile=/dev/null \
      "$SSH_USER@$1" "$2"
}

banner() {
  echo
  echo "==============================================================="
  echo "  $1"
  echo "==============================================================="
}

# ---- preflight --------------------------------------------------------
banner "preflight"
echo "  victim idx:  $VICTIM_IDX ($VICTIM_IP)"
echo "  survivors:   ${SURVIVORS[*]}"
echo "  hot keys:    $HOT_KEYS x $WRITERS_PER_KEY writers   = $((HOT_KEYS*WRITERS_PER_KEY)) in-flight PUTs"
echo "  cold keys:   $COLD_KEYS (control set)"
echo "  payload:     $((PAYLOAD_BYTES/1048576)) MiB"
echo "  populate:    max ${POPULATE_PARALLEL} concurrent PUTs to victim"
echo "  kill delay:  +${KILL_DELAY}s"
echo "  settle:      ${SETTLE}s"
echo "  bucket:      $BUCKET"
echo "  class:       $STORAGE_CLASS"
echo "  run id:      $RUN_ID"

if [[ "$RESET" == "1" ]]; then
  echo "  resetting in-memory lock state on all nodes ..."
  for ip in "${ENDPOINTS[@]}"; do
    ssh_node "$ip" 'systemctl stop rustfs' || true
  done
  sleep 2
  for ip in "${ENDPOINTS[@]}"; do
    ssh_node "$ip" 'systemctl start rustfs' || true
  done
  echo "  waiting ${RESET_WAIT}s for cluster re-form ..."
  sleep "$RESET_WAIT"
fi

head -c "$PAYLOAD_BYTES" /dev/urandom > "$PAYLOAD"

# ---- bucket setup -----------------------------------------------------
banner "bucket setup"
aws_call "$VICTIM_IP" s3api head-bucket --bucket "$BUCKET" 2>/dev/null \
  || aws_call "$VICTIM_IP" s3api create-bucket --bucket "$BUCKET" >/dev/null
echo "  bucket ready"

# ---- pre-populate -----------------------------------------------------
banner "pre-populate hot + cold baselines"
echo "  uploading $HOT_KEYS hot + $COLD_KEYS cold keys via victim endpoint ..."
for start in $(seq 1 "$POPULATE_PARALLEL" "$HOT_KEYS"); do
  end=$((start + POPULATE_PARALLEL - 1))
  (( end > HOT_KEYS )) && end=$HOT_KEYS
  for k in $(seq "$start" "$end"); do
    ( populate_put_one "${HOT_PREFIX}${k}" ) &
  done
  wait
done
if (( COLD_KEYS > 0 )); then
  for start in $(seq 1 "$POPULATE_PARALLEL" "$COLD_KEYS"); do
    end=$((start + POPULATE_PARALLEL - 1))
    (( end > COLD_KEYS )) && end=$COLD_KEYS
    for k in $(seq "$start" "$end"); do
      ( populate_put_one "${COLD_PREFIX}${k}" ) &
    done
    wait
  done
fi
echo "  done"
echo "  waiting ${POST_POPULATE_SETTLE}s for replication before baseline reads ..."
sleep "$POST_POPULATE_SETTLE"

# ---- baseline read sanity check --------------------------------------
banner "baseline reads (all endpoints, hot keys only)"
b_ok=0; b_fail=0
for ep in "${ENDPOINTS[@]}"; do
  for k in $(seq 1 $HOT_KEYS); do
    if timeout_aws_get_object "$ep" "$BUCKET" "${HOT_PREFIX}${k}" "$DRAIN"; then
      b_ok=$((b_ok+1))
    else
      b_fail=$((b_fail+1))
    fi
  done
done
echo "  hot baseline: $b_ok ok / $b_fail fail (expected $((HOT_KEYS*NODE_COUNT))/0)"
if [[ $b_fail -ne 0 ]]; then
  echo "  baseline failed -- aborting (cluster already in degraded state)"
  exit 2
fi

# ---- the burst -------------------------------------------------------
banner "burst: same-key contention via victim endpoint"
echo "  $((HOT_KEYS*WRITERS_PER_KEY)) concurrent overwriters across $HOT_KEYS keys"
T0=$(date +%s.%N)
for k in $(seq 1 $HOT_KEYS); do
  for w in $(seq 1 $WRITERS_PER_KEY); do
    ( aws_put_call "$VICTIM_IP" s3api put-object --bucket "$BUCKET" \
        --key "${HOT_PREFIX}${k}" --body "$PAYLOAD" --storage-class "$STORAGE_CLASS" >/dev/null 2>&1 ) &
  done
done
sleep "$KILL_DELAY"
echo "  +${KILL_DELAY}s -- killing rustfs on victim ($VICTIM_IP)"
ssh_node "$VICTIM_IP" 'pkill -9 rustfs; pkill -9 podman' || true
wait
T1=$(date +%s.%N)
burst_elapsed=$(LC_ALL=C awk -v a="$T0" -v b="$T1" 'BEGIN { printf "%.2f", b - a + 0 }')
echo "  burst returned after ${burst_elapsed}s"

echo "  settling for ${SETTLE}s (peer-down detection) ..."
sleep "$SETTLE"

# ---- read phase ------------------------------------------------------
banner "post-kill reads"
declare -A hot_fail_by_ep hot_fail_by_key
hot_ok=0; hot_fail=0
echo "  hot keys: $READ_ROUNDS rounds x ${#SURVIVORS[@]} survivors x $HOT_KEYS keys"
hot_fast_exit=0
for round in $(seq 1 $READ_ROUNDS); do
  for ep in "${SURVIVORS[@]}"; do
    for k in $(seq 1 $HOT_KEYS); do
      if timeout_aws_get_object "$ep" "$BUCKET" "${HOT_PREFIX}${k}" "$DRAIN"; then
        hot_ok=$((hot_ok+1))
      else
        hot_fail=$((hot_fail+1))
        hot_fail_by_ep[$ep]=$((${hot_fail_by_ep[$ep]:-0}+1))
        hot_fail_by_key[$k]=$((${hot_fail_by_key[$k]:-0}+1))
        if (( HOT_FAIL_FAST_EXIT > 0 && hot_fail >= HOT_FAIL_FAST_EXIT )); then
          hot_fast_exit=1
          break 3
        fi
      fi
    done
  done
done
if (( hot_fast_exit == 1 )); then
  echo "  hot read phase fast-exit after ${hot_fail} failures (HOT_FAIL_FAST_EXIT=${HOT_FAIL_FAST_EXIT})"
fi

cold_ok=0; cold_fail=0
declare -A cold_fail_by_ep
echo "  cold keys (control): 1 round x ${#SURVIVORS[@]} survivors x $COLD_KEYS keys"
if (( COLD_KEYS > 0 )); then
  for ep in "${SURVIVORS[@]}"; do
    for k in $(seq 1 $COLD_KEYS); do
      if timeout_aws_get_object "$ep" "$BUCKET" "${COLD_PREFIX}${k}" "$DRAIN"; then
        cold_ok=$((cold_ok+1))
      else
        cold_fail=$((cold_fail+1))
        cold_fail_by_ep[$ep]=$((${cold_fail_by_ep[$ep]:-0}+1))
      fi
    done
  done
fi

# ---- report ----------------------------------------------------------
banner "RESULT"
hot_total=$((hot_ok+hot_fail))
cold_total=$((cold_ok+cold_fail))
hot_rate=$(LC_ALL=C awk -v a=$hot_fail -v b=$hot_total 'BEGIN{ if(b==0){print "n/a"} else printf "%.1f", 100*a/b }')
cold_rate=$(LC_ALL=C awk -v a=$cold_fail -v b=$cold_total 'BEGIN{ if(b==0){print "n/a"} else printf "%.1f", 100*a/b }')

echo "  HOT (contended): $hot_ok / $hot_total ok    fail: $hot_fail   ($hot_rate%)"
echo "  COLD (control):  $cold_ok / $cold_total ok    fail: $cold_fail   ($cold_rate%)"

if [[ $hot_fail -gt 0 ]]; then
  echo
  echo "  hot failures by endpoint:"
  for ep in "${SURVIVORS[@]}"; do
    f=${hot_fail_by_ep[$ep]:-0}
    per_ep_total=$((READ_ROUNDS * HOT_KEYS))
    printf "    %-15s %3d / %3d fail\n" "$ep" "$f" "$per_ep_total"
  done
  echo "  hot failures by key (idx -> total fail across all rounds*endpoints):"
  for k in $(seq 1 $HOT_KEYS); do
    f=${hot_fail_by_key[$k]:-0}
    [[ $f -gt 0 ]] && printf "    key %2d: %3d\n" "$k" "$f"
  done
fi

if [[ $cold_fail -gt 0 ]]; then
  echo
  echo "  cold failures by endpoint (unexpected on a healthy cluster):"
  for ep in "${SURVIVORS[@]}"; do
    f=${cold_fail_by_ep[$ep]:-0}
    [[ $f -gt 0 ]] && printf "    %-15s %3d fail\n" "$ep" "$f"
  done
fi

echo
if [[ $hot_fail -eq 0 ]]; then
  echo "  --> bug NOT triggered this run (likely the slow-path waiters drained"
  echo "      before kill, or the victim's PUTs hadn't fanned out far enough)."
  echo "      Try larger PAYLOAD_BYTES, larger WRITERS_PER_KEY, or tune KILL_DELAY."
else
  echo "  --> bug REPRODUCED. Hot-key reads fail while the cold control set"
  echo "      is fine -- only the contended keys hold stale WRITERS_WAITING flags."
fi
