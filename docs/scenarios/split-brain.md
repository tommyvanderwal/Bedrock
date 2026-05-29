# Scenario: DRBD split-brain

Both sides of a DRBD resource went Primary (accepted writes) while
disconnected from each other. Their disks hold divergent extents and DRBD
refuses to auto-resync — there is no single source of truth.

## Default policy: most splits auto-resolve

Every Bedrock DRBD resource carries this `net {}` policy
(`bedrock_d/vm/drbd_config.py`, `installer/lib/tier_storage.py`):

```
protocol C;
allow-two-primaries  no;
after-sb-0pri  discard-zero-changes;   # no writes diverged → keep the one that did
after-sb-1pri  discard-secondary;      # one Primary → it wins, Secondary rolls back
after-sb-2pri  disconnect;             # BOTH were Primary → stop, wait for operator
```

So a split needs **manual** intervention only when both sides were Primary
at the time they diverged (`after-sb-2pri disconnect`). The 0pri/1pri cases
DRBD heals itself on reconnect. Everything below is the 2-primaries case.

## How it happens

Single-primary plus the weighted-vote quorum makes two-live-primaries rare,
not impossible. Paths that can produce one:

- **Manual force-promote**: operator runs `drbdadm primary --force` on a
  Secondary whose peer is actually still alive and writing.
- **Witness mis-wired**: the witness biases failover toward "don't promote"
  (see below), but a witness in the same fault domain as the node it watches
  can fail together with it, removing that bias.
- **Migrate aborts at the wrong instant**: the migrate saga sets
  `allow-two-primaries=yes` on source and target for the live-handoff window.
  If the saga aborts mid-window **and** the link drops **and** the operator
  manually promotes elsewhere, both sides can diverge.

Why two-primaries is hard to hit by failover alone: a survivor promotes only
with a vote majority where `node = 100, witness = 1`. A witness counts only
when reachable AND reflecting our write; an invalid one *raises* the bar.
A lone survivor without majority returns NoQuorum and does not promote.

## Detection

`drbdadm status` on the affected nodes:

```
vm-foo-disk0 role:Primary
  disk:UpToDate
  <peer> connection:StandAlone   (or Connecting that never completes)
```

Kernel log (both sides):

```
drbd vm-foo-disk0/0: Split-Brain detected but unresolved, dropping connection!
drbd vm-foo-disk0 <peer>: self-D0A1B2C3... peer-E4F5A6B7...   (mismatched generation UUIDs)
```

The dashboard DRBD tile shows the peer as `StandAlone` (not Secondary) and
does not sync. Bedrock collects this every 3 s: the mgmt master SSHes each
node, runs `drbdadm status`, and `parse_drbd_status` (`mgmt/app.py`) feeds
the tile. There is no dedicated split-brain dashboard event; grep the kernel
log on each node:

```bash
journalctl -k --since '1 hour ago' | grep -i 'split-brain\|drbd'
```

## Rules before resolving

Recovery **discards data on one side**. The operator picks the winner.

1. **The current VM Primary wins** — it is taking live writes; losing them
   is visible at the application level, whereas the stale side's lost writes
   usually are not.
2. **Both Primary and unsure** → stop the VM on one side first
   (`virsh destroy`) before touching DRBD, so you never truncate a disk out
   from under a running VM.
3. **Precious workload** → copy the losing side off first. Protocol-C only
   ACKs on durable peer write, but a split means one side holds writes the
   other never saw; if those are business-critical, extract them before
   discard.

## Resolution — 2-way (Pet), keep Primary

node1 holds the live VM; node2 has diverged stale writes.

```bash
# LOSER (node2) — about to discard its divergence
ssh node2 '
  drbdadm secondary vm-foo-disk0              # if it was Primary
  drbdadm disconnect vm-foo-disk0
  drbdadm -- --discard-my-data connect vm-foo-disk0
'

# WINNER (node1)
ssh node1 'drbdadm connect vm-foo-disk0'

# DRBD resyncs node1 → node2; watch:
ssh node1 'drbdadm status vm-foo-disk0'
# SyncSource/Inconsistent → SyncSource/UpToDate.
```

`--discard-my-data` tells the loser "drop my divergence, take the winner's
version". Correct flag for the standard case.

## Resolution — 3-way (ViPet)

Two losers, one winner. Apply discard-my-data to each loser independently:

```bash
ssh loser1 'drbdadm -- --discard-my-data connect vm-foo-disk0'
ssh loser2 'drbdadm -- --discard-my-data connect vm-foo-disk0'
ssh winner 'drbdadm connect vm-foo-disk0'
```

If two of three diverged from one Primary, promote the side with the
operator-verified newest data and discard-my-data on the other two.

## Resolution — both sides have value

Two Primaries accepted writes the operator cannot lose (e.g. database writes
on both sides during a partition that then healed). There is no automated
merge.

1. Stop both VMs (`virsh destroy` on each host).
2. Mount each side read-only:
   ```bash
   drbdadm secondary vm-foo-disk0
   mount -o ro /dev/<underlying-LV> /mnt/foo-sideA
   ```
3. `rsync` / `diff` each side's unique data to a neutral host.
4. Pick a winner; apply the discard-my-data resolution above.
5. Re-apply the loser's unique data to the restarted VM from the extracted
   files.

Database-admin territory, not DRBD's to solve.

## Prevention

- Run a witness. It biases the weighted vote toward not promoting without a
  clear majority, which keeps a partitioned survivor from going Primary.
- Avoid `drbdadm primary --force` on a live cluster; prefer the
  witness-driven promote.
- The migrate saga restores `allow-two-primaries=no` on both peers after the
  handoff (see [`../actions/vm-migrate.md`](../actions/vm-migrate.md)). If you
  customise the migrate flow, preserve that final tighten.
- A stable DRBD ring on a dedicated link (direct cable or VLAN), addressed on
  the node loopback `/32`, makes partitions rare.

After resolution, the next 3 s push flips the dashboard DRBD tile from
`StandAlone` to `SyncSource` / `SyncTarget` and on to `UpToDate`.
