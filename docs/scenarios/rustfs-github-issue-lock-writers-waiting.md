# RustFS upstream issue — copy-paste body

Use this file when filing `rustfs/rustfs` (bug report template). Select from **“Issue body”** through the end of **“Ask”** (inclusive).

**Note on [rustfs/rustfs#2794](https://github.com/rustfs/rustfs/issues/2794):** that report is about **beta.1 distributed deployment never becoming ready** (`erasure read quorum`, remote disk / format load failures in Kubernetes). It is **not the same bug** as the contended-key **shared-lock / `WRITERS_WAITING`** cancellation issue below. Link #2794 only if you want upstream to know you saw **unrelated** beta.1 operational pain in other topologies.

---

## Issue body

## Summary

`FastObjectLockManager` appears vulnerable to a cancellation-safety leak in exclusive slow-path waiter accounting: stale `WRITERS_WAITING` can remain set after peer/task teardown, causing subsequent shared-lock acquisitions on contended keys to time out even when no active writer holds the lock.

## RustFS Version

- `docker.io/rustfs/rustfs:1.0.0-beta.1`
- Also previously observed on `1.0.0-alpha.99`

## Environment

- 4-node RustFS lab (`192.168.2.189-192`)
- Storage class for test objects: `REDUCED_REDUNDANCY` (`EC:1`)
- RustFS via podman + systemd
- Reproducer script: `installer/lib/rustfs-patches/reproduce-leak.sh` (public link below)

## Steps To Reproduce

1. Use high-contention same-key profile:
   - `HOT_KEYS=16`
   - `WRITERS_PER_KEY=36`
   - `PAYLOAD_BYTES=16 MiB`
   - `KILL_DELAY=0.6`
   - `READ_ROUNDS=2`
   - `STORAGE_CLASS=REDUCED_REDUNDANCY`
2. Start concurrent overwrites through one victim endpoint.
3. Kill victim RustFS during burst.
4. Read hot keys from surviving endpoints and compare with cold control keys.

## Expected Behavior

- Shared-lock reads should recover once no writer is actively holding lock.
- Hot-key reads should not systematically fail while cold controls stay healthy.

## Actual Behavior

- Hot contended keys fail reads (timeouts / lock-acquire failures), while cold control keys remain healthy.
- Failure signature is key-contention specific, not whole-cluster outage.

## Reproduction Frequency (Current)

- Beta.1 c02-style focused run (20 iterations): **`14/20 strict`**, `bad=1`
  - strict criterion: `hot_fail > 0 && cold_fail == 0`
- Beta.1 Monte Carlo run (200 iterations, varied knobs): **`44/200 strict`**, `bad=0`
- Highest observed hotspot band remains around **`(hot=16, writers=36, payload=16 MiB)`**

## Additional Context / Hypothesis

Observed behavior matches a stale waiter-bit path:

- slow-path exclusive waiter increments `WRITERS_WAITING`
- task is cancelled during peer failure/teardown
- decrement is skipped
- stale `WRITERS_WAITING` then blocks shared-lock fast-path on that object, leading to read timeouts under contention

A local patch that ignores stale `WRITERS_WAITING` for shared fast-path (still blocks on active writer bit) removed this failure mode in prior stock-vs-patched lab validation.

## Public Artifacts

### Scenario write-up

- https://github.com/tommyvanderwal/Bedrock/blob/master/docs/scenarios/rustfs-shared-lock-leak-lab-validation-2026-04-30.md
- https://github.com/tommyvanderwal/Bedrock/blob/master/docs/scenarios/rustfs-distributed-lock-writers-waiting-leak-explained.md

### Reproducer + patch

- Reproducer:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/reproduce-leak.sh
- Patch under test:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/0002-shared-lock-bypass-stale-writers-waiting.patch
- RustFS fork branch with patch:
  - https://github.com/tommyvanderwal/rustfs/tree/fix/shared-lock-stale-writers-waiting

### Latest beta.1 focused run

- Log:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/run-c02-20x-20260503T213554Z.log
- CSV:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-c02-beta1-20x-20260503T213554Z.csv

### Latest beta.1 Monte Carlo run (200)

- Log:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260504-011229.log
- CSV:
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260504-011229.csv

### Historical stock vs patched comparison

- Stock confirm (80):
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T083055Z.csv
- Patched confirm (80):
  - https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-confirm-20260430T143527Z.csv

## Ask

Could maintainers confirm whether this cancellation path in lock waiter accounting is expected, and whether an upstream fix should enforce decrement-on-cancel (e.g. guard/Drop-based cleanup) so stale `WRITERS_WAITING` cannot poison shared-lock fast-path for contended keys?

---

## After push: raw GitHub URL for this file

`https://github.com/tommyvanderwal/Bedrock/blob/master/docs/scenarios/rustfs-github-issue-lock-writers-waiting.md`

Open that page, copy from **Issue body** through **Ask**, paste into `rustfs/rustfs` new issue.
