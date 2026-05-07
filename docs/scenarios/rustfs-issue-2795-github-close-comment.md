# rustfs/rustfs #2795 — issue close comment (copy/paste)

Use the block below as the closing comment on [rustfs/rustfs#2795](https://github.com/rustfs/rustfs/issues/2795).

---

## Closing comment (GitHub)

Thanks everyone — especially for [**PR #2805**](https://github.com/rustfs/rustfs/pull/2805) (cancellation-safe waiter accounting on the slow lock path).

### Resolution for *this* issue

From our side, **the failure mode described in this issue is addressed as far as we can reasonably test it in the lab.**

We re-ran the Bedrock **4-node EC:1** contention/kill repro harness against **main including #2805**:

- **Focused c02-style sweep (20×):** **`strict=0/20`**, **`bad=0`**  
  ([run log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/run-c02-20x-validate2805-20260505T122955Z.log), [CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-c02-beta1-20x-20260505T122955Z.csv))
- **Monte Carlo (200×, seed `2805001`):** **`strict=6/200`**, **`bad=0`** — compared to our documented **beta.1** baseline of **`44/200` strict** on the same harness family, this is a **large reduction**  
  ([CSV](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.csv), [log](https://github.com/tommyvanderwal/Bedrock/blob/master/installer/lib/rustfs-patches/sweep-results/sweep-4node-monte200-20260505-151017.log))

Together with #2805 landing, **we consider this issue closed**: the original symptom bucket we tied to slow-path waiter cancellation behavior is no longer reproducing at the reliability we saw pre-fix on the profiles we trust most (notably the focused c02 runs).

### Follow-up (not blocking this close)

Randomized and anchor-focused sweeps **still occasionally** produce rows matching the harness “strict” criterion (`hot_fail>0` with `cold_fail==0`) **well below** historical beta.1 rates, but **not at literal zero** everywhere in knob space.

As one example, we ran an additional **400-iteration** residual sweep pinned/jittered around the Monte strict hits (`installer/lib/rustfs-patches/sweep_4node_residual_focus.py`, seed `2805991`): **`strict_hits=25/400`**, **`bad_rows=0`**. Hits were **not** uniformly distributed: they clustered more heavily on certain anchor families (especially “weak”-schedule repeats around high fan-in settings) than on the original symmetric-looking anchors — consistent with **some residual, non–100% reliable weakness that still warrants characterization**.

We are **still investigating** whether that residue reflects **real remaining server-side pathology**, **harness/client-timeout sensitivity**, or **distributed heal timing** under EC:1 load — i.e. whether it matters in real deployments.

**If we isolate a new, specific mechanism with credible reproducer evidence, we will open a fresh bug report** rather than keeping this thread open as a catch-all.

Thanks again for the fix and the quick turnaround.

---

## Notes (repo maintenance)

- Longer validation write-up with context: [`rustfs-issue-2795-post-merge-validation-2026-05-05.md`](./rustfs-issue-2795-post-merge-validation-2026-05-05.md)
- Residual sweep artifacts live under `installer/lib/rustfs-patches/sweep-results/` (e.g. `sweep-4node-residual-20260506-001544.csv` / `.log`). Commit and push those files before linking them from GitHub if you want permanent URLs in follow-up comments.
