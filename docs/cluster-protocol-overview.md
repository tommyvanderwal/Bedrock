# Bedrock cluster protocol — high-level overview

Audience: operator reviewing the design at a glance. ~10 minutes.

This document is the **one place** to understand what the cluster
protocol does and how. It pairs with:
- `cluster-protocol-design.md` — the architectural-decisions rationale
- the code under `rust/bedrock-rust/` and `installer/lib/`

If something here disagrees with the code, the code is wrong. The doc
is the contract.

---

## 1. What the cluster protocol gives you

In one sentence: **At any instant, every node knows whether it is
allowed to keep running, and there is exactly one truth about who
the leader is.**

Concretely:

- **Single-writer log.** Cluster decisions (membership, witness,
  storage tier mode, VM intents, etc.) are typed entries appended
  to a hash-chained, fsynced log. The mgmt master writes; replication
  carries every entry to every other node. Forks are detected by
  hash mismatch and replication refuses to advance past them.
- **Self-fence on lease loss.** A node that cannot establish quorum
  (peers visible + witness reachable) within a TTL takes itself
  off the cluster — writes a fence marker, brings interfaces down,
  reboots. No supervisor needs to intervene.
- **Weighted-vote leader election** at N≥3. Witness is a tiebreaker,
  not a quorum source.

Everything else is plumbing.

---

## 2. The 2 Bedrock services

```
                                            ┌────────────────┐
                                            │     witness    │
                                            │   (ESP32 / SW) │
                                            │   UDP :12321   │
                                            └───────┬────────┘
                                                    │ Echo protocol
                                                    │ (encrypted UDP)
                                                    │
        ┌──────────────────────────────────┐        │        ┌──────────────────────────────────┐
        │            NODE A                │        │        │            NODE B                │
        │                                  │        │        │                                  │
        │  ┌────────────────────────────┐  │        │        │  ┌────────────────────────────┐  │
        │  │  bedrock-rust  (Rust)      │◄─┼────────┴────────┼─►│  bedrock-rust  (Rust)      │  │
        │  │   • log + replicate        │  │                 │  │                            │  │
        │  │   • witness lease (1 Hz)   │  │  TCP :8200      │  │                            │  │
        │  │   • self-fence (pause-only)│◄─┼─────────────────┼─►│                            │  │
        │  │   • role file              │  │                 │  │                            │  │
        │  └─────────────┬──────────────┘  │                 │  └─────────────┬──────────────┘  │
        │                │ /run/*.sock     │                 │                │                  │
        │                │ /run/*.role     │                 │                │                  │
        │  ┌─────────────▼──────────────┐  │                 │  ┌─────────────▼──────────────┐  │
        │  │  bedrock-mgmt   (Python)   │  │                 │  │  bedrock-mgmt   (Python)   │  │
        │  │   ① log subscriber         │  │                 │  │                            │  │
        │  │     fold → cluster.json    │  │                 │  │                            │  │
        │  │     regen daemon.toml      │  │                 │  │                            │  │
        │  │   ② boot orchestrator      │  │                 │  │                            │  │
        │  │     start drbd/libvirtd/VMs│  │                 │  │                            │  │
        │  │   ③ fence responder        │  │                 │  │                            │  │
        │  │     pause VMs, stop NFS,   │  │                 │  │                            │  │
        │  │     unfence, resume        │  │                 │  │                            │  │
        │  │   ④ reactor                │  │                 │  │                            │  │
        │  │     react to log changes   │  │                 │  │                            │  │
        │  │   FastAPI + Svelte :8080   │  │                 │  │                            │  │
        │  └────────────────────────────┘  │                 │  └────────────────────────────┘  │
        │                                  │                 │                                  │
        │  ────  third-party, not auto-started ────           │                                  │
        │  drbd.service       libvirtd.service                │                                  │
        │       (started by ② orchestrator only)              │                                  │
        └──────────────────────────────────┘                 └──────────────────────────────────┘
```

| Component | Role | Failure mode |
|---|---|---|
| **bedrock-rust** | log + replication, witness lease, weighted-vote election, self-fence isolation (`ip link down`, write marker, then idle). Hard real-time. ~3k LOC. Bounded everything; no open-ended work. | Panic → systemd `Restart=on-failure` → re-join. Hang → witness lease expires → self-fence (NICs down + marker). Daemon stays running but idle until mgmt unfences. |
| **bedrock-mgmt** | Single Python process with four asyncio tasks (subscriber + boot + fence-responder + reactor) plus the FastAPI dashboard at :8080. | Crash → systemd restart → orchestrator re-runs boot phase. Hang during fence cleanup → 270s watchdog `systemctl reboot` (safety net only — never fires when cleanup is healthy). |
| **witness** | UDP service that records `(sender_id, last_seen_ms)` per node. Tiebreak signal only — never in the commit path. | Unreachable → election still works between nodes that see each other; ambiguous splits stall (correct: no false leader). |
| **drbd / libvirtd** | Third-party. **Not auto-started at boot.** The mgmt orchestrator's boot phase starts them once cluster contact is confirmed. | Same systemd model — `Restart=on-failure`. mgmt's reactor reacts to relevant log entries (vm_migrated etc.). |

---

## 3. The log

```
   index → 1            2            3            4            …
          ┌───────┐    ┌───────┐    ┌───────┐    ┌───────┐
          │ boot  │───►│cluster│───►│ node  │───►│ tier  │───►  …
          │ strap │    │ init  │    │register│   │ state │
          └───┬───┘    └───┬───┘    └───┬───┘    └───┬───┘
              │            │            │            │
            sha256       sha256       sha256       sha256
            (incl. prev_hash → hash chain)

   Each on-disk frame:
   ┌─────┬───────┬─────────────────┬───────┬──────────┬───────┐
   │idx  │epoch  │ prev_hash (32B) │ kind  │ payload  │ hash  │
   │8B   │8B     │                 │ 1B    │ N bytes  │ 32B   │
   └─────┴───────┴─────────────────┴───────┴──────────┴───────┘
```

Properties:

- **Append-only.** Bytes never change once fsynced. No truncation,
  no rewrite. Recovery after crash = read from disk; the latest
  frame is whatever fsynced last.
- **Hash-chained.** `hash = SHA256(idx || epoch || prev_hash || kind || payload)`.
  Two nodes with the same `(idx, hash)` have byte-identical history
  up to that point. Mismatch → fork → replication halts.
- **Single-writer.** Only the mgmt master appends. Followers replicate
  but never originate entries. This is enforced at two levels:
  - Python (`tier_storage._is_mgmt_master()` gates `_log_append_typed`)
  - Rust (peer.rs detects chain breaks and refuses to apply, surfacing
    the fork via DIVERGENCE log)
- **Typed.** Payload is MessagePack with a `t` tag — `node_register`,
  `tier_state`, `vm_create_intent`, etc. Each type has a constructor
  in `installer/lib/log_entries.py` and a fold rule in
  `installer/lib/view_builder.py`. Rust treats the payload as
  opaque bytes; only Python interprets it.

What lives in the log (current set):

| Lifecycle | Entries |
|---|---|
| Cluster bootstrap | `bootstrap`, `cluster_init` |
| Node membership | `node_register`, `node_unregister`, `mgmt_master`, `node_maintenance_set` |
| Storage | `tier_state`, `drbd_node_id_assigned`, `drbd_node_id_freed` |
| Witness | `witness_register`, `witness_unregister` |
| VMs | `vm_create_intent`, `vm_created`, `vm_create_failed`, `vm_destroyed`, `vm_migrated`, `vm_state_change` |
| Operational | `param_change` |

Add a new entry kind by adding a constructor in `log_entries.py` and a
fold rule in `view_builder.py`. The Rust daemon needs no change.

---

## 4. Replication

```
   master                                     follower
   ──────                                     ────────
   ┌──────────┐                               ┌──────────┐
   │  log     │                               │  log     │
   │  on disk │                               │  on disk │
   └────┬─────┘                               └────▲─────┘
        │ append (single writer)                   │ append (after verify)
        │                                          │
   ┌────▼──────────┐    TCP :8200            ┌─────┴──────────┐
   │ peer-tx loop  │═════════════════════════►│ peer-rx loop  │
   │               │  ReplicateEntry frames   │               │
   │               │  (one per log entry)     │               │
   │               │◄═════════════════════════│               │
   │               │       Ack {up_to}        │               │
   └───────────────┘                          └───────────────┘
        │                                          │
        ▼                                          ▼
   ┌────────────────┐                      ┌────────────────┐
   │ CommitNotifier │                      │ CommitNotifier │
   │ (master locals)│                      │ (replicated)   │
   └────────┬───────┘                      └────────┬───────┘
            │                                       │
            ▼ Subscribe push                        ▼ Subscribe push
   ┌────────────────┐                      ┌────────────────┐
   │ bedrock-watcher│                      │ bedrock-watcher│
   │  on master     │                      │  on follower   │
   └────────────────┘                      └────────────────┘
```

A **link** is one TCP connection. Two nodes typically have **two
links** (each side dials the other) for orthogonality. Replication
flows on whichever link the follower asked from; idle links carry
heartbeats only. TCP keepalive (5 s/3 s/3 retries) tears dead links
down within ~15 s — well under the 2× TTL takeover window.

Per-link state in the registry:

| Field | Updated when |
|---|---|
| `direction` | inbound/outbound at connect |
| `identified_role` | peer's `Identify` frame |
| `latest_index` | peer's `Heartbeat` frame |
| `last_acked_index` | peer's `Ack` frame (after applying our push) |
| `last_frame_ms_ago` | any frame received |

`PeerStatus` IPC op exposes this for `_wait_replicated`.

---

## 5. Witness — the lease

```
          every heartbeat_ms (default 1000 ms):
          ┌───────────────────────────────────────┐
          │                                       ▼
   ┌───────────┐  HEARTBEAT(target=LIST)   ┌──────────────┐
   │   node    │──────────────────────────►│   witness    │
   │ sender_id │                           │              │
   │     N     │◄──────────────────────────│ remembers    │
   └───────────┘  STATUS_LIST {            │ (sender_id,  │
                    [(sid, ms_seen),...]   │  ms_seen,    │
                  }                        │  payload)    │
                                           └──────────────┘
          │
          ▼  parse → witness_seen: HashMap<sid, ms_ago>
          ▼  count peers visible via TCP (peer.rs registry)
          ▼  compute_election(...)
          ▼
   ┌───────────────┐
   │ Election      │  Leader / Follower / NoQuorum
   │ outcome       │
   └───────────────┘
```

The **lease** is implicit: each successful heartbeat resets a clock
(`last_ok`). If the node can't heartbeat any witness for `ttl_ms`
(default 5000 ms) AND can't satisfy quorum from peers alone, it
self-fences. No "leader → witness writes a record" — the witness
just observes; the lease lives in the node's local clock.

---

## 6. Election (the "4.1 votes" rule)

Constants:
- `VOTES_PER_NODE = 10`
- `VOTE_PER_WITNESS = 1`
- `total_votes = (1 + N_other_peers) × 10 + 1`
- `threshold = total_votes / 2 + 1`     (strict majority)

A node's score:
- 10 (self)
- 10 × (TCP-visible peers in MY partition — from peer.rs registry)
- +1 if any witness reachable

Outcomes:

```
   compute_election(...)
       │
       ├── fence marker present?            ──► Follower (overrides everything)
       │
       ├── score < threshold?               ──► NoQuorum  (caller may self-fence after TTL)
       │
       ├── any sid < mine alive at witness? ──► Follower  (yield to lower-id leader)
       │
       └── otherwise                        ──► Leader
```

Worked examples for a 4-node cluster (`total = 41`, `threshold = 21`):

| Scenario | My partition peers | Witness | Score | Outcome |
|---|---|---|---|---|
| Fully connected, I am sender_id 1 | 3 | yes | 41 | Leader |
| Fully connected, I am sender_id 2 | 3 | yes | 41 | Follower (1 alive) |
| 2-2 split, my side has witness | 1 | yes | 21 | Leader (just barely) |
| 2-2 split, my side has no witness | 1 | no | 20 | NoQuorum → self-fence |
| 1-3 split, alone with witness | 0 | yes | 11 | NoQuorum → self-fence |
| 3-1 split, I'm in the 3 with witness, sid 2 | 2 | yes | 31 | Leader (sid 1 in the 1-side, gone) |

Notes:
- "TCP-visible peers in MY partition" means: peers I can reach via
  the cluster cables. The witness's STATUS_LIST is **not** used for
  the count, because the witness sees both sides of a partition.
- "Smaller-id alive" is checked against the witness STATUS_LIST,
  because that's the only way to learn about a peer in another
  partition. After a peer self-fences, it stops heartbeating; the
  witness's `last_seen_ms` ages past the takeover threshold (2× TTL,
  default 10 s); the eligible follower then promotes.

---

## 7. Self-fence (pause-not-shutdown, unfence-or-reboot)

The fence model is **pause + isolate + clean up + unfence**, not
shutdown + reboot. Reboot exists only as a safety net for when the
cleanup itself hangs.

```
   ── Rust side ──────────────────────────────────────────────────────

   lease loop tick
        │
        ▼
   fence_marker_present()?                  (was the daemon already fenced?)
        │
        ├── yes ─► reset clocks; idle. Don't heartbeat, don't run
        │         election, don't try to fence again. Just wait.
        │
        └── no
             │
             ▼
        (normal path: heartbeat all witnesses, run compute_election,
         maybe self_fence on TTL exhaustion)
             │
             ▼ on self_fence:
        1. ip link set <fence_interfaces> down
        2. write /tmp/bedrock-rust.fence (timestamp)
        3. write /run/bedrock-rust.role = "fenced"
        4. continue running                  (NEVER exit, NEVER reboot)
        5. next loop iteration sees marker → idles (above)


   ── Python side (mgmt fence_responder) ─────────────────────────────

   poll for /tmp/bedrock-rust.fence (1 Hz)
        │
        ▼ on appearance:
        within 270 s budget:
            virsh suspend $(virsh list --state-running --name)
            exportfs -au                     (drop our NFS exports)
        │
        ├── budget exceeded ─► systemctl reboot   (safety net only)
        │
        └── cleanup ok
                │
                ▼ unfence:
            ip link set <fence_interfaces> up
            rm /tmp/bedrock-rust.fence
                │
                ▼ wait for bedrock-rust to re-run election
                  and report leader/follower in /run/bedrock-rust.role
                │
                ▼ reconcile paused VMs against the (now-current) log:
            for each paused VM:
                if log says home != us              → virsh destroy   (peer took over)
                if log says home == us, running     → virsh resume    (transient blip; we're back)
                otherwise                           → leave paused
                │
                ▼
            re-run boot orchestrator's start_local_services()
            (start drbd, libvirtd, virsh start VMs that should be here)
```

**What is NOT done in cleanup:**
- We do not `virsh shutdown` (that loses guest state).
- We do not `drbdadm secondary` (DRBD's protocol-C quorum handles that on reconnect).
- We do not `systemctl stop libvirtd` or `systemctl stop drbd` (we care about VMs, not the daemons that manage them — keeping them running gives us instant resume).

**Why this is safe:**

A paused VM does no IO → its DRBD primary is quiet → another node
can promote without dual-primary risk (DRBD's quorum prevents it
from ever becoming dual-primary anyway). When we reconnect, DRBD's
protocol-C reconnect logic decides who's the real primary based on
activity log + quorum. If a peer promoted while we were dark, we
demote and resync; if no one promoted, we stay primary.

**The all-isolated case:**

If a switch failure isolates every node simultaneously, every node
fences, every node pauses its VMs, no one demotes anything, and no
one reboots. When the network comes back, election runs, the same
node that was leader before is leader again (lowest sender_id with
quorum, log indices are equal because nothing was written), and
every paused VM resumes. Total downtime = pause window. No state
lost.

**Why each "skip the fence" branch exists:**

| Lease-loop branch | Reason |
|---|---|
| fence marker present → idle | We've already been fenced this session; mgmt is cleaning up. Anything we do would conflict. |
| witness ok → reset clocks | Witness reachable means tiebreaker available; we're not orphaned. |
| peer in maintenance → reset clocks | Operator put the peer down; silence is expected. |
| peer-quorum without witness → reset clocks | At N≥3, my partition has majority without needing the witness. |
| no peers configured (standalone) → reset clocks | 1-node cluster; witness loss alone shouldn't fence a single-node setup. |
| TTL exhausted + had_success | Genuine lease loss. Fence: isolate + idle. mgmt handles the rest. |

The `had_success` guard prevents startup-during-witness-blip from
fencing immediately on boot.

---

## 8. Operational verbs

```
   bedrock init    ─► writes bootstrap, cluster_init, node_register, mgmt_master, tier_state
                     starts bedrock-rust, bedrock-watcher, bedrock-mgmt locally

   bedrock join    ─► POSTs /api/nodes/register on the master ─► master appends node_register
                     master replies with cluster_uuid + cluster_key
                     joining node initialises its log with the SAME bootstrap entry
                     joining node starts bedrock-rust as Follower, dials master :8200
                     replication catches the joining node up to master's latest_index

   bedrock node leave <name>
                   ─► master appends node_unregister(name, reason)
                     master regenerates daemon.toml from the new snapshot (peer_sender_ids shrinks)
                     replication carries the entry to every peer; their watchers regen + restart
                     master SSHs the leaving node and stops its bedrock services

   bedrock node maintenance <name> on|off
                   ─► master appends node_maintenance_set(name, on/off)
                     replicated; folded by view_builder; renders peer_in_maintenance in
                     daemon.toml on every OTHER node so they don't fence on the planned silence

   bedrock witness add <id> <host:port> <pubkey>
                   ─► master appends witness_register
                     replicated; daemon.toml on every node grows a [[witness]] block;
                     bedrock-rust restarts and starts heartbeating the new witness

   bedrock storage promote     (N=1 → N=2)
                   ─► master converts local LV to DRBD primary, NFS-exports
                     SSHs the peer to run _peer-promote (joins DRBD secondary, NFS-mounts)
                     master appends drbd_node_id_assigned + tier_state via _log_append_typed
                     peer's get_drbd_node_id() runs but log-append is gated off (master-only)
```

All of these are **single-writer** through the master. Followers may
mutate their LOCAL JSON cache (`cluster.json`) for transient/derived
fields, but the canonical log entry is written by the master only.

---

## 9. State machine — what each node is doing

```
              power on
                 │
                 ▼
        ┌─────────────────┐
        │ bootstrap_done? │
        │  (state.json)   │
        └────┬────────┬───┘
             │ no     │ yes
             ▼        ▼
         ┌──────┐   ┌─────────────────┐
         │ wait │   │ cluster_uuid?   │
         │ for  │   │  (state.json)   │
         │ init │   └────┬────────┬───┘
         │ /join│        │ no     │ yes
         └──────┘        ▼        │
                     ┌──────┐     │
                     │ wait │     │
                     │ for  │     │
                     │ init │     │
                     │ /join│     │
                     └──────┘     │
                                  ▼
                          ┌──────────────┐
                          │ run daemons: │
                          │ bedrock-rust │
                          │ -watcher     │
                          │ -mgmt        │
                          └───────┬──────┘
                                  │
                                  ▼
              ┌───────────────────────────────────────┐
              │ lease loop running                    │
              │  every tick:                          │
              │   - heartbeat all witnesses           │
              │   - compute_election()                │
              │   - set last_election (Leader/Follower│
              │     /NoQuorum)                        │
              └─────────┬─────────────────────────────┘
                        │
              ┌─────────▼──────────┐
              │ healthy? (witness  │
              │ ok OR peer fresh   │
              │ OR maintenance     │
              │ OR peer-quorum     │
              │ OR standalone)     │
              └─┬──────────────┬───┘
                │ yes          │ no  (and TTL elapsed and had_success)
                │              ▼
                │       ┌──────────────┐
                │       │  self-fence  │
                │       │  exit daemon │
                │       └───────┬──────┘
                │               │ (300 s cleanup window, then)
                │               ▼
                │       ┌──────────────┐
                │       │   reboot     │
                │       └──────────────┘
                │
                └── back to lease loop
```

---

## 10. Failure-mode table — what happens when X breaks

| What breaks | Cluster outcome |
|---|---|
| Master mgmt-app crashes | Reads still work on followers (their cluster.json is fresh from log replication). Writes fail until master mgmt-app restarts. No fencing. |
| Master bedrock-rust crashes (no fence) | systemd restarts it. Reconnects to peer + witness within seconds. Log replication resumes. Election re-runs. |
| Master node hard-crashes | Witness ages out the master's STATUS_LIST entry after takeover threshold. Highest-priority surviving node with quorum promotes to Leader on next tick. |
| Follower bedrock-rust crashes | systemd restarts it. Pulls missed log entries from master via ReplicateRequest. |
| Follower node hard-crashes | Master keeps writing. Replication retries every 2 s. Catches up when peer is back. |
| Witness unreachable, peer fresh | Both nodes keep running. Election holds last value. Reconnect on next witness reachable. |
| Witness unreachable, peer also gone | TTL ticks down. Self-fence at TTL on a node with no quorum. |
| Network partition 2-2 with witness on one side | Side WITHOUT witness has score 20/41 → NoQuorum → self-fences. Side WITH witness has 21/41 → Leader. |
| Network partition 2-2 with NO witness anywhere | Both sides score 20/41 → both self-fence. Cluster halts. (Correct: no signal to break the tie.) |
| Network partition 1-3 | Singleton has score ≤11 → fences. The 3 has 31 → Leader. |
| Log fork (would happen if a follower wrote) | peer.rs DIVERGENCE log; replication refuses to apply across the fork. **The single-writer gate prevents this from happening.** |
| bedrock-watcher crashes | systemd restarts. Catches up via Read, re-Subscribes. cluster.json may be stale during the restart window; daemon.toml unchanged so bedrock-rust unaffected. |

---

## 11. What the protocol does NOT do

By deliberate omission:

- **Locks, mutexes, leader-only RPCs.** The master is identified by
  the election outcome; all writes go through the master's mgmt API.
  No distributed lock manager.
- **Multi-writer.** No.
- **Byzantine tolerance.** Nodes are trusted within the cluster
  (cluster_key authenticates witness traffic, SSH keys authenticate
  peer-to-peer admin). A malicious node is out of scope.
- **Auto-recovery from divergence.** If the chain forks (single-writer
  bug), the daemon stops applying. An operator must reset the bad
  follower's log dir manually.
- **Continuous reads from a follower for writes.** Writes go to the
  master's API. The dashboard on a follower can show data; mutations
  go to the master.
- **Delayed/batched commits.** Each append is fsynced before the API
  returns the index. Synchronous on purpose — the log is the
  durability boundary.

---

## 12. How the pieces fit on disk

```
   /etc/bedrock/
      cluster.key                    32 raw bytes, mode 0600
      cluster.json                   <- watcher rewrites on every fold
      state.json                     <- this node's POV (role, mgmt_url)
      daemon.toml                    <- generated from snapshot; bedrock-rust reads
      installer.env                  <- BEDROCK_REPO=...

   /var/lib/bedrock/
      log/                           <- segment files, append-only
         segment-00000.log
         ...

   /tmp/bedrock-rust.fence           <- fence marker (tmpfs; auto-cleared on reboot)
                                        Cleared by mgmt's fence_responder
                                        after cleanup completes.

   /run/bedrock-rust.sock            <- IPC socket (mode 0600)
   /run/bedrock-rust.role            <- single line: leader|follower|noquorum|fenced
                                        Updated by Rust on every election change;
                                        polled by mgmt's boot orchestrator.

   /opt/bedrock/mgmt/                <- FastAPI app + Svelte UI build
      app.py
      orchestrator.py                <- subscribe + boot + fence-respond + reactor
      ui/build/...                   <- served at http://<this-node>:8080/

   /usr/local/bin/
      bedrock                        <- CLI
      bedrock-rust                   <- Rust daemon (static-musl)

   /etc/systemd/system/
      bedrock-rust.service           <- Restart=on-failure (never exits in fence path)
      bedrock-mgmt.service
      bedrock-vm.service             <- master only (VictoriaMetrics)
      bedrock-vl.service             <- master only (VictoriaLogs)

   Third-party, NOT auto-started — orchestrator decides:
      drbd.service                   <- systemctl disable'd at install
      libvirtd.service               <- systemctl disable'd at install
```

---

## 13. Data flow on a typical write

```
   user → curl POST /api/vms/create  (vm_type=pet)
        │
        ▼  bedrock-mgmt on master (FastAPI)
        │
        │  validate vm_type vs N_nodes
        │  build_cluster_state() check for duplicate name
        │
        ▼  rust_ipc.Daemon.append(vm_create_intent)
        │
        ▼  bedrock-rust: write frame to log (fsync), notify CommitNotifier
        │
        ├─► return idx,hash to mgmt → mgmt returns 200 + intent_log_index to user
        │
        └─► CommitNotifier:
              ├─► local subscribers (master's own watcher)
              └─► peer-tx threads → ReplicateEntry over TCP :8200
                                        │
                                        ▼  followers' bedrock-rust
                                        │  verify chain → fsync → notify
                                        │
                                        ▼  followers' watchers
                                        │  fold_into snapshot
                                        │  write cluster.json + state.json
                                        │  render daemon.toml
                                        │  if daemon.toml changed → systemctl restart bedrock-rust

   meanwhile (async on master):
        ▼  _vm_create_replicated() → lib.vm._create_pet
        │  — allocates DRBD vol on both nodes via SSH
        │  — defines libvirt domain on both
        │
        ▼  rust_ipc.Daemon.append(vm_created)
              [same flow as above]
```

The intent → created split is what makes crash recovery clean: if
the master crashes between the intent and the created, the next
startup sees a vm_create_intent without a matching vm_created or
vm_create_failed, and Python decides to resume or roll back.

---

## 14. Where to look in code

| Behavior | File |
|---|---|
| Frame format, hash chain | `rust/bedrock-rust/src/log_store.rs` |
| Append + fsync, replication | `rust/bedrock-rust/src/peer.rs` |
| IPC ops + Subscribe stream | `rust/bedrock-rust/src/ipc.rs` |
| Witness lease, election, fence (pause-only) | `rust/bedrock-rust/src/witness.rs` |
| Daemon config schema | `rust/bedrock-rust/src/config.rs` |
| Single-writer log gate | `installer/lib/tier_storage.py::_is_mgmt_master + _log_append_typed` |
| Typed entry constructors | `installer/lib/log_entries.py` |
| Snapshot fold (replay log → cluster.json) | `installer/lib/view_builder.py` |
| Subscribe + boot + fence-respond + reactor | `mgmt/orchestrator.py` |
| daemon.toml projection from snapshot | `installer/lib/daemon_setup.py::render_from_snapshot` |
| `bedrock node leave` | `installer/bedrock::_cmd_node_leave` |
| Mgmt API + dashboard | `mgmt/app.py`, `mgmt/ui/src/...` |

---

## 15. Constants you might tune

| Knob | Default | Where | Effect |
|---|---|---|---|
| `lease_ttl_ms` | 5000 | daemon.toml | How long a lease lasts. Lower = faster failover, more sensitive to jitter. |
| `heartbeat_ms` | 1000 | daemon.toml | How often we hit the witness. Lower = faster detection, more witness load. |
| takeover_threshold | 2× ttl | hard-coded | How long the witness must not have heard from a peer before we treat it as down. |
| `VOTES_PER_NODE` | 10 | witness.rs constant | Per-node vote weight. |
| `VOTE_PER_WITNESS` | 1 | witness.rs constant | The "+1 tiebreaker" weight. |
| TCP keepalive | 5s/3s/3 | peer.rs | How fast a silent partition tears down dead links. |
| RECV_TIMEOUT (witness UDP) | 4s | witness.rs constant | UDP recv blocks at most this long. retry-once on timeout. |
| HEALTHY_TICKS_TO_CLEAR_MARKER | 30 | witness.rs constant | Healthy ticks (witness+peer ok) before auto-clearing a stale fence marker. |

If you find yourself wanting to tune anything else, the doc is
incomplete — call it out, not the code.

---

## 16. Mental model in one paragraph

The cluster is a **single ordered list of typed events** (the log)
and a **bunch of derivations from it** (cluster.json, state.json,
daemon.toml, the dashboard's view, the per-link state). Exactly one
process at any time may append to the log: the mgmt master. Every
other node tails the log over a TCP cable and rebuilds the same
derivations locally — byte-for-byte identical because the fold is
deterministic. When two nodes can't see each other, the witness
casts the deciding vote about which side is allowed to keep
appending. When neither side has the witness, both sides take
themselves off the cluster rather than accept ambiguity. That's
the whole protocol.
