# RustFS issue #2795 — post-merge lab validation (PR #2805)

## Purpose

Short lab validation note after upstream merged [`fix(lock): make slow-path waiter accounting cancellation-safe (#2805)`](https://github.com/rustfs/rustfs/pull/2805). Use the **GitHub comment** section when closing [rustfs/rustfs#2795](https://github.com/rustfs/rustfs/issues/2795).

## Environment

- Four-node nested-KVM lab (`192.168.2.189`–`192.168.2.192`), EC:1-style reduced redundancy workloads per existing Bedrock sweep harnesses.
- Deployed image: **`localhost/rustfs:validate-2805`**, built from `rustfs/rustfs` **main** including the merge commit for #2805 ([`49b2782d`](https://github.com/rustfs/rustfs/commit/49b2782d) in the validation clone used for the image build).

## Focused sweep (c02 profile, 20 runs)

- **Result:** `strict=0/20`, `bad=0` (strict = `hot_fail>0 && cold_fail==0` per CSV rows).
- Artifacts (Bedrock repo):
  - [run log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/run-c02-20x-validate2805-20260505T122955Z.log)
  - [CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-c02-beta1-20x-20260505T122955Z.csv)

## Monte Carlo sweep (200 iterations)

- **Driver:** `installer/lib/rustfs-patches/sweep_4node_monte200.py`
- **Seed:** `MONTE_SEED=2805001`
- **Result:** `strict_hits=6`, `any_hits=6`, `bad_rows=0` (same strict definition as above).
- **Strict iterations (for transparency):** `48`, `81`, `87`, `133`, `139`, `148` — each with `cold_fail=0` and small `hot_fail` counts (1–3), i.e. harness-level signature matches, not cluster-wide failure.

### Comparison to pre-fix beta.1 Monte baseline

Documented elsewhere in this repo for the same harness family: beta.1 Monte 200-run **`44/200 strict`**, `bad=0` (see [rustfs-github-issue-lock-writers-waiting.md](./rustfs-github-issue-lock-writers-waiting.md)). Post-merge **`6/200`** is a large reduction but **not** a literal zero on this randomized suite — wording below reflects that.

### Artifacts

- [Monte CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.csv)
- [Monte detailed log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.log)
- [Outer wrapper log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-validate2805-20260505T151017Z.log) (same run; captures shell redirect / driver banner)

---

## GitHub issue comment (copy/paste)

We re-ran the Bedrock 4-node EC:1 repro harness against an image built from **main including PR #2805** (`validate-2805` build).

- **Focused c02 sweep (20×):** `strict=0/20`, `bad=0` — [log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/run-c02-20x-validate2805-20260505T122955Z.log), [CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-c02-beta1-20x-20260505T122955Z.csv).
- **Monte Carlo (200×, seed `2805001`):** `strict=6/200`, `bad=0` — compared to our documented beta.1 baseline **`44/200` strict** on the same style harness, this is a **large reduction** in signature hits; the remaining six rows still satisfy the strict CSV criterion (`hot_fail>0`, `cold_fail==0`) with low hot counts — [CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.csv), [log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.log).

Net: lab evidence strongly supports that #2805 addresses the cancellation/waiter-accounting failure mode we were chasing; the randomized suite no longer looks like beta.1’s **`44/200`** failure rate, even though we do not claim a perfect **0/200** on Monte after the fix.
