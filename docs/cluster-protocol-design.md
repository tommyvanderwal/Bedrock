# Bedrock — Cluster Operations & State Design

This document captures the architectural decisions confirmed in design discussion. It states **what** to build and **why**, and stays light on **how** — the existing Bedrock code base and Claude Code working sessions will fill in implementation details. The goal is to keep architecture coherent across implementation work without prematurely fixing details that should be decided in code.

Every item here is a confirmed decision, not a proposal. New ideas should be discussed and confirmed before being added.

---

## 1. Component model

Three processes participate per node, plus a witness on a separate device:

- **bedrock-rust** — hot-path daemon. Lease bewaking, heartbeat, log append + replicate, self-fence trigger. Dumb but reliable. Survives Python crashes. Does not interpret payloads, does not make recovery decisions.

- **bedrock-python** — management plane. FastAPI REST API, orchestration logic, materialized state views, recovery decisions. Smart but allowed to fail. A Python crash does not trigger cluster failover.

- **bedrock-witness** — separate device (ESP32 - Echo protocol). Not in the commit path; consulted on leader-claim.

Asymmetric responsibility is the defining principle: Rust is configured by Python and acts within those parameters; Python decides policy but cannot interfere with the timing-critical loop.

Both Python and Rust ship as one Bedrock version, installed together. No protocol versioning for now; breaking changes go through the leader-only-mode upgrade path.

---

## 2. Process language choices

Rust for the daemon because:
- No GC pauses in the lease-renewal loop
- Deterministic timing on commodity hardware with mlockall + monotonic clock + RT priority
- Small static binary footprint (matters for the witness on MikroTik)
- Same language as the witness and DRBD wire-protocol re-implementation

Python for management because:
- Productive for the orchestration logic (recovery profiles, parameter adjustment, cluster transitions)
- FastAPI gives us a clean REST surface
- A management-tempo language is the right level for management-tempo work

The same Python REST API runs identically on every node. Most commands are forwarded to the leader; 

---

## 3. Log as single source of truth

A single append-only log per cluster, replicated between nodes by Rust. The log is canonical for everything that needs cluster-wide consistency:

- Cluster configuration (parameters, node membership, witness configuration, keys)
- Service definitions and rules (anti-affinity, etc.)
- Task state transitions (live migration started/completed, VM placement changes)
- Anything else that "must be the same on every node"

Materialized views (in Python) are derived from the log and queryable. The log is canonical; views are caches and can be rebuilt by replay.

at keast Two categories of entries share the same format:
- **Rules**: latest version per key wins (current state matters)
- **Facts/events**: append history (sequence matters)
state for the cluster is probably per key wins / state

The distinction is in how Python materializes them, not in the log format.

The log replaces the need for separate config replication, separate task tracking, and separate state stores.

---

## 4. Hash chaining

Each log entry is hash-chained (SHA-256) to the previous entry. Two reasons:

- **Divergence detection between peers**: if two nodes report different hashes for the same `(index, epoch)`, history has diverged and the cluster must refuse to make progress until resolved. 
- **Witness echo**: the witness records `(epoch, last_committed_index, last_committed_hash)` per node from periodic heartbeats. This adds a third independent observation point for divergence detection at leader-claim.


The first entry of a fresh cluster is a deterministic bootstrap message containing the cluster UUID — the literal payload `"Hello World! <cluster-uuid>"`. This makes a re-initialized cluster distinguishable from a continued one.

---

## 5. Witness model

The witness is a passive sensor. Per heartbeat, each node sends `(epoch, last_committed_index, last_committed_hash)` to the witness. The witness keeps the most recent tuple per node in memory only — no persistence across restart, state bootstraps from nodes on reconnect.

The witness is consulted at leader-claim time, not in the commit path. A node wanting to become leader queries the witness for what it has last seen from the peer and refuses to promote if its own state is stale.

Multiple witnesses are supported up to 15. The default deployment for internet-witness configurations is 3-of-5 (majority of 5). Local witness deployments may use a single witness. Each witness has its own cluster-specific key.

A future fileshare witness mode is planned; the design must keep witness-type pluggable.

Internet-witness failover-time budget is set explicitly per cluster — a cluster using high-latency internet witnesses runs with a longer TTL and accepts longer failover times. This is a deliberate operator choice, not a quality difference.

---

## 6. Lease-based leadership and self-fence

Leadership is a time-bounded lease at the witness. The leader renews periodically; if it cannot renew, it must self-fence before the lease can be considered expired by the witness.

Self-fence sequence:
1. Bring **all** cluster interfaces down. No exceptions, no "keep mgmt up" — a fenced node knows nothing.
2. Persist a fence marker to tmpfs.
3. Signal Python to clean up.
4. Wait up to **300 seconds** for Python to do its work (kill VMs, secondary all DRBD, release resources).
5. Reboot the node when Python finished or the timeout expired - 5 minutes

The 300-second window is generous because the node is already considered out of the cluster from the moment the lease expires. Giving Python a real chance to clean up reduces post-reboot recovery work without affecting cluster safety.

Boot after fence (or any reboot): the node comes up in secondary-only state, accepts log replication, and only claims leadership if Python decides the conditions are met (peer reachable or witness-confirmed peer-down with this node having the highest known committed index). A booting node never unilaterally evicts its peer.

There is no watchdog in V1. Self-fence-on-deadline plus the eventual reboot is the V1 protection. Hardware watchdogs are a later addition.

---

## 7. Network topology

Pre-V1 supported topology: two nodes connected by **at least one** direct cable, ideally two (RJ45 + USB4 for orthogonality across PHY/driver/connector). 

Each cable is a separate Rust-managed transport. Heartbeat is sent over all available links; receiving on any link counts as peer-alive. Log replication uses one preferred link with fallback to the others on TCP error.

The python stack must also know all paths between nodes. So any rest-api on any node can always deliver that message to the Leader - master. 

---


## 8. Snapshots, backups, log compaction

Snapshots are periodic materialized-state dumps written by Python. They serve two purposes:
- Allow Rust to compact (drop) old log entries that are now covered by the snapshot
- Form the basis of cluster backups (snapshot file plus log entries since the snapshot's index)

Snapshot serialization uses MessagePack with an explicit version envelope and a Python dataclass schema. JSON conversion is available for debugging and inspection.


---

## 9. Cluster transitions and parameter changes

Cluster-level parameter changes (TTL adjustment, node addition/removal, witness changes) go through a leader-only-mode transition: drain to one node as temporary single source of truth, change parameters, re-expand with new parameters. This serializes parameter changes by design, eliminating concurrent-state-during-transition.

Scaling 1 → 2 nodes is the hard case (the bottleneck this design exists to solve). 2 → 3+ uses simpler logic because majority-quorum mechanics start working. The architecture must support smooth growth from 1 to 8+ nodes; the 2-node case is the tightest constraint, and once it is correct the larger cases follow with less custom logic.

---

## 10. Keys and identity

Each node has a private/public key pair, generated at install/setup time. Nodes use these to authenticate themselves.

The cluster has a shared cluster key used for cluster-internal authenticated messaging (e.g., adding a witness via signed message).

Each witness has a witness-specific cluster key. When a witness is added, its key is encrypted under the cluster's shared key and stored in the log, so all nodes can use it without out-of-band distribution.

Key rotation runs monthly. The previous generation remains valid through the rotation window so rotation does not cause outages — Kerberos TGT-style overlap. Rotation events are log entries.

Setup and initial key generation are Python-side concerns. Rust holds the keys it needs at runtime.

---

## 11. Python ↔ Rust IPC

Local IPC between the Python management process and the Rust daemon. The interface lets Python:
- Configure the daemon and start/stop participation
- Append entries to the log (blocking until committed)
- Read log entries and subscribe to commit events
- Query daemon status
- Trigger fence on demand (testing, manual operations)

Rust pushes events to Python: committed entries, peer/witness state changes, fence triggers.

The IPC survives Python crashes — Rust keeps running with last-known-good config and queues events with a bounded buffer. Python recovers by reading from the log on restart. Rust does not depend on Python being available for cluster-safety decisions.

---

## 12. REST API surface (Python)

The same FastAPI REST API runs identically on every node. Commands are forwarded to the current leader, except maybe some very local specific commands. Aim for 0 need.

---

## 13. Summary of the discipline this design enforces

Each architectural choice traces to a concrete reason discussed and confirmed:

| Choice | Reason |
|---|---|
| Rust hot-path, Python management | GC pauses are documented to break leader leases under load |
| Append-only log as single source of truth | Replaces config replication, task tracking, state replication with one mechanism |
| SHA-256 hash chain | Detects silent peer divergence (real production failure mode) |
| Witness out of commit path | Reduces witness load to what only it can answer (leader-claim arbitration) |
| Self-fence brings all cluster interfaces down | A fenced node knows nothing; partial fencing has caused split-brain in production |
| 300-second clean-up window before reboot | Node is already out of cluster; Python can do its work without time pressure |
| Static IPs on cluster interfaces | Boot recovery requires network ready before cluster code |
| Leader-only-mode for parameter changes | Eliminates concurrent old-and-new parameter state by serializing |
| Multi-witness up to 15, default 3-of-5 for internet | Single internet witness is a single point of failure with long latency tail |
| Monthly key rotation with overlap | Operational hygiene without rotation outages |
| First entry is "Hello World! + cluster UUID" | Distinguishes re-init from continued cluster |
| 2-node minimum should leverage a direct cable | Filters supportbase, eliminates switch-induced failure modes |

If a scenario can no longer be named for one of these choices, the choice should be revisited.
