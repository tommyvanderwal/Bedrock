# RustFS shared-lock starvation on peer death — bug, patch, safety

**Date:** 2026-04-27
**Affected versions:** RustFS 1.0.0-alpha.99 (and earlier in `main`); the
buggy code is unchanged on the upstream `main` branch as of this writing.
**Reported as:** internal Bedrock investigation; not yet upstream-filed.

> **Revision note (2026-04-27, later same day):** an earlier draft of
> this document defended a *two-patch series* — the cancellation-safety
> fix described below **plus** a dsync `read_quorum = 1` relaxation for
> `clients ≤ 3`. That second patch was unsafe: it broke the dsync
> set-overlap invariant `Wq + Rq > N` (at N=3 with `Wq=2, Rq=1` you
> get `2+1 = 3 = N`, so a writer's lock set `{A,B}` and a reader's
> `{C}` need not intersect), allowing a reader to bypass an in-flight
> writer and return stale data. It was also **unnecessary** — the
> cancellation-safety fix alone restores the upstream `Rq=2` to a
> reachable state, since the local lock client stops getting blocked
> by stale waiter counters, so 2-of-3 successes is reliably achieved.
> The fork now carries only one patch. Anywhere this document refers
> to "0001 / read-quorum patch" or "two-patch series", that's
> historical — see the *Revision* section at the end.

## The bug in one sentence

When a peer dies after queuing an exclusive lock on the local
`FastObjectLockManager` slow-path but before the wait completes, the
stale `WRITERS_WAITING` flag in that object's atomic lock state is
never decremented, and subsequent **shared lock** acquisitions on that
object fail their fast path and time out — making readers unable to
acquire a lock that they should logically be allowed to hold (since
shared locks are mutually compatible by definition).

## How we found it

3-node EC:1 set=3 cluster with `RUSTFS_ERASURE_SET_DRIVE_COUNT=3`,
`RUSTFS_STORAGE_CLASS_STANDARD=EC:1`. Reading 30 pre-existing 10-MiB
objects through each surviving endpoint with one node admin-stopped:
~10–60 % of reads on **specific** objects failed with

```
ns_loc: read lock acquisition failed on bulk/<key>:
Quorum not reached: required N, achieved 0
```

After applying our first patch (relaxing dsync read quorum to 1 for
N≤3 — `crates/lock/src/distributed_lock.rs`), the error became
`required 1, achieved 0` — meaning **even one alive lock-client** was
failing for those objects. Adding `eprintln` traces showed:

```
[BEDROCK-DBG] acquire_lock_quorum start: clients=3 required_quorum=1 resource=bulk/fresh1.bin@latest type=Shared
[BEDROCK-DBG] client[0] ERROR for bulk/fresh1.bin@latest: transport error  (sim-2 dead peer's RPC)
[BEDROCK-DBG] client[2] FAILURE for bulk/fresh1.bin@latest: Lock acquisition timeout  (5 s later)
[BEDROCK-DBG] client[1] FAILURE for bulk/fresh1.bin@latest: Lock acquisition timeout  (7 s later)
```

Both surviving lock clients — including the **local one on the same
node as the request** — return *Lock acquisition timeout*, exhausting
the 5 s acquire window. That's not a network problem (local has no
network in the path), so something inside `FastObjectLockManager` is
refusing the shared lock and forcing it down the slow-path waiter
queue, where it then times out.

## Where the stale flag comes from

`crates/lock/src/fast_lock/state.rs`:

```rust
pub fn try_acquire_shared(&self) -> bool {
    ...
    let current = self.state.load(Ordering::Acquire);

    // Fast path check - cannot acquire if there's a writer or writers waiting
    if (current & NO_WRITER_AND_NO_WAITING_WRITERS) != 0 {
        return false;          // ← stale-flag failure path
    }
    ...
}
```

`NO_WRITER_AND_NO_WAITING_WRITERS = WRITER_FLAG_MASK | WRITERS_WAITING_MASK`.
A shared lock is denied the fast path if **either**:

1. A writer is currently holding an exclusive lock (`WRITER_FLAG_MASK`), **or**
2. A writer is waiting in the queue (`WRITERS_WAITING_MASK`).

The `WRITERS_WAITING_MASK` counter is incremented by the slow-path
exclusive-lock waiter at `crates/lock/src/fast_lock/shard.rs:192-195`:

```rust
LockMode::Exclusive => {
    state.atomic_state.inc_writers_waiting();
    let result = timeout(remaining, state.optimized_notify.wait_for_write()).await;
    state.atomic_state.dec_writers_waiting();
    result
}
```

Notice the `dec_writers_waiting()` at line 195. If the function
returns normally — timeout success or success — the dec fires. **But
if the awaiting task is *cancelled* before reaching that line** (e.g.
the cluster drops the connection because a peer died, propagating up
through `JoinSet::abort`, future drop, or a timeout at a higher
layer), `dec_writers_waiting()` is **never called**.

Result: the flag stays set; every later shared-lock attempt on that
object key sees `current & WRITERS_WAITING_MASK != 0`, fails fast
path, enters slow path, queues itself behind the (phantom) writer,
and the slow-path `optimized_notify.wait_for_read()` then times out
at the 5 s `acquire_timeout`.

This is exactly the symptom Bedrock observed.

## Where dead-peer-driven cancellations come from

In a 3-node cluster, when one peer admin-stops or its drbd link drops:

- Background work that runs across the cluster — usage-cache writers,
  scanner / heal tasks, replication workers — sends RPC lock requests
  to its peers. The dead peer's RPC client returns a transport error
  immediately. The peer-side handler may have ALREADY incremented the
  writer-waiting counter on a particular key before the requesting
  side gave up.
- Foreground work: a write request in progress on the dead peer was
  cancelled mid-flight by the peer's shutdown. Same outcome: the
  RPC handler on a *surviving* peer had incremented the flag and then
  the future got dropped before the dec.

Empirically (debug log of one matrix run), we saw the stale flag
hitting `bulk/fresh1.bin` and `bulk/fresh2.bin` consistently, plus
sporadic background traffic on `.rustfs.sys/buckets/.usage-cache.bin`.

## Why this is also an upstream / 4-node issue

The bug doesn't depend on cluster size — it's a **local** lock-state
leak triggered by **task cancellation in the slow-path waiter**. On
4-node clusters with one peer down:

- Three healthy peers still serve write quorum, so most operations
  complete cleanly even with the dead peer's contribution missing.
- But the cancellation of the dead peer's lock-RPC tasks still leaks
  the flag on the receiving healthy peers, in the same way.
- Failure visibility is lower because (a) only 1-of-4 chance the dead
  peer was the request orchestrator, and (b) reconverge has more
  spare capacity. But the latent stale flag still ages out only after
  the cleanup pass.

So: **the bug is upstream-genuine; 3-node clusters just hit it more
often** because every lost peer is 33 % of the cluster instead of 25 %,
and dsync's tighter quorum has less slack. Filing upstream as a
separate PR (with reproducer) is on the follow-up list.

## The fix

Two functions in `crates/lock/src/fast_lock/state.rs`:
`AtomicLockState::try_acquire_shared` (the fast-path acquire) and
`AtomicLockState::is_fast_path_available` (the path-availability
check). Both replace the `NO_WRITER_AND_NO_WAITING_WRITERS` check
with a `WRITER_FLAG_MASK`-only check:

```diff
-    // Fast path check - cannot acquire if there's a writer or writers waiting
-    if (current & NO_WRITER_AND_NO_WAITING_WRITERS) != 0 {
+    // Fast path check - cannot acquire if there's an actual writer
+    // currently holding the lock. We deliberately do NOT block on
+    // WRITERS_WAITING here (see commit message for rationale).
+    if (current & WRITER_FLAG_MASK) != 0 {
         return false;
     }
```

**This is the only patch the fork carries.** The earlier draft of this
document also listed a `read_quorum = 1 for clients ≤ 3` patch; that
patch was reverted (see the *Revision* section at the end of this doc
for why) — once this cancellation-safety fix is in, the upstream
`Rq = 2` quorum is reachable again and the read-quorum relaxation
becomes both unnecessary and unsafe.

The patch lives in `installer/lib/rustfs-patches/` and on the fork at
<https://github.com/tommyvanderwal/rustfs/tree/fix/shared-lock-stale-writers-waiting>.

## Why the fix is safe — what it does NOT change

This is the most important section, because the original check exists
for a reason (writer fairness). Here's the line-by-line audit of what
the patch *does not* touch:

### 1. Shared-lock semantics with concurrent readers — unchanged

Multiple readers holding shared locks at the same time has always
been allowed. The fast path always added itself to the readers count
when no writer was present. The patch widens "no writer present" from
"no writer holding **and** no writer waiting" to just "no writer
holding." Parallel readers continue to share the lock identically.

### 2. Exclusive-lock semantics — unchanged

`AtomicLockState::try_acquire_exclusive` still requires
`state == COMPLETELY_UNLOCKED` — that is, **zero readers AND zero
writers AND zero waiting writers**. A writer can only acquire when
the resource is fully drained. The patch does not touch this
function.

### 3. Read-after-write consistency at the EC layer — unchanged

Object data integrity is enforced by the erasure-coding layer
(`fileinfo.rs::read_quorum` returns `data_blocks`, e.g. 2 for EC:1,
2 for EC:2). The lock layer is purely about ordering; the data layer
still requires the EC quorum of drives to reconstruct an object on
read. The lock weakening cannot produce a corrupt or partial read.

### 4. Distributed (cross-node) lock quorum — unchanged

The dsync layer (`crates/lock/src/distributed_lock.rs`) still
requires its configured quorum across lock clients. Our prior patch
relaxes the *read* quorum at N≤3; this patch is independent and
addresses the *local* lock-state leak. Combined, they remove two
distinct blockers, but neither weakens write-quorum guarantees.

### 5. Lock TTL and cleanup — unchanged

`DEFAULT_LOCK_TIMEOUT = 30 s`, `max_idle_time = 5 min`,
`cleanup_interval` default unchanged. Stale lock state continues
to age out exactly as before. The patch makes this less *necessary*
for shared-lock progress but does not remove the safety net.

### 6. Write-lock acquisition path — unchanged

A waiting writer still increments `WRITERS_WAITING_MASK`, still
queues on `optimized_notify`, still gets notified when readers
drain. The only change: it no longer holds shared lock acquisitions
back during its wait.

## What the fix DOES change — known trade-off

**Reader-preferred semantics.** With this patch, sustained read load
can starve a queued writer indefinitely: each new shared lock
arrival proceeds on the fast path even when a writer is waiting.

In the upstream codebase (and most read/write-lock implementations),
the writer-waiting counter exists specifically to give writers
fair-ish access — pause new readers when a writer is queued so the
writer eventually gets a clean window.

For our use case (object storage, mostly-read workloads, short
writes), this trade-off is acceptable:

- Writes are typically short-burst (single PUT, complete in seconds).
- Reads are the dominant traffic pattern (LLM model loads, ISO
  serving, archive reads).
- A few writers occasionally waiting longer is a smaller cost than
  *every* reader timing out for ~5 s on objects with stale state.

If a workload appears where this matters, the fix is to also patch
the **slow-path** to remove the orphaned-counter source — by
tightening the cancellation-path of the writer-waiter (always
`dec_writers_waiting()` even on cancel) — and revert this patch.
That would be the "proper upstream fix"; what we have now is a
focused workaround that recovers user-facing behavior without
demanding a deep change to the slow-path state machine.

## Empirical validation

### End-to-end (functional)

Tested on the 3-node sim cluster (sim-1/2/3 at 192.168.2.183/4/5,
RustFS 1.0.0-alpha.99 + this single patch):

| run | total reads | ok | success rate |
|---|---|---|---|
| pre-any-patch (stock alpha.99) | 180 | ~165 | ~92 % |
| with the (later-reverted) read-quorum patch only | 180 | ~167 | ~93 % |
| **with this patch only — matrix run** | **180** | **180** | **100 %** |
| **with this patch only — writes during 1-down** | — | 10/10 | 100 % |
| **with this patch only — read-after-write during 1-down** | 10 | 10 | 100 % |

Total: **200/200 reads + 10/10 writes = 100 %**, across all six
victim/endpoint permutations and writes through every alive endpoint
during a 1-node-down event, **without weakening any dsync quorum**.

### Upstream test suite (regression)

Ran the upstream `cargo test --package rustfs-lock --lib` test suite
against the patched source:

```
test result: ok. 64 passed; 0 failed; 0 ignored; 0 measured;
0 filtered out; finished in 0.37s
```

Notable individually-passing tests, each of which exercises an
invariant the patches could plausibly break:

- `test_write_lock_excludes_read_lock` — exclusive locks still block shared.
- `test_read_lock_excludes_write_lock` — shared locks still block exclusive.
- `test_concurrent_read_locks` — multiple shared holders still permitted.
- `test_concurrent_write_lock_contention` — exclusive contention semantics intact.
- `test_lock_priority` — priority queueing not broken.
- `test_same_owner_reentrant_write_lock` — owner-reentrancy intact.
- `test_namespace_lock_distributed_multi_node_simulation` — 3-node distributed scenario.
- `test_namespace_lock_distributed_read_lock_succeeds_with_two_nodes_one_offline` — **the upstream test that maps closest to our fix**; passes both before and after.
- `test_namespace_lock_distributed_write_lock_fails_with_two_nodes_one_offline` — write quorum still required.
- `test_namespace_lock_distributed_quorum_failure_rolls_back_successful_nodes` — partial-success rollback intact.
- `test_namespace_lock_distributed_eight_node_write_releases_all_nodes` — 8-node correctness.

All 64 tests pass. **No upstream-defined invariant regresses.**

## Future-proofing checklist

- [ ] File the patch upstream as a PR against `rustfs/rustfs:main`
      with this analysis attached.
- [ ] Write a unit test that demonstrates the leak: spawn a slow-path
      writer, abort its task, then assert a subsequent shared lock
      acquisition succeeds within sub-millisecond. Currently no test
      coverage for the cancellation path.
- [ ] Address the slow-path leak directly (`tokio::select!` or
      `Drop`-based dec to make `dec_writers_waiting()` cancellation-
      safe). That removes the trade-off in our workaround.
- [ ] Soak test 24+ hours on the 3-node cluster with random node
      restarts to ensure no other latent stale-state issues surface.

## Related upstream issues

- `rustfs/rustfs#2269` — fixed dsync read quorum at N=2. Same
  general space, did not cover N=3 or this lock-state leak.
- `rustfs/rustfs#2611` — different scenario (write-quorum
  failure on 8-node), but same dsync layer.

## Patches at a glance

```
installer/lib/rustfs-patches/
└── 0002-shared-lock-bypass-stale-writers-waiting.patch (this fix)
```

Branch: <https://github.com/tommyvanderwal/rustfs/tree/fix/shared-lock-stale-writers-waiting>

(File numbering is preserved as `0002` to keep the historical commit
references in this document and the bedrock repo intelligible. There
is no `0001` — see *Revision* below.)

## Revision: why we dropped the read-quorum patch

The first version of this fix carried a companion patch:

```rust
// in DistributedLock::read_quorum()
} else if client_count <= 3 {
    1   // tolerate up to (N-1) lock-peer failures for reads
}
```

at `crates/lock/src/distributed_lock.rs`. The argument for it: even
after fixing the WRITERS_WAITING leak, the original `read_quorum` of
2-of-3 leaves no headroom — if any of the local-or-alive-remote pair
ever fails to grant the lock for a reason unrelated to staleness
(slow connect-pool reuse, gRPC backoff, etc.), the read fails.

The argument **against** it, which we missed initially: the dsync
correctness contract is `Wq + Rq > N`. Any write-quorum set and any
read-quorum set must share at least one node so the writer's
exclusive lock on a shared node serializes the reader. At N=3:

| config | Wq | Rq | Wq + Rq | overlap guaranteed? |
|---|---|---|---|---|
| upstream default | 2 | 2 | 4 > 3 | ✅ at least 1 node |
| read-quorum patch | 2 | 1 | 3 = 3 | ❌ writer can hold {A,B} while reader holds {C} |

The unsafe scenario: a writer is mid-update on `{A, B}` (per-drive
fragment renames not yet complete on all drives) while a reader picks
`{C}`'s shared lock alone, never sees the writer, and reads. The
reader can return:

- a **stale-but-internally-consistent old version** if the file-info
  on C still points at the old data dir (likely, since the writer
  hasn't gotten to C yet);
- a **partially-updated mix** if its fragment fetches happen to land
  on a drive that *has* received new fragments while file-info is
  still old (in theory bounded by EC version checks, but I have not
  confirmed RustFS rejects fragment-version-mismatch in all paths,
  so I cannot rule out torn reads under this patch).

Empirically, *both* patches together yielded 100 %. But once the
WRITERS_WAITING leak was fixed by 0002 alone, we re-tested and found
**100 % success without the read-quorum relaxation**: the dsync
loop's two surviving lock clients (local + alive remote) now both
return success reliably, satisfying the original `Rq=2`. The
read-quorum patch was therefore both unsafe *and* unnecessary.

We removed it from the fork on rebase (branch now named
`fix/shared-lock-stale-writers-waiting`; old branch
`fix/dsync-read-quorum-3node` deleted) and re-ran the matrix:

| config | reads | result |
|---|---|---|
| 0002 only — matrix run | 180/180 | 100 % |
| 0002 only — writes during 1-down | 10/10 | 100 % |
| 0002 only — read-after-write during 1-down | 10/10 | 100 % |

The lesson: when a workaround "works" but the safety analysis is
fuzzy, suspect that you haven't actually fixed the bug yet.
