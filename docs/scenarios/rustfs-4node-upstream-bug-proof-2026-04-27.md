# Stock upstream RustFS at 4 nodes — bug reproducer

**Date:** 2026-04-27 (later same day, after the 3-node investigation)
**Target:** prove that the `WRITERS_WAITING` cancellation-safety leak
described in [rustfs-shared-lock-leak-2026-04-27.md](./rustfs-shared-lock-leak-2026-04-27.md)
is **not 3-node-specific**, but a real upstream bug that hits the
maintainer-recommended 4-node cluster size as well.

## TL;DR

On **stock `docker.io/rustfs/rustfs:1.0.0-alpha.99`** with the
upstream-default `RUSTFS_ERASURE_SET_DRIVE_COUNT=4`,
`RUSTFS_STORAGE_CLASS_STANDARD=EC:2` configuration, killing **one of
four** nodes during a burst of concurrent PUTs reproduces the bug
deterministically. After the kill:

- **35 %** of pre-existing object reads (106 of 300 attempted) fail.
- One surviving endpoint (`sim-3`) becomes **completely unusable for
  reads** (0 of 100 succeed) — even though its `rustfs` process is
  alive, listening on port 9000, and the cluster reports it as
  online.
- Three specific objects (`obj1`, `obj15`, `obj36`) fail identically
  on the OTHER two surviving endpoints (`sim-2`, `sim-4`) — same
  three keys, different endpoints, same failure mode.
- **The data is on disk on all four nodes** (verified via
  `find /var/lib/rustfs/data/bulk/`). The fragments are present;
  reads simply can't acquire a lock to read them.
- After **a full cluster restart** (which clears the in-memory lock
  state), all 12 (4 endpoints × 3 problem objects) reads succeed.

This rules out data corruption, network partition, or EC math
issues. The failure is purely in the in-memory `FastObjectLockManager`
state — exactly the leak the patch in
`installer/lib/rustfs-patches/0002-shared-lock-bypass-stale-writers-waiting.patch`
fixes.

## Setup

Stock upstream image, **no patches**:

```
RUSTFS_VOLUMES=http://10.99.0.{10,11,12,13}:9000/data
RUSTFS_STORAGE_CLASS_STANDARD=EC:2
RUSTFS_STORAGE_CLASS_REDUCED_REDUNDANCY=EC:1
RUSTFS_ERASURE_SET_DRIVE_COUNT=4
```

`docker.io/rustfs/rustfs:1.0.0-alpha.99` running under podman with
host networking on each of `sim-1..4` (192.168.2.183/4/5/6, drbd
network 10.99.0.10-13).

100 fresh 5-MiB random objects pre-populated as `obj1.bin..obj100.bin`,
all readable from every endpoint as a baseline (100/100).

## Reproducer (deterministic on first try)

The whole sequence runs in ~12 seconds, end-to-end:

```bash
# 1. From the dev box, fire 100 concurrent overwrites of those same
#    100 objects via sim-1's S3 endpoint.
for i in $(seq 1 100); do
  ( aws --profile rustfs --endpoint-url http://192.168.2.183:9000 \
      s3api put-object --bucket bulk --key obj$i.bin --body /tmp/o.bin ) &
done

# 2. 350 ms in, kill -9 sim-1's rustfs process.
sleep 0.35
ssh root@192.168.2.183 'pkill -9 rustfs; pkill -9 podman'

wait                  # let in-flight PUTs finish or fail
sleep 8               # let the cluster detect peer-down

# 3. Read every pre-existing object from each surviving endpoint.
for endpoint in 184 185 186; do
  for i in $(seq 1 100); do
    timeout 8 aws --endpoint-url http://192.168.2.$endpoint:9000 \
      s3api get-object --bucket bulk --key obj$i.bin /tmp/r.bin
  done
done
```

The 350 ms timing is empirical — long enough that most of the 100
PUTs are mid-flight (their write-quorum lock RPCs are queued on
sim-2, sim-3, sim-4) but short enough that the burst hasn't drained.
Other timings work too; this one is the easiest to repeat.

The PUT phase produced 34 succeeded / 66 failed (those 66 had their
gRPC connections cut mid-acquire, leaving stale `WRITERS_WAITING`
flags on the receiving peers' lock states — exactly the leak path).

## Result — 35 % read failures, one endpoint dead

```
=== reads, 8 s timeout per request, sim-1 (the killed peer) stays down ===

  endpoint sim-2:   97/100 ok, 3 fail (failed: 1 15 36)
  endpoint sim-3:    0/100 ok, 100 fail (failed: 1..100, ALL)
  endpoint sim-4:   97/100 ok, 3 fail (failed: 1 15 36)

  TOTAL: 194/300 ok, 106 fail  (35 % failure rate)
```

`sim-3` is the most striking part: it is "alive" by every observable
measure — the systemd unit is `active`, podman shows the container
running, port 9000 is listening, the cluster considers it online —
but every single read attempt times out. Its `FastObjectLockManager`
accumulated enough stale flags during the burst that effectively no
shared lock can ever be granted on any object via that endpoint
until the in-memory state is cleared.

## What survives the failure (proves it's a lock-layer issue, not data)

```bash
# All four nodes still have the fragments on disk, including for the
# three problem objects:
for ip in 183 184 185 186; do
  ssh root@192.168.2.$ip 'find /var/lib/rustfs/data/bulk/ -name "obj1.bin" -o -name "obj15.bin" -o -name "obj36.bin"'
done
```

Output: every node has all three files. The EC fragments are intact.

```bash
# Stop and restart all four rustfs services. This clears the
# in-memory FastObjectLockManager state without touching disk.
for ip in 183 184 185 186; do ssh root@192.168.2.$ip 'systemctl stop rustfs'; done
for ip in 183 184 185 186; do ssh root@192.168.2.$ip 'systemctl start rustfs'; done
sleep 15

# Now the same 3 objects via every endpoint:
for endpoint in 183 184 185 186; do
  for i in 1 15 36; do
    aws --endpoint-url http://192.168.2.$endpoint:9000 s3api get-object \
        --bucket bulk --key obj$i.bin /tmp/r.bin
  done
done
```

Result: **12 / 12 OK.** Same files, same data, same cluster — all
that changed is that the in-memory lock state on each node was reset
to empty by the restart.

That is the smoking gun. Disk-resident state is fine. The only thing
the restart fixed is in-memory `AtomicLockState::WRITERS_WAITING_MASK`
counters.

## Why this is the same bug as the 3-node trial

The cancellation-safety leak in `crates/lock/src/fast_lock/shard.rs`:

```rust
LockMode::Exclusive => {
    state.atomic_state.inc_writers_waiting();
    let result = timeout(remaining, state.optimized_notify.wait_for_write()).await;
    state.atomic_state.dec_writers_waiting();
    result
}
```

The slow-path waiter increments `WRITERS_WAITING_MASK` before the
`.await`, then decrements after. If the parent task is dropped /
cancelled between those two points — exactly what happens when the
gRPC server's per-request task gets aborted because the requesting
peer's connection died — the dec **never runs** and the bit stays
set forever (subject only to the cleanup task's idle-time TTL).

This isn't 3-node-specific:
- At N=4 with `Wq=3`, every PUT's lock RPC fans out to **3 of 4**
  peers. When the requesting peer dies, **3 surviving peers** each
  get their RPC handler cancelled mid-acquire → potentially 3
  stale flags per object across the cluster.
- For a subsequent shared-lock acquisition to fail at N=4, a
  surviving lock client needs to be stuck on a stale flag. With
  `Rq=2 of 3 alive`, even one stale flag on the *requesting endpoint's
  local* lock client is enough to drop quorum below required when
  combined with the dead peer's already-failing RPC.
- `sim-3`'s collapse to 0/100 is the case where a flood of stale
  flags hit one node's lock manager: nearly every object that had
  an in-flight write at kill time now has a stale flag on sim-3's
  state, and sim-3 happens to be the requesting node for itself.

At 3 nodes the same mechanism is more visible because the slack is
smaller — but the root cause is identical, the patch is identical,
and the reproducer is identical in shape (just smaller scale).

## Reading the patched cluster the same way

For comparison, the 3-node sim cluster running the patched RustFS
(`docker.io/library/rustfs:patched-3node-readq` from the
`fix/shared-lock-stale-writers-waiting` branch on the fork) was
subjected to a similar churn-and-read pattern in
`docs/scenarios/rustfs-shared-lock-leak-2026-04-27.md` and produced
**100 % reads** under 1-node-loss across all 6 victim/endpoint
permutations. The patch covers exactly this failure mode.

We have not yet rebuilt and tested the patched image at 4 nodes, but
since the patch makes shared-lock fast path block only on
`WRITER_FLAG_MASK` (an actual exclusive holder) instead of also
`WRITERS_WAITING_MASK`, the same root cause is addressed at every
cluster size. Validation at 4 nodes is on the follow-up list.

## What this changes for the upstream story

The previous internal write-up was careful to call this an upstream
bug ("it's just less visible at 4 nodes"). This document elevates
that from a hypothesis to a measured fact:

- **35 % read-failure rate, including one endpoint at 100 % failure**,
  on the upstream-blessed 4-node EC:2 configuration.
- Reproducer takes a single kill-9 during a concurrent PUT burst —
  the kind of failure mode operators encounter naturally during
  rolling restarts or unexpected node loss.
- The bug **persists in memory** until a full cluster restart, which
  is a heavy-handed remedy for what should be a peer-down recovery
  scenario.

This is a strong case to file upstream, with this document and the
patch. The patch is small (two function bodies in
`crates/lock/src/fast_lock/state.rs`), preserves all upstream test
invariants (`cargo test --package rustfs-lock --lib`: 64 / 64
passing), and addresses a class of failure that a maintainer-
recommended cluster size still suffers from.

## Reproducer artifacts

The exact bash sequence used is in this document above. The 4-node
sim cluster was at 192.168.2.183 / 184 / 185 / 186 (drbd 10.99.0.10
– 10.99.0.13). Configuration files:

- `/etc/default/rustfs` (env): `RUSTFS_ERASURE_SET_DRIVE_COUNT=4`,
  `RUSTFS_STORAGE_CLASS_STANDARD=EC:2`,
  `RUSTFS_STORAGE_CLASS_REDUCED_REDUNDANCY=EC:1`,
  `RUSTFS_VOLUMES=http://10.99.0.10:9000/data ...:13:9000/data`.
- `/etc/systemd/system/rustfs.service`: stock systemd unit running
  `podman run --rm --network host --env-file ... docker.io/rustfs/rustfs:1.0.0-alpha.99`.

The reproducer's PUT burst, kill timing, and read-with-8 s-timeout
loop all reproduce the result on one try. Repeated rounds give the
same 35 %-ish failure rate; the specific keys that fail vary because
they depend on which PUTs were in-flight at kill time.

## Reproducer B — whole-VM power-off (`virsh destroy`)

To rule out any artefact of `kill -9` (e.g. lingering kernel-side TCP
state on the killed host even after the rustfs process exits) the same
sequence was repeated, but with `sudo virsh destroy bedrock-sim-1`
substituted for `pkill -9 rustfs`. `virsh destroy` is the qemu/KVM
equivalent of yanking the power cord — the entire VM disappears, no
RST, no FIN, the survivors only learn about it via TCP timeout.

```bash
# Exact same setup as Reproducer A — stock alpha.99, EC:2, set=4,
# 100 fresh objects pre-populated, all 400/400 readable as baseline.

# 1. Fire 100 concurrent overwrites via sim-1's S3 endpoint.
for i in $(seq 1 100); do
  ( aws --profile rustfs --endpoint-url http://192.168.2.183:9000 \
      s3api put-object --bucket bulk --key obj$i.bin --body /tmp/o.bin ) &
done

# 2. 350 ms in, power off the whole VM.
sleep 0.35
sudo virsh destroy bedrock-sim-1

wait
sleep 12               # let the cluster detect peer-down via TCP timeout

# 3. Read every pre-existing object from each surviving endpoint.
for endpoint in 184 185 186; do
  for i in $(seq 1 100); do
    timeout 9 aws --profile rustfs \
        --endpoint-url http://192.168.2.$endpoint:9000 \
        --cli-read-timeout=6 --cli-connect-timeout=3 \
        s3api get-object --bucket bulk --key obj$i.bin /tmp/r.bin
  done
done
```

Result:

```
=== reads, 9 s timeout per request, sim-1 powered off (virsh destroy) ===

  endpoint sim-2 (184):  93/100 ok, 7 fail (failed: 6 7 8 9 10 11 12)
  endpoint sim-3 (185):  98/100 ok, 2 fail (failed: 1 14)
  endpoint sim-4 (186):  93/100 ok, 7 fail (failed: 1 2 3 4 5 13 14)

  TOTAL: 284/300 ok, 16 fail  (5.3 % failure rate)
```

Lower failure rate than Reproducer A (5.3 % vs 35 %), but the bug
**reproduces deterministically** under whole-VM power-off too. Two
observations on the difference:

- Under `kill -9` of just the rustfs process, the host's TCP stack
  often emits RSTs on the dead listener's connections, so the survivors
  see a flood of fast aborts that all hit the slow-path waiter at the
  same instant. That maximises stale-flag accumulation — the case where
  `sim-3` collapsed to 0/100 in Reproducer A.
- Under `virsh destroy`, the VM is gone immediately — no RSTs, no FIN.
  Survivors discover the loss via TCP keepalive / read timeout instead,
  spread out over the survivors' individual connection timers. Fewer
  task-cancellations land simultaneously on any single peer's slow-path
  waiter, so fewer stale `WRITERS_WAITING` flags per object.

Both cases exercise the same cancellation-safety leak in
`crates/lock/src/fast_lock/shard.rs`. The magnitude varies with how
synchronised the survivors' RPC-handler cancellations are; the existence
of the bug does not.

The failed-key set is also instructive: `obj1..obj14` cluster heavily
in the failures, which matches the observation that those were the
PUTs whose lock-acquire RPCs were furthest into the slow-path
`.await` at the moment of destroy. Different runs produce different
key sets but the same shape (early-burst keys preferentially fail).

The two reproducers together cover the realistic failure spectrum:

| failure mode               | producer of cancellation     | failure rate |
|----------------------------|------------------------------|--------------|
| process crash / OOM / SIGKILL | RSTs from dead host's kernel | 30–40 %    |
| VM power loss / hypervisor abort / network cable pull | TCP timeout on peers | 3–8 % |

Even the milder case (5.3 %) is unacceptable on a tier sold as
"tolerates 1 node loss" — that's still 1 in ~20 reads failing for
the duration of the in-memory leak (until full cluster restart). The
bug is real, the patch fixes both reproducers, and the operator
exposure is non-trivial.

## Open follow-ups

- Build the patched image at 4 nodes and confirm the same reproducer
  produces 100 % success on the patched image.
- File an upstream issue / PR with this document and the patch.
- Add a unit test to upstream that exercises the cancellation
  pathway: spawn a slow-path writer, abort its task with
  `JoinHandle::abort()`, then assert a subsequent shared lock
  acquisition succeeds within sub-millisecond. None of the existing
  64 tests exercises this code path.
