# Scenario: DRBD split-brain

Both sides of a DRBD resource went Primary (accepted writes) while
disconnected from each other. Their disks now have divergent extents
and DRBD refuses to auto-resync — there is no single source of truth.

## How it happens in Bedrock

Bedrock's default config minimises split-brain risk but does not
eliminate it. Paths that can lead to one:

- **Misconfigured manual promote**: operator runs `drbdadm primary
  --force` on a Secondary that thinks its peer is dead, while the peer
  is actually still running and accepting writes (e.g., across a
  network partition that wasn't witnessed correctly).
- **Witness lost during failover**: the 2-of-3 quorum check should
  prevent this, but a mis-wired witness (e.g., on the same power
  domain as the node it's supposed to watch) removes the safety net.
- **Two-primaries during migrate**: the migrate path sets
  `allow-two-primaries=yes` on both nodes for the duration of the
  handoff. If the migrate aborts mid-way **and** the network drops
  **and** the operator manually promotes elsewhere, the two sides can
  diverge.

## Detection

`drbdadm status` on the affected nodes shows:

```
  vm-foo-disk0 role:Primary
    disk:UpToDate
    <peer> connection:StandAlone (or Connecting that never completes)
```

Kernel log (both sides):

```
  drbd vm-foo-disk0/0: Split-Brain detected but unresolved, dropping connection!
  drbd vm-foo-disk0 <peer>: self-D0A1B2C3... peer-E4F5A6B7...  (mismatched generation UUIDs)
```

The dashboard DRBD tile shows peer as `StandAlone` (not Secondary) and
does not sync.

## Rules before resolving

Split-brain recovery **discards data on one side**. The operator must
choose the winner — the side whose data gets kept. Guidelines:

1. **The current VM Primary wins** — it's accepting live writes; losing
   them is visible (application level) whereas losing the other side's
   stale writes is usually invisible.
2. **If both are Primary and unsure**: stop the VM on one side first
   (`virsh destroy`) before doing anything to DRBD — you do not want to
   truncate the VM's disk underneath it.
3. **Back up first if the workload is precious**. Even though
   protocol-C DRBD only ACKs on durable peer write, split-brain means
   one side has writes the other doesn't; if those writes are business-
   critical they should be copied off the losing side before discard.

## Resolution — standard case (keep Primary, overwrite Secondary)

Assume node1 holds the live VM, node2 has diverged stale writes.

```bash
# On the LOSER (node2) — we are about to nuke its divergent writes
ssh node2 '
  drbdadm secondary vm-foo-disk0              # if it was Primary
  drbdadm disconnect vm-foo-disk0
  drbdadm -- --discard-my-data connect vm-foo-disk0
'

# On the WINNER (node1)
ssh node1 '
  drbdadm connect vm-foo-disk0
'

# DRBD now resyncs node1 → node2; watch progress:
ssh node1 'drbdadm status vm-foo-disk0'
# Should progress from SyncSource/Inconsistent to SyncSource/UpToDate.
```

`--discard-my-data` tells the loser "throw away my divergence, accept
the winner's version". This is the correct flag for the standard case.

## Resolution — 3-way (ViPet) split-brain

Two losers, one winner. Apply the same procedure to each loser
independently:

```bash
ssh loser1 'drbdadm -- --discard-my-data connect vm-foo-disk0'
ssh loser2 'drbdadm -- --discard-my-data connect vm-foo-disk0'
ssh winner 'drbdadm connect vm-foo-disk0'
```

If two of three sides diverged from a single primary (unusual), promote
the one with the operator-verified newest data and discard-my-data on
the other two.

## Resolution — both sides have value

Rare but possible: two Primaries accepted writes the operator cannot
afford to lose (e.g., database writes on both sides during a partition
that healed). There is no automated merge.

Procedure:

1. Stop both VMs (`virsh destroy` on each host).
2. Mount each side's disk read-only:
   ```bash
   drbdadm secondary vm-foo-disk0
   mount -o ro /dev/<underlying-LV> /mnt/foo-sideA
   ```
3. `rsync` / `diff` as needed to extract each side's unique data to a
   neutral host.
4. Pick a winner, apply the discard-my-data resolution above.
5. Manually re-apply the loser's unique data to the restarted VM from
   the extracted files.

This is database-admin territory, not DRBD's problem to solve.

## Prevention

- Always run a witness. The failover orchestrator's 2-of-3 quorum
  prevents promotion without majority agreement.
- Never `drbdadm primary --force` on a live cluster except in genuine
  emergencies (and prefer the witness-driven promote).
- Convert paths in Bedrock already set `allow-two-primaries=no` after
  a successful migrate (see [`../actions/vm-migrate.md`](../actions/vm-migrate.md))
  — if you customise the migrate flow, preserve this.
- Stable network is worth the investment: the DRBD ring (10.99.0.x) on
  a dedicated physical link (direct-cable or VLAN) makes partitions
  rare.

## Log lines the operator can grep for

Bedrock does not today emit a dashboard event for split-brain detection
(follow-up: parse `journalctl -k` for `drbd.*Split-Brain` and push_log
it). For now:

```bash
# on each node:
journalctl -k --since '1 hour ago' | grep -i 'split-brain\|drbd'
```

After resolution, the next state_push_loop tick (≤ 3 s) flips the
dashboard DRBD tile from `StandAlone` to `SyncSource` / `SyncTarget`
and eventually `UpToDate`.
