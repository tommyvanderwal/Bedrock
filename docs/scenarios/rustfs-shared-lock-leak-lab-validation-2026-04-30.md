# RustFS shared-lock leak validation (4-node EC:1 lab)

## Scope

This report documents:

1. what the bug is,
2. why the current patch should fix it,
3. why the patch should not violate critical lock invariants,
4. all large sweep data gathered on this dev box,
5. current patched-image validation status.

Environment in this runbook:

- Nested KVM lab on this dev box
- 4 RustFS nodes (`192.168.2.189-192`)
- RustFS service via podman + systemd
- Storage class for test objects: `REDUCED_REDUNDANCY` (`EC:1`)
- Reproducer driver: `installer/lib/rustfs-patches/reproduce-leak.sh`
- Sweep drivers:
  - `installer/lib/rustfs-patches/sweep_4node_20x10.py`
  - `installer/lib/rustfs-patches/sweep_4node_confirm.py`

## Bug statement

The lock bug is a cancellation-safety leak in RustFS fast lock state handling:

- slow-path exclusive waiter increments `WRITERS_WAITING`
- task gets cancelled (peer death / request teardown)
- decrement is skipped
- `WRITERS_WAITING` stays stale
- future shared-lock attempts on the same object fail fast-path and time out

In short: stale waiter metadata blocks readers even when no writer is actually holding the lock.

## Why this is likely the real bug (not just environment noise)

The signal signature is consistent across controlled sweeps:

- failures are on hot contended keys,
- cold control keys remain healthy (`cold_fail=0`),
- reproductions track contention/timing knobs (not random cluster outages),
- full infra-invalid samples were removed and separately diagnosed (disk-full episodes),
- after infra hardening, valid sample quality reached 100% in long runs (`bad=0`).

This strongly indicates lock-state behavior, not generic storage/network instability.

## Patch under test

Patch file:

- `installer/lib/rustfs-patches/0002-shared-lock-bypass-stale-writers-waiting.patch`

Core behavior change:

- shared-lock fast path blocks only on `WRITER_FLAG_MASK` (active writer)
- no longer blocks on `WRITERS_WAITING_MASK` (which can become stale)

Writer acquisition logic remains unchanged (still requires fully unlocked state).

## Why this should fix the issue

The observed failure needs stale `WRITERS_WAITING` to poison reader fast-path.

The patch removes stale-waiter dependence for shared fast-path:

- stale waiter bit no longer forces read slow-path timeout,
- readers can proceed when there is no active writer,
- therefore cancellation leak no longer manifests as persistent read lock timeouts.

## Why this should not break important locking correctness

### Preserved critical properties

- **No read/write overlap with active writer:** shared path still blocks on `WRITER_FLAG_MASK`.
- **Writer exclusion semantics unchanged:** exclusive path still requires unlocked state.
- **Distributed quorum math unchanged:** no read/write quorum relaxations introduced.
- **No API-level change to lock modes:** only stale waiter handling in shared fast-path is adjusted.

### Known tradeoff

- Potential writer fairness degradation under sustained heavy read load (already called out in patch notes).
- This is a liveness/fairness tradeoff, not a read/write overlap correctness break.

### Code-level regression evidence

Patched source validation:

- `cargo test --package rustfs-lock --lib`
- Result: `64 passed, 0 failed`

## Completed stock-image evidence (this dev box)

### 200-run broad sweep (20 variants x 10)

- CSV: `installer/lib/rustfs-patches/sweep-results/sweep-4node-20x10-20260429T203204Z.csv`
- Result: `75/200 strict` (37.5%)
- Sample quality: `bad=0`

Directional signal from this dataset:

- `hot=16` better than `hot=14`, `hot=12`
- `writers=36` strongest (100% in grouped direction analysis)
- strongest practical region around:
  - `hot=16`
  - `writers=36`
  - `payload=16 MiB`
  - `kill_delay=0.6`

### 80-run focused confirmation on stock

- CSV: `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T083055Z.csv`
- Result: `54/80 strict` (67.5%)
- Sample quality: `bad=0`

Per-profile final stock rates:

- `c02` (`hot=16,w=36,p=16,k=0.6`): `20/20` (100%)
- `c01` (`hot=14,w=36,p=16,k=0.6`): `14/20` (70%)
- `c04` (`hot=16,w=32,p=16,k=0.75`): `11/20` (55%)
- `c03` (`hot=14,w=36,p=12,k=0.6`): `9/20` (45%)

This identifies `c02` as the best lock-in profile on stock image in this lab.

## Patched-image validation run (completed)

Cluster has been switched from stock to patched image on all nodes:

- old: `docker.io/rustfs/rustfs:1.0.0-alpha.99`
- new: `docker.io/library/rustfs:patched-3node-readq`

Patched sweep executed:

- command runner: `installer/lib/rustfs-patches/sweep_4node_confirm.py`
- live log: `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-patched-current.out`
- CSV: `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.csv`
- LOG: `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.log`
- scope: same 4 profiles x 20 repeats (`80` runs)

Patched outcome:

- strict reproductions: `0/80`
- sample quality: `bad=0` (all valid rows)
- per-profile:
  - `c01`: `0/20`
  - `c02`: `0/20`
  - `c03`: `0/20`
  - `c04`: `0/20`

Conclusion from stock vs patched:

- stock same-profile confirmation: `54/80 strict` (67.5%)
- patched same-profile confirmation: `0/80 strict` (0%)
- infrastructure quality held constant (`bad=0` both runs)
- this is strong evidence that the patch removes the observed shared-lock failure mode.

## Stock vs patched table

| profile | settings (`hot,writers,payload,kill`) | stock (`80-run` split) | patched (`80-run` split) |
|---|---|---:|---:|
| `c01` | `14,36,16MiB,0.6` | `14/20` | `0/20` |
| `c02` | `16,36,16MiB,0.6` | `20/20` | `0/20` |
| `c03` | `14,36,12MiB,0.6` | `9/20` | `0/20` |
| `c04` | `16,32,16MiB,0.75` | `11/20` | `0/20` |
| **total** | same 4 profiles x 20 repeats | **54/80** | **0/80** |

## RustFS issue submission preferences

RustFS accepts bug reports through GitHub Issues and provides templates:

- bug template: `.github/ISSUE_TEMPLATE/bug_report.md`
- feature template: `.github/ISSUE_TEMPLATE/feature_request.md`

Observed project behavior and guidance:

- Use GitHub issues on `rustfs/rustfs`.
- For this report, use **bug report** template.
- Provide clear reproduction steps, expected vs actual behavior, environment, and logs/CSV links.
- The project uses status labels (example seen in issues: `S-reproducing`), so a reproducible report with dataset attachments is aligned with their workflow.

Recommended issue payload for this bug:

1. one-sentence bug statement,
2. minimal reproducer command and exact knobs (`c02` profile),
3. stock 80-run proof (`20/20` in c02),
4. patched 80-run comparison (pending completion),
5. lock invariants/safety notes,
6. attached CSV artifacts.

## Public evidence links (Bedrock + RustFS fork)

All key artifacts are in this repository and should be linked directly in the upstream issue:

- report:
  - `docs/scenarios/rustfs-shared-lock-leak-lab-validation-2026-04-30.md`
- stock broad sweep:
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-20x10-20260429T203204Z.csv`
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-20x10-20260429T203204Z.log`
- stock focused confirmation:
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T083055Z.csv`
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T083055Z.log`
- patched focused confirmation:
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.csv`
  - `installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.log`
- patch under test (RustFS fork):
  - `installer/lib/rustfs-patches/0002-shared-lock-bypass-stale-writers-waiting.patch`
  - branch: `https://github.com/tommyvanderwal/rustfs/tree/fix/shared-lock-stale-writers-waiting`

GitHub URL pattern for Bedrock-hosted artifacts after push:

- `https://github.com/tommyvanderwal/Bedrock/blob/master/<path>`

Concrete examples:

- report:
  - `https://github.com/tommyvanderwal/Bedrock/blob/master/docs/scenarios/rustfs-shared-lock-leak-lab-validation-2026-04-30.md`
- stock 80-run:
  - `https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T083055Z.csv`
- patched 80-run:
  - `https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.csv`

## Upstream recommendation

Recommended upstream issue framing:

1. This is a cancellation-safety leak in fast lock waiter accounting, causing stale `WRITERS_WAITING` to block shared-lock fast path.
2. Reproduction is deterministic in this lab with profile `c02` on stock (`20/20`).
3. Patch eliminates the failure mode under the same profile set (`0/80` across four top profiles).
4. Lock correctness is preserved around active writer exclusion; only stale waiter handling changes on shared fast path.
