# Resync-volume cost — buggy vs patched, with the realistic demote-the-loser sequence

Full failover + heal, the way Bedrock actually does it (minority **demoted to Secondary** before
heal). `cost_round.sh`; backing device 1 GiB; majority writes **W = 256 MiB** during the partition.

| | minority current at heal | heal outcome | resync volume | manual intervention |
|---|---|---|---|---|
| **patched** | `C0` (no mint) | auto, incremental, 4 UpToDate | **262144 KB (= W)** | none |
| **stock (bug fired)** | `L` ≠ `C0` (resume-io minted, ROTATED=YES) | auto, incremental, 4 UpToDate | **262144 KB (= W)** | none |

## Honest finding (corrects an over-claim)

**The spurious mint does NOT, by itself, force a larger resync.** When the loser is demoted to
Secondary before heal (which Bedrock's `demote_arbiter_host` / `on-suspended-primary-outdated`
does), the forked generation `L` still reconciles **incrementally** against the majority — the
common ancestor `C0` survives in both bitmap-UUID slots, so DRBD resyncs only the majority's
writes (W = 256 MiB), not the whole device, and reaches UpToDate automatically. Same as patched.

So the heal **resync volume is dominated by the role/heal sequence, not by the UUID fork:**
- Loser **demoted** before heal (`pcount ≤ 1`): incremental `W`, auto — **both** patched and stock.
- Loser **still Primary** at heal (`pcount = 2`): `after-sb-2pri=disconnect` → StandAlone /
  manual — **both** patched and stock (the two-Primary role, not the fork, causes it).

## What the bug's real harm is (and why the patch still matters)

The damage is **not** resync bandwidth. It is the **spurious generation fork itself**: a node that
committed **zero bytes** mints a new data generation while frozen. That is semantically wrong and
creates **split-brain risk** that materialises (StandAlone, manual `--discard-my-data`) in the
less-favourable heal sequences — exactly the two-Primary / no-after-sb / racing-promote cases a
real failover can hit. The original incident landed in such a sequence. The one-line ARM-guard
patch **eliminates the spurious fork at the source** (validated: stock mints 3×, patched 0×), so
the minority always heals from the true common ancestor regardless of the heal sequence.

## Severity, corrected

- **Data: always safe** — the minority commits zero bytes; no committed data ever diverges.
- **Resync volume: bounded** — incremental (~W) in the well-handled (demote) path, not a full
  device; this is *better* than the earlier "full resync" framing.
- **Real cost: operational** — a false generation fork → split-brain handling / manual recovery in
  unfavourable heal sequences, plus the correctness violation of a zero-write node minting.
- Worth fixing: yes (clean one-line patch + the bedrock-d resume-io gating), but it is **not** a
  data-loss or mass-resync emergency.
