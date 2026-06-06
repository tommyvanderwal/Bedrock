#!/bin/bash
# DRBD quorum-lost-Primary UUID-rotation bug reproducer / fix validator.
#
# Self-contained, Bedrock-independent: builds a 4-node DRBD-9 resource on loop
# files over the mgmt network, then runs the scenario-B partition + failover +
# heal, capturing the spurious 'new current UUID' and the resync volume.
#
# Same harness validates BOTH the unpatched and patched module — only the .ko
# loaded differs.  Usage:
#   repro.sh distribute <src_sim> <ko_dir_on_src> <label>   # fan the .ko out to all sims
#   repro.sh setup <label>                                  # swap module + build clean resource
#   repro.sh round <N> <outdir>                             # one partition/failover/heal round
#   repro.sh teardown                                       # remove test resource (leaves module)
#   repro.sh gi | status                                    # helpers
set -u

# node-id -> ip / hostname  (resolved 2026-06-06 via spawn.get_mgmt_ip)
IPS=(192.168.2.30 192.168.2.27 192.168.2.28 192.168.2.29)
HOSTS=(bedrock-305eec bedrock-56f13b bedrock-853bdf bedrock-9fa125)
SIMNAME=(sim-1 sim-2 sim-3 sim-4)             # for human output
PRIMARY=0                                      # node-id of the Primary we partition (sim-1)
KEEP=1                                          # the peer the Primary KEEPS (sim-2) -> minority of 2
MAJ=(2 3)                                       # the other side (sim-3, sim-4); MAJ[0] gets force-promoted
RES=bugtest
MINOR=25
PORT=7790
BACKING=/root/bugtest.img
BACKLINK=/dev/drbd-bugtest-backing
SIZE_MB=1024                                    # backing device size
W_MB=256                                        # bytes the survivor writes during the partition

SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
S() { local n=$1; shift; $SSH root@"${IPS[$n]}" "$@"; }    # run on node-id $1
ALL() { for n in 0 1 2 3; do S "$n" "$@"; done; }
say() { echo -e "\n\033[1;36m== $* ==\033[0m"; }

gen_res() {
  cat <<EOF
resource $RES {
  options {
    quorum all;
    on-no-quorum suspend-io;
    auto-promote no;
  }
  net { protocol C; }
  volume 0 {
    device    /dev/drbd$MINOR;
    disk      $BACKLINK;
    meta-disk internal;
  }
  on ${HOSTS[0]} { node-id 0; address ${IPS[0]}:$PORT; }
  on ${HOSTS[1]} { node-id 1; address ${IPS[1]}:$PORT; }
  on ${HOSTS[2]} { node-id 2; address ${IPS[2]}:$PORT; }
  on ${HOSTS[3]} { node-id 3; address ${IPS[3]}:$PORT; }
  connection-mesh { hosts ${HOSTS[0]} ${HOSTS[1]} ${HOSTS[2]} ${HOSTS[3]}; }
}
EOF
}

cmd_distribute() {  # distribute <src_node> <ko_dir> <label>
  local src=$1 dir=$2 label=$3
  local dst="/root/drbd-ko-$label"
  say "distribute .ko from ${SIMNAME[$src]}:$dir -> all sims:$dst (label=$label)"
  mkdir -p /tmp/ko-$label
  for f in drbd.ko drbd_transport_tcp.ko; do
    $SSH root@"${IPS[$src]}" "cat $dir/$f" > /tmp/ko-$label/$f || { echo "pull $f FAILED"; return 1; }
    echo "pulled $f ($(du -h /tmp/ko-$label/$f | cut -f1))"
  done
  for n in 0 1 2 3; do
    S "$n" "mkdir -p $dst"
    for f in drbd.ko drbd_transport_tcp.ko; do
      $SSH root@"${IPS[$n]}" "cat > $dst/$f" < /tmp/ko-$label/$f
    done
    echo "  ${SIMNAME[$n]}: $(S "$n" "ls -la $dst/*.ko | wc -l") ko files; ver=$(S "$n" "modinfo $dst/drbd.ko 2>/dev/null | awk '/^version/{print \$2}'")"
  done
}

cmd_setup() {  # setup <label>
  local label=$1
  local dst="/root/drbd-ko-$label"
  say "STOP all Bedrock/weed/obs services + swap to module label=$label"
  ALL "u=\$(systemctl list-units --no-legend --plain 'bedrock*' 'weed*' 'vmagent*' 'vlagent*' 'vmbackup*' 2>/dev/null | awk '{print \$1}'); [ -n \"\$u\" ] && systemctl stop \$u 2>/dev/null; systemctl stop bedrock-d 2>/dev/null; true"
  # force-unmount any filesystem on a drbd device (kill stragglers holding it open)
  ALL "for m in \$(mount | awk '/\\/dev\\/drbd/{print \$3}'); do fuser -km \"\$m\" 2>/dev/null; sleep 1; umount -f \"\$m\" 2>/dev/null || umount -l \"\$m\" 2>/dev/null; done; true"
  ALL "drbdadm down all 2>/dev/null; sleep 1; true"
  ALL "rmmod drbd_transport_tcp drbd_transport_lb-tcp drbd_transport_rdma 2>/dev/null; rmmod drbd 2>/dev/null; true"
  for n in 0 1 2 3; do
    local loaded; loaded=$(S "$n" "lsmod | grep -c '^drbd '")
    if [ "$loaded" != "0" ]; then echo "  ${SIMNAME[$n]}: drbd STILL loaded - aborting"; return 1; fi
  done
  say "insmod built module on all sims"
  ALL "insmod $dst/drbd.ko && insmod $dst/drbd_transport_tcp.ko"
  for n in 0 1 2 3; do
    echo "  ${SIMNAME[$n]}: $(S "$n" "cat /proc/drbd 2>/dev/null | head -1")"
  done
  say "create backing loop ($SIZE_MB MB) + symlink $BACKLINK on all"
  ALL "truncate -s ${SIZE_MB}M $BACKING; L=\$(losetup -j $BACKING | cut -d: -f1); [ -z \"\$L\" ] && L=\$(losetup -f --show $BACKING); ln -sfn \$L $BACKLINK; echo \$L"
  say "write $RES.res on all"
  local res; res=$(gen_res)
  for n in 0 1 2 3; do printf '%s\n' "$res" | $SSH root@"${IPS[$n]}" "cat > /etc/drbd.d/$RES.res"; done
  say "create-md + up on all"
  ALL "drbdadm create-md --force $RES >/dev/null 2>&1 && echo md-ok || echo md-FAIL"
  ALL "drbdadm up $RES && echo up-ok || echo up-FAIL"
  sleep 3
  say "skip initial sync (fresh zeroed devices) via clear-bitmap on node $PRIMARY"
  S "$PRIMARY" "drbdadm new-current-uuid --clear-bitmap $RES/0 && echo cleared || echo clear-FAIL"
  sleep 4
  say "status after setup"
  S "$PRIMARY" "drbdadm status $RES"
}

curuuid() { S "$1" "drbdadm get-gi $RES/0 2>/dev/null" | cut -d: -f1; }   # current-UUID of node $1
allgi() { for n in 0 1 2 3; do echo "${SIMNAME[$n]} (node$n): $(S "$n" "drbdadm get-gi $RES/0 2>/dev/null")"; done; }

partition() {   # cut {0,1} | {2,3}
  for n in 0 1 2 3; do S "$n" "iptables-save > /tmp/bugtest-ipt.bak"; done
  for n in 0 1; do S "$n" "iptables -I INPUT 1 -s ${IPS[2]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[2]} -j DROP; iptables -I INPUT 1 -s ${IPS[3]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[3]} -j DROP"; done
  for n in 2 3; do S "$n" "iptables -I INPUT 1 -s ${IPS[0]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[0]} -j DROP; iptables -I INPUT 1 -s ${IPS[1]} -j DROP; iptables -I OUTPUT 1 -d ${IPS[1]} -j DROP"; done
}
heal() { for n in 0 1 2 3; do S "$n" "iptables-restore < /tmp/bugtest-ipt.bak 2>/dev/null; true"; done; }

reset_clean() {   # fresh resource so every round starts all-UpToDate
  ALL "drbdadm down $RES 2>/dev/null; true"
  ALL "drbdadm create-md --force $RES >/dev/null 2>&1; drbdadm up $RES"
  sleep 3; S "$PRIMARY" "drbdadm new-current-uuid --clear-bitmap $RES/0 >/dev/null 2>&1"; sleep 3
}

cmd_round() {   # round <N> <outdir>
  local N=$1 OUT=$2
  mkdir -p "$OUT"
  say "ROUND $N  (module under test) -> $OUT"
  ALL "dmesg -C 2>/dev/null; true"                 # clear ring buffer: all dmesg below is this round

  # baseline: promote sim-1, write a small known generation, replicate
  S "$PRIMARY" "drbdadm primary $RES"
  S "$PRIMARY" "dd if=/dev/urandom of=/dev/drbd$MINOR bs=1M count=32 oflag=direct status=none 2>/dev/null; sync"
  sleep 2
  local C; C=$(curuuid "$PRIMARY")
  allgi > "$OUT/gi_1_before.txt"
  echo "C (pre-partition current-UUID on ${SIMNAME[$PRIMARY]}) = $C" | tee "$OUT/summary.txt"

  say "PARTITION  {sim-1,sim-2} | {sim-3,sim-4}"
  partition
  sleep 10                                          # let DRBD detect loss, run 2PC, (maybe) rotate
  S "$PRIMARY" "drbdadm status $RES" > "$OUT/status_2_partition_sim1.txt" 2>&1
  S "$PRIMARY" "dmesg -T | grep -iE 'quorum|new current UUID|susp' | tail -25" > "$OUT/dmesg_2_partition_sim1.txt" 2>&1
  allgi > "$OUT/gi_2_partition.txt"
  local L; L=$(curuuid "$PRIMARY")
  local rotated=NO; [ -n "$L" ] && [ "$L" != "$C" ] && rotated=YES
  { echo "L (post-partition current-UUID on ${SIMNAME[$PRIMARY]}) = $L";
    echo "ROTATED (L != C) = $rotated";
    echo "dmesg 'new current UUID' on sim-1 during partition: $(S "$PRIMARY" "dmesg -T | grep -c 'new current UUID'")"; } | tee -a "$OUT/summary.txt"

  say "FAILOVER  force-promote ${SIMNAME[${MAJ[0]}]} + write ${W_MB}M"
  S "${MAJ[0]}" "drbdadm primary --force $RES" > "$OUT/failover_promote.txt" 2>&1
  S "${MAJ[0]}" "drbdadm resume-io $RES 2>/dev/null; true"
  timeout 60 ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null root@"${IPS[${MAJ[0]}]}" "dd if=/dev/urandom of=/dev/drbd$MINOR bs=1M count=$W_MB oflag=direct status=none 2>/dev/null; sync; echo wrote-${W_MB}M" > "$OUT/failover_write.txt" 2>&1
  cat "$OUT/failover_write.txt"
  allgi > "$OUT/gi_3_failover.txt"

  say "HEAL"
  heal
  sleep 15
  for n in 0 1 2 3; do S "$n" "drbdadm status $RES" > "$OUT/status_4_heal_${SIMNAME[$n]}.txt" 2>&1; done
  for n in 0 1 2 3; do S "$n" "dmesg -T | grep -iE 'resync|split|will sync|bits set|StandAlone|UpToDate|Inconsistent' " > "$OUT/dmesg_4_heal_${SIMNAME[$n]}.txt" 2>&1; done
  allgi > "$OUT/gi_4_heal.txt"

  # resync volume: DRBD logs "will sync X KB [Y bits set]" on each SyncTarget
  say "RESYNC VOLUME (from kernel 'will sync')"
  for n in 0 1 2 3; do
    local v; v=$(S "$n" "dmesg -T | grep -oE 'will sync [0-9]+ KB' | tail -3 | tr '\n' ';'")
    [ -n "$v" ] && echo "${SIMNAME[$n]}: $v" | tee -a "$OUT/summary.txt"
  done
  S "$PRIMARY" "dmesg -T | grep -iE 'Resync done|sync.*finished'" >> "$OUT/summary.txt" 2>&1

  say "ROUND $N done; resetting to clean all-UpToDate for next round"
  ALL "drbdadm secondary $RES 2>/dev/null; true"
  reset_clean
  echo "post-reset status:"; S "$PRIMARY" "drbdadm status $RES" | head -12 | tee "$OUT/status_5_postreset.txt"
}

cmd_gi() { for n in 0 1 2 3; do echo "-- ${SIMNAME[$n]} --"; S "$n" "drbdadm show-gi $RES/0 2>/dev/null | tail -8"; done; }
cmd_status() { S "$PRIMARY" "drbdadm status $RES"; }

cmd_teardown() {
  say "teardown $RES"
  ALL "drbdadm down $RES 2>/dev/null; rm -f /etc/drbd.d/$RES.res; L=\$(readlink $BACKLINK); losetup -d \$L 2>/dev/null; rm -f $BACKLINK $BACKING; true"
}

case "${1:-}" in
  distribute) shift; cmd_distribute "$@";;
  setup)      shift; cmd_setup "$@";;
  round)      shift; cmd_round "$@";;
  gi)         cmd_gi;;
  status)     cmd_status;;
  teardown)   cmd_teardown;;
  promote)    S "$PRIMARY" "drbdadm primary $RES; dd if=/dev/urandom of=/dev/drbd$MINOR bs=1M count=32 oflag=direct status=none 2>/dev/null; sync"; echo "sim-1 current=$(curuuid $PRIMARY)";;
  part)       partition; echo "partitioned";;
  heal)       heal; echo "healed";;
  cur)        echo "sim-1 current-UUID = $(curuuid "$PRIMARY")";;
  allgi)      allgi;;
  on)         shift; n=$1; shift; S "$n" "$@";;
  reset)      heal; ALL "drbdadm secondary $RES 2>/dev/null; true"; reset_clean; S "$PRIMARY" "drbdadm status $RES";;
  *) echo "usage: $0 {distribute <src> <dir> <label>|setup <label>|round <N> <outdir>|gi|status|teardown}";;
esac
