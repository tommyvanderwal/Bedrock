# DRBD Internals — How Replication Stays Fast and Crash-Safe

## The Problem

Replicating every disk write to a second node sounds expensive.
Naive approaches either:
- **Double write I/O** (write-ahead log on both nodes) — kills performance
- **Skip logging** — fast, but lose data on crash
- **Periodic sync** (rsync-style) — stale data between syncs

DRBD solves this with two elegant mechanisms:
**Generation UUIDs** for versioning, and an **Activity Log** for crash recovery.
Together they add near-zero overhead during normal operation.

## Generation UUIDs — Zero-Cost Version Tracking

DRBD does NOT use timestamps, write counters, or sequence numbers.
It uses **UUIDs that only change when the cluster topology changes**.

```
  Each node stores 4 UUIDs per resource:

  +--< Current UUID >-----  Identifies THIS generation of data
  |
  +--< Bitmap UUID >------  Base generation for the dirty bitmap
  |
  +--< Younger history >--  Previous generation (one back)
  |
  +--< Older history >----  Generation before that


  Example (both nodes healthy, in sync):

  Node1: E54D4BD7B3D1FDD4 : 0000000000000000 : 0000000000000000 : 0000
  Node2: E54D4BD7B3D1FDD4 : 0000000000000000 : DC2376817A79C2A4 : 0000
         ^^^^^^^^^^^^^^^^
         Same Current UUID = same data generation
```

### What happens during normal writes

```
  Write #1        Write #2        Write #1000
     │               │               │
     ▼               ▼               ▼
  ┌──────────────────────────────────────┐
  │  UUID stays the same.               │
  │  Zero metadata writes.              │
  │  Zero overhead.                     │
  │                                     │
  │  As long as both nodes are in sync, │
  │  the UUID never changes.            │
  └──────────────────────────────────────┘
```

### What happens when a node crashes

```
  Time ──────────────────────────────────────────────────►

  T0: Both nodes in sync
      Node1 Current: AAAA    Node2 Current: AAAA

  T1: Node2 crashes (power failure)
      Node1 detects peer loss
      Node1 generates NEW UUID: BBBB
      Node1 moves AAAA to history
      Node1 continues writing with UUID BBBB

      Node1 Current: BBBB    Node2 Current: AAAA (frozen on disk)
      Node1 History:  AAAA

  T2: Node2 boots back up
      DRBD compares UUIDs:

      Node1: Current=BBBB  History=AAAA
      Node2: Current=AAAA

      Match found: Node2.Current == Node1.History
      → Node2 is BEHIND Node1
      → Resync from Node1 to Node2
      → Direction is AUTOMATIC and CERTAIN
```

### What happens when BOTH nodes crash simultaneously

```
  T0: Both nodes in sync
      Node1 Current: AAAA    Node2 Current: AAAA

  T1: Power outage — BOTH nodes die at the same time
      Neither node generates a new UUID (no time to)

  T2: Both boot up
      DRBD compares:

      Node1: Current=AAAA
      Node2: Current=AAAA

      UUIDs MATCH → data is identical → no resync needed
      Either node can be Primary
```

### Why this is brilliant

```
  ┌────────────────────────────────────────────────────┐
  │                                                    │
  │  UUID rotation cost:  ONE write, ONCE, only when   │
  │  the cluster topology changes (peer joins/leaves)  │
  │                                                    │
  │  Normal operation:    ZERO extra writes             │
  │  Secondary node:      ZERO extra writes             │
  │  Both nodes in sync:  ZERO extra writes             │
  │                                                    │
  │  Compare to journaling/WAL approaches:              │
  │    - Write-ahead log: 1 extra write PER write      │
  │    - On BOTH nodes: 2x total write amplification    │
  │    - DRBD: 0x write amplification                  │
  │                                                    │
  └────────────────────────────────────────────────────┘
```

## Activity Log — Crash Recovery Without Full Resync

The UUID tells DRBD WHO has newer data. But after a crash,
DRBD also needs to know WHICH blocks might be out of sync.

The Activity Log (AL) is a small, fixed-size structure that tracks
which 4MB disk regions have been recently written to.

### Structure

```
  Activity Log (stored in DRBD metadata area on disk)

  ┌─────────────────────────────────────────┐
  │  Slot 0:    extent #500   (4MB region)  │
  │  Slot 1:    extent #501                 │
  │  Slot 2:    extent #742                 │
  │  Slot 3:    extent #743                 │
  │  ...                                    │
  │  Slot 1236: extent #500                 │
  │                                         │
  │  Default: 1237 slots × 4MB = 4.8GB      │
  │  Circular buffer — oldest evicted first  │
  └─────────────────────────────────────────┘
```

### How writes interact with the AL

```
  Write arrives for block in extent #500

  ┌─────────────────────────────────────┐
  │  Is extent #500 already in the AL?  │
  └──────┬───────────────────┬──────────┘
         │                   │
        YES                  NO
         │                   │
         ▼                   ▼
  ┌──────────────┐   ┌─────────────────────────┐
  │ Write data   │   │ 1. Write AL entry to    │
  │ directly.    │   │    disk (one 4K I/O)    │
  │              │   │ 2. Flush disk           │
  │ ZERO extra   │   │ 3. THEN write data      │
  │ I/O cost.    │   │                         │
  └──────────────┘   │ One-time cost per new   │
                     │ 4MB extent              │
                     └─────────────────────────┘
```

### Real-world overhead — measured on this cluster

```
  vm-test (Linux VM, 10GB disk):

  Total data writes:   96,388
  AL metadata writes:       21
  Ratio:               1 AL write per 4,590 data writes

  That is 0.02% overhead.

  Why so low?  VM workloads have strong locality.
  The OS, applications, temp files, and databases all
  write to the same disk regions repeatedly. Those
  regions stay in the AL and never cause extra I/O.
```

### On crash recovery

```
  Node crashes with dirty AL containing extents:
  [500, 501, 742, 743, ... up to 1237 entries]

  On restart:

  ┌───────────────────────────────────────────────┐
  │  DRBD reads the AL (one sequential read)      │
  │  Marks those extents as "possibly dirty"      │
  │  Resyncs ONLY those extents to the peer       │
  │                                               │
  │  Worst case: 1237 × 4MB = 4.8 GB to resync   │
  │  On a 500GB disk, that is < 1%                │
  │                                               │
  │  Compare to: full resync = 500 GB             │
  │  Savings: 99%+ of resync data avoided         │
  └───────────────────────────────────────────────┘
```

## The Bitmap — Block-Level Dirty Tracking

Below the AL, DRBD also maintains a **bitmap** — one bit per 4KB block
of the entire disk. When the peer is disconnected, every write sets the
corresponding bit. On reconnect, only the dirty bits need resyncing.

```
  Layer     Granularity    Purpose
  ──────────────────────────────────────────────────
  UUID      Whole disk     Who has newer data?
  AL        4MB extents    Which regions were hot at crash?
  Bitmap    4KB blocks     Exactly which blocks changed?
```

The three layers work together:

```
  After crash:
  1. UUID comparison → direction of resync (who is source?)
  2. AL → which 4MB regions to check (skip 99% of disk)
  3. Bitmap → within those regions, which 4KB blocks? (skip more)

  After clean disconnect + reconnect:
  1. UUID comparison → direction
  2. Bitmap only → exactly which blocks changed while apart
     (AL not needed — shutdown was clean)
```

## Cold Boot Decision Matrix

```
  ┌─────────────────────────────────────────────────────────┐
  │ Scenario               │ UUID state    │ Action         │
  ├─────────────────────────────────────────────────────────┤
  │ Both nodes clean       │ Match         │ No resync      │
  │ shutdown, no writes    │               │ Either = Primary│
  │                        │               │                │
  │ Both nodes crash       │ Match (no     │ AL-based resync│
  │ simultaneously         │ time to       │ (worst 4.8GB)  │
  │                        │ rotate)       │ Either = Primary│
  │                        │               │                │
  │ One node crashed,      │ Survivor has  │ Bitmap resync  │
  │ other continued        │ newer UUID    │ from survivor  │
  │ writing                │               │ to crashed node│
  │                        │               │                │
  │ One node crashed,      │ UUIDs match   │ No resync      │
  │ other was idle         │ (no writes    │ needed         │
  │ (Secondary, no writes) │ happened)     │                │
  │                        │               │                │
  │ Split-brain (both      │ Both have     │ CONFLICT       │
  │ wrote independently)   │ different     │ Needs policy   │
  │                        │ new UUIDs     │ (discard one)  │
  └─────────────────────────────────────────────────────────┘
```

## Summary — Why DRBD's Design Is Exceptional

```
  ┌────────────────────────────────────────────────────────┐
  │                                                        │
  │  Normal writes:     0 extra I/O  (UUID unchanged)     │
  │  Secondary node:    0 extra I/O  (no AL, no journal)  │
  │  New 4MB extent:    1 extra I/O  (AL update, rare)    │
  │  Topology change:   1 extra I/O  (UUID rotation)      │
  │  Crash recovery:    < 1% resync  (AL limits scope)    │
  │  Clean reconnect:   bitmap only  (exact dirty blocks) │
  │                                                        │
  │  Total write amplification: effectively 0x             │
  │  Data integrity guarantee: Protocol C (synchronous)    │
  │  Recovery certainty: 100% (UUID history is definitive) │
  │                                                        │
  │  20+ years of production use. Real engineering.        │
  │                                                        │
  └────────────────────────────────────────────────────────┘
```
