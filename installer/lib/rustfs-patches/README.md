# RustFS source patches for Bedrock

Local copies of the RustFS source modifications Bedrock needs.
Each patch is also published as a branch on the fork at
<https://github.com/tommyvanderwal/rustfs> for easy rebase/upstream when
RustFS releases new versions.

## Current patch set (against `rustfs/rustfs@main` ≥ 1.0.0-alpha.99)

A single patch. Earlier iterations carried a second one (a dsync
read-quorum relaxation) that turned out to be both unnecessary and
unsafe — see `docs/scenarios/rustfs-shared-lock-leak-2026-04-27.md`
for the full reasoning. Once the actual bug was identified and fixed
by the patch below, the read-quorum relaxation became redundant *and*
broke the dsync set-overlap invariant (`Wq + Rq > N`), so it was
dropped from the fork on this rebase.

| # | file | branch | rationale |
|---|---|---|---|
| 0002 | `0002-shared-lock-bypass-stale-writers-waiting.patch` | [`fix/shared-lock-stale-writers-waiting`](https://github.com/tommyvanderwal/rustfs/tree/fix/shared-lock-stale-writers-waiting) | Cancellation-safety leak in the slow-path waiter on `FastObjectLockManager`: when a peer dies before its slow-path exclusive-lock waiter task reaches `dec_writers_waiting()`, the `WRITERS_WAITING_MASK` bit stays set indefinitely and every subsequent shared lock acquisition on that object's state fails its fast path → enters slow path → times out at the 5 s acquire window. Patch makes shared-lock fast path block only on `WRITER_FLAG_MASK` (an actual exclusive holder), not on stale waiter counters. Preserves all upstream lock-overlap and quorum guarantees; passes all 64 upstream `cargo test --package rustfs-lock --lib`. **This bug is not 3-node-specific** — same leak exists on 4+ node clusters; just less visible there because more peers stay healthy and reconverge faster. |

## Why no read-quorum patch

Earlier rebuilds carried a `read_quorum = 1 for clients ≤ 3` patch.
It "fixed" the symptom but broke the underlying invariant: distributed
read/write locks rely on every read-quorum set and every write-quorum
set sharing **at least one node**, i.e. `Wq + Rq > N`. At N=3 that
means `Wq + Rq ≥ 4`. The original `Wq=2, Rq=2` satisfies this; the
relaxed `Wq=2, Rq=1` does **not** (`2+1 = 3 = N`), so a writer holding
the lock on `{A, B}` and a reader holding on `{C}` never see each
other and the reader can return stale data while the writer is still
mid-update.

The actual bug — and the only thing that needed fixing — was the
cancellation-safety leak that prevented the *normal* `Rq=2` from
being reachable when one peer was dead. With that fixed, `Rq=2` is
satisfied (local + alive remote = 2), the original quorum holds, and
the lock-overlap invariant is preserved.

Empirically validated, 0002 alone, on a 3-node sim cluster (RustFS
1.0.0-alpha.99 + this patch):

| test | reads | writes | success |
|---|---|---|---|
| matrix (every node down in turn × every endpoint × 30 objects) | 180 | — | 100 % |
| writes during 1-down (10 PUTs via surviving endpoint) | — | 10 | 100 % |
| read-after-write during 1-down | 10 | — | 100 % |
| upstream `cargo test --package rustfs-lock --lib` | 64 | — | 100 % |

## How to rebase onto a new RustFS release

```bash
cd /tmp/rustfs-src/rustfs   # or your local clone

git remote update --prune
git checkout fix/shared-lock-stale-writers-waiting
git rebase v1.0.0-<new-tag>

# Conflicts most likely in crates/lock/src/fast_lock/state.rs around
# try_acquire_shared and is_fast_path_available. Re-apply the
# WRITER_FLAG_MASK-only check (see the patch) and drop the upstream
# NO_WRITER_AND_NO_WAITING_WRITERS check.

git push --force-with-lease origin fix/shared-lock-stale-writers-waiting
```

## Building the patched container image

`build-patched.sh` in this directory does it:

```bash
RUSTFS_SRC=/tmp/rustfs-src/rustfs ./build-patched.sh
```

It strips the BuildKit `--mount=type=cache` directives (so plain
Docker without buildx works) and switches the runtime base from
`ubuntu:22.04` to `ubuntu:24.04` so the trixie-built binary's
`GLIBC_2.39` references resolve.

## When to drop this patch

When upstream merges a cancellation-safe `dec_writers_waiting()` —
ideally via `Drop` on a guard struct around the slow-path waiter —
this patch becomes a no-op and can be removed.
