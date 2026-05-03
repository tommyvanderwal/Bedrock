# Bedrock vs the field — honest benchmark

Comparing how Bedrock handles cluster-failure scenarios against the
incumbents we'd lose customers to. The point is to **find where we're
doing it dumber than they are**, not to claim wins. Everybody does
dumb things; the question is whether we do fewer of them.

Sources are public docs + first-principles reading of how each
product behaves under the listed scenarios. Where I'm making an
educated guess vs citing a documented behavior I say so.

---

## 1. The systems we benchmark against

| Product | Storage | Hypervisor | Cluster manager | Witness model |
|---|---|---|---|---|
| **VMware vSphere HA** | vSAN or external SAN | ESXi | vCenter + HA agents (FDM) | Network heartbeats + datastore heartbeats |
| **Nutanix AHV (AOS)** | Nutanix HCI (Cassandra-backed) | KVM-derived | CVM (Controller VM, one per host) — Curator/Cassandra | Cassandra quorum across CVMs |
| **Proxmox VE + Ceph** | Ceph RBD | KVM | corosync + pve-cluster + pve-ha-manager | corosync quorum (qdevice for 2-node) |
| **Scale Computing HC3** | SCRIBE (proprietary block layer; conceptually DRBD-like) | KVM-derived | HyperCore | Quorum across nodes; vendor-specific |
| **Bedrock** (this design) | DRBD + NFS-export | libvirt/KVM | bedrock-rust + bedrock-mgmt | Echo UDP witness (ESP32 or SW); weighted-vote |

---

## 2. Detection time — "how fast do we know a node is gone?"

| Product | Default | Typical reality |
|---|---|---|
| VMware vSphere HA | 12 s heartbeat-loss → "isolated" check (5 s) → declare unreachable. Plus `das.failuredetectiontime` overrides. | ~15-30 s before VM restart begins. |
| Nutanix AHV | CVM heartbeat ~5 s; Cassandra gossip detects within ~30 s. | ~30 s to declare host failed. |
| Proxmox + Ceph | corosync token timeout default 1 s, retransmit window ~10 s; pve-ha-manager polls every 10 s. | ~20-60 s. Ceph OSD failures detected separately (~30 s). |
| Scale HC3 | Vendor-claimed sub-30 s; specifics not public. | ~30 s reasonable assumption. |
| **Bedrock** | TCP keepalive 5/3/3 = ~14 s for cable-cut; witness STATUS_LIST takeover_threshold = 2× ttl = 10 s; whichever fires first. | **~10-15 s** in the cable-cut case, **~10 s** in the daemon-stop case (witness ages out). |

**Takeaway:** Bedrock is in the same ballpark or slightly faster for
node-down detection. Nothing dumb here.

---

## 3. Fence behavior — "what does a losing node do?"

| Product | Default fence response |
|---|---|
| VMware vSphere HA | Per-cluster "Isolation Response": **PowerOff** (default since 6.x), Shutdown, or Leave-Powered-On. PowerOff = hard kill of every VM. Then HA restarts them on surviving hosts. |
| Nutanix AHV | If a host loses CVM connectivity: VMs are killed by AHV's HA on that host; Acropolis restarts them on healthy hosts. |
| Proxmox + Ceph | pve-ha-manager fences via **STONITH** (hardware power switch) or watchdog timer that hard-resets the node. After fence, surviving HA-manager restarts marked-HA VMs. |
| Scale HC3 | Failed node's VMs are restarted on survivors. The losing node is fenced (mechanism not fully public; some form of internal stonith). |
| **Bedrock** | Self-fence: `ip link down` cluster interfaces, write fence marker, **stay running idle**. Mgmt pauses VMs (`virsh suspend` — preserves state), stops NFS exports, then unfences. **No reboot in the happy path.** |

**This is the one place Bedrock is doing it less dumbly than everyone
else** — and it's a real difference, not a marketing one.

The competition all does some flavor of "kill the VMs, restart cold
elsewhere". For a transient blip (cable yanked then plugged back in;
switch fault that recovers; brief witness loss), every other product
wakes the cluster up on the surviving side via cold VM restarts.
Customer impact = a power cycle for every VM on the affected node.

Bedrock pauses VMs and resumes them when the network resolves —
**zero state loss** for transient blips. For genuine node-down, we
do the same as everyone else (peer takes over, our stale paused
copy gets destroyed when we eventually rejoin). But the transient-
blip case is *much* more common in practice than node-actually-died,
and we handle it without restarting the workload.

### Real risks of pause-not-shutdown

There aren't many. The cold-restart competitors get a free
structural property (a killed source can't split-brain), but with
correct implementation we get equivalent safety without giving up
the state. The points below are **implementation contracts to keep**,
not fundamental design weaknesses.

**Always log before acting.** Every state-changing operation writes
its log entry first; the action runs after. If the action fails
mid-flight, the log says what was intended and recovery is
deterministic. This is already the codebase rule (single-writer
log + reactor) and the answer to most "what if X races Y?"
worries — Y can't run before its log entry is committed.

**Implementation items the orchestrator must keep right:**

| # | Property | Concretely |
|---|---|---|
| 1 | Resume only when we *know* nothing took over | On unfence: bedrock-rust runs election; mgmt waits for `/run/bedrock-rust.role` to settle; cluster.json catches up. **If the log shows a vm_migrated or new tier_state while we were dark → failover happened → virsh destroy paused VM, drbdadm secondary, DRBD resyncs from peer.** Otherwise → virsh resume. No "dual primary" because we never unpause without confirming we're still authoritative. |
| 2 | Pause cap = 5 minutes | Mirrors AD/Kerberos TGT renewal. After 5 min of no leader/follower role re-confirmed, mgmt escalates: virsh destroy paused VMs, fall through to the failover path. |
| 3 | Watchdog independent of mgmt | A separate systemd timer (not an asyncio task inside the process being watchdogged) checks the marker age. If marker > 5 min and mgmt still hasn't unfenced → `systemctl reboot` as last resort. |
| 4 | Fence requires ALL peer paths down, not one | Single-NIC failure with multi-cable setup must not fence — peer is still visible via the other link. The lease loop already does this (peer registry counts distinct hosts not cables). When a node has a flaky link, mgmt should *demote* the unstable side to secondary so the stable side carries traffic; not flap the whole node. |
| 5 | NFS mounts re-target on tier_state.master change | Reactor watches for `tier_state` updates whose `master` field changed; if so, unmount-then-remount /var/lib/bedrock/mounts/* against the new master's drbd_ip. Soft mount keeps stale ops returning ECONNRESET fast. |

The first four are work-to-do for v1.0 GA. The fifth is a small
reactor extension. **None of these are fundamental design risks** —
they're checklists for the orchestrator to honour. If we get them
right, pause-not-shutdown is strictly safer than cold-restart
across the scenario space because we never lose state we didn't
need to lose.

---

## 4. Recovery time — "VM back online elsewhere, in seconds"

| Product | Failover-restart latency for a typical VM |
|---|---|
| VMware vSphere HA | 30-90 s typical; depends on VM count and storage IO. Boot-from-cold counts. |
| Nutanix AHV | ~60-120 s typical; faster for cattle workloads, slower if many VMs queue. |
| Proxmox + Ceph | 60-120 s for VM cold start + Ceph rebalance background. |
| Scale HC3 | Vendor claim ~30-60 s. Our take: similar to VMware, fast cold-restart. |
| **Bedrock**, transient blip | **0 s** (resume from pause; no cold start). |
| **Bedrock**, real node-down | DRBD primary promotion (~5 s) + virsh start (~5-30 s depending on guest) = **15-45 s**. Comparable to VMware. |

For real node-down, we're competitive. For transient blips, we're
ahead because we don't restart at all.

---

## 5. Split-brain prevention — "two leaders writing the same DRBD"

This is the core safety property. Failures here = data loss.

**A note on what the witness does and doesn't do.** In Bedrock, the
witness is **arbitration-only** — it never sees a write, never
acknowledges a commit, and isn't on the data path for anything. The
log replicates between nodes over TCP; the witness is consulted
purely to break ties when the nodes can't see each other. A node
that can talk to its peers ignores the witness for liveness purposes
(witness loss alone never fences a healthy cluster). This is by
design: keep the witness's job tiny so the witness can be tiny.
That's why an ESP32 is enough — and why the witness can sit on the
public internet without being on the data hot-path.

The 2-node case is the tightest constraint. We **recommend a direct
cable** between the two nodes for orthogonality (no switch in the
peer-link path), but **don't require it** — operators with no
support for back-to-back cabling (rack hardware, multi-site setups,
etc.) run the peer link over their existing switching gear and
accept the wider blast radius. The witness still arbitrates the
same way; partitioned both peer-link and witness-link only happens
on a deeply-coordinated failure.

| Product | Mechanism | Failure mode |
|---|---|---|
| VMware vSphere HA | Network heartbeats AND datastore heartbeats — two independent channels. ESXi has APD (All Paths Down) and PDL (Permanent Device Loss) detection. | Robust in practice; vSAN cluster partition handling has had bugs over the years. |
| Nutanix AHV | CVM Cassandra requires majority quorum for writes. Loss of CVM majority → cluster halts. **No external witness — the CVM majority *is* the arbitrator.** | Robust by design; expensive (each host runs a CVM with 30+ GB RAM overhead). |
| Proxmox + Ceph | corosync quorum (Totem) + STONITH. Without STONITH → known split-brain risk; with STONITH → reboot enforces. Ceph monitors require quorum for writes. | STONITH via IPMI is standard but operators often skip it ("works fine for me until it doesn't"). 2-node cluster needs qdevice or stretches. |
| Scale HC3 | Internal voting + fencing; appliance-level guarantees. | Closed; trust the vendor. |
| **Bedrock** | Weighted-vote (10 per node + 1 per witness, strict majority). Witness consulted **only at election time**, never during commits. DRBD protocol-C quorum below us. Self-fence on lease loss. | Tested for 2/3/4-node, partition + witness scenarios. **Single witness is a single point of arbitration** — if witness lies, bad things happen. v1.0 mitigation: 3-of-5 multi-witness (the data model already supports it; quorum-of-witnesses logic is the missing piece). |

**Where Bedrock is genuinely stronger:**
- Hash-chained log = forensically clean. If divergence ever happens,
  the chain pinpoints the exact entry. No competitor exposes this.
- Single-writer log — only the master can append. Every other
  product has a distributed-write model and trusts internal
  consensus.
- The arbitration channel and the data channel are different
  protocols on different ports (UDP/12321 for witness vs TCP/8200
  for peer log replication). A failure that only affects one
  doesn't poison the other.

**Where Bedrock is weaker:**
- Single witness today. VMware has 2 channels (network + datastore).
  Nutanix's Cassandra-quorum doesn't *need* a separate witness.
  Proxmox has corosync 3-node quorum or qdevice. We have one Echo
  UDP service — if it lies, we have no second source until 3-of-5
  ships.
- DRBD protocol-C is mature but has edge cases (especially around
  resync after multiple failures). Ceph's CRUSH map + monitors is
  a more battle-tested data layer at scale.

---

## 6. Storage scaling — "what about >4 nodes?"

| Product | Storage scale | Failure tolerance |
|---|---|---|
| VMware vSAN | Tens to hundreds of hosts; FTT (failures to tolerate) configurable per VM. | Loses N hosts, still serves data; rebalances. |
| Nutanix AHV | Same scale; Cassandra + Curator handle large clusters well. | RF=3 standard; loses 2 hosts gracefully. |
| Proxmox + Ceph | Hundreds of hosts; Ceph erasure coding + replication. | Configurable; can tolerate many failures. |
| Scale HC3 | Up to ~8 nodes practically (vendor-recommended). | RF=2 typical. |
| **Bedrock** | DRBD pairs/triples per resource. Practically capped at 3-4 active replicas per tier; total cluster size ~8 nodes is reasonable. | Per-tier replica count (2 for bulk/critical default). |

**Honest framing:** in practical installations, almost nobody loses
more than 2 nodes gracefully — those scenarios drift from "HA" to
"site disaster recovery", which has different mechanics (snapshots,
async replication) regardless of vendor. For the 2-4 node hyper-
converged shops Bedrock targets, pair-and-triple DRBD is exactly
what's needed.

**Roadmap, not a permanent ceiling:**
- **v1.2 — rack-aware DRBD pairing.** A "stretched cluster" topology
  where pair members are spread across rooms / DCs deliberately,
  with witnesses placed in a third location for arbitration. e.g.
  4-in-A + 4-in-B + 3 witnesses elsewhere. Same protocol; the
  witness-vote math already handles it.
- **Bedrock 2.0 — alternative storage backends.** EC-based
  block/file stores when the use case warrants it (think Ceph-
  alike behind the same log/mgmt protocol). The single-writer
  log + materialised-views architecture doesn't depend on DRBD;
  the storage layer is interchangeable.

So the honest summary: today's storage tops out where the target
market tops out. Growing past that is roadmapped, not blocked.

---

## 7. Operational features — what they have that we don't

Honest list of where competitors are simply more featureful:

| Feature | VMware | Nutanix | Proxmox | Bedrock |
|---|---|---|---|---|
| Distributed Resource Scheduler (DRS) auto VM placement / rebalancing | ✓ | ✓ | partial (HA only) | ✗ |
| Live migration with no shared storage | ✓ (vMotion + Storage vMotion) | ✓ | ✓ | partial (DRBD-replicated only) |
| VM-level snapshots (consistent across nodes) | ✓ | ✓ | ✓ (ZFS/Ceph) | ✗ (DRBD has no snapshot abstraction) |
| Backup integration | ✓ (Veeam, etc.) | ✓ | ✓ (Proxmox Backup Server) | ✗ |
| Multi-cluster / multi-site | ✓ vCenter multi-cluster | ✓ Prism Central | partial | ✗ |
| Container orchestration in same pane | ✓ (Tanzu) | ✓ (Karbon) | ✗ | ✗ |
| Hardware compatibility matrix maintained | ✓ | ✓ | community-driven | none |
| 24/7 vendor support | ✓ ($$$) | ✓ ($$$) | optional ($) | none |

**Where Bedrock is dumber than competitors today (with roadmap):**
1. **No automatic placement.** v1 is operator-chosen placement. **v1.1
   or v1.2** brings DRS-class behaviour: anti-affinity rules, service
   graph (which VMs depend on which), automatic rebalancing on node
   add/remove or load skew. Sequencing decision: do this or backup
   integration first — only one of the two ships in v1.1.
2. **No snapshot/backup story.** Same answer: **v1.1 or v1.2**, the
   other half of the v1.1-vs-v1.2 split. VM snapshots (DRBD-
   consistent), backup-target integration (Proxmox Backup Server,
   Borg, S3), restore-VM-from-backup as an orchestrated flow.
3. **No multi-cluster.** Each cluster is an island today. v2.0
   timeframe; out of v1.x scope.
4. **No hardware HCL.** AlmaLinux 9 + KVM + a NIC + a disk is the
   spec. No vendor maintaining a hardware compatibility matrix; the
   operator owns hardware choice.
5. **Recovery procedures.** Today's `bedrock` CLI is the entry point
   for unhappy-path operations. **The not-yet-built paths are not
   going to be manual procedures — they're orchestrated Python flows
   with clear preconditions and post-conditions, equivalent to
   one-click "rebuild this node" in the competitors. The CLI verb
   exists, the orchestrator does the steps, the log captures intent.**
   What's left is implementing them, not designing a CLI workflow
   each time.

**Where Bedrock is plausibly less dumb:**
1. **Pause-not-restart on transient fence** (above).
2. **Hash-chained log = single source of truth** for cluster state,
   versioned and audit-friendly. Competitors' state is in opaque
   distributed databases (Cassandra, etcd, internal).
3. **No license/cost.** VMware Broadcom-era pricing is hostile;
   Nutanix expensive; Proxmox subscription optional but support
   model differs. Bedrock = free + simple to install.
4. **Smaller blast radius.** Bedrock-rust is ~3 kLOC of Rust with
   pure-logic unit tests covering the safety paths. ESXi alone is
   millions of lines. Less code = fewer places for it to be wrong
   (in theory; practice depends on how many bugs we miss).
5. **Boot-aware orchestration.** Competitors auto-start their
   storage layer on boot regardless of cluster state; the
   "dangerous-on-boot" cleanup is more sophisticated in their
   stacks but also has more places to be wrong. Bedrock's "don't
   start drbd or libvirtd until cluster contact verified" is a
   blunter rule with less to go wrong.

---

## 8. Failure scenarios — side by side

For each scenario: what does each product do?

### Scenario A: ONE peer-link cable cut, multi-cable setup

This is the recommended deployment: ≥2 cables between every pair of
nodes (RJ45 + USB4, or two NICs through different switches, etc.).

| Product | Behavior |
|---|---|
| VMware HA | Depends on configuration; with multi-NIC team, single-cable failure is invisible. With single NIC: same as scenario B for that host. |
| Nutanix AHV | Multi-NIC bond absorbs the loss; cluster keeps going. |
| Proxmox + Ceph | corosync runs over a redundant ring (rrp_mode=passive); single-cable loss invisible. |
| **Bedrock** | **Non-event.** peer.rs runs each cable as an independent TCP link; the registry counts distinct peer hosts not cables, so the quorum count doesn't change when one of two cables drops. Replication continues over the remaining link. TCP keepalive eventually drops the dead link from the registry. **No fence, no pause, no VM impact.** |

This is the design intent — single-NIC failures must not trigger
anything. The lease loop already counts hosts, not cables; mgmt's
job in this scenario is to surface an alert and (if the affected
node has a history of flapping) demote it from any DRBD primary
roles so a stable peer carries traffic.

### Scenario B: All peer connectivity between two nodes lost

The scenarios where there really is no path between the two nodes:
single-cable setup with the cable cut, multi-cable setup with all
of them gone, switch failure when the only path was via switches.

| Product | Behavior |
|---|---|
| VMware HA | Network heartbeats fail; datastore heartbeats may still work over a separate path. Per Isolation Response, host PowerOffs all VMs; HA restarts them on the surviving side cold. |
| Nutanix AHV | CVMs lose gossip; partitioned side without majority halts. The other side keeps running; failed-over VMs restart cold. |
| Proxmox + Ceph | corosync down between the two; STONITH or watchdog reboots the loser. VMs restarted on survivors cold. |
| Scale HC3 | Similar — fence + cold restart on survivor. |
| **Bedrock** | The side without witness scores ≤10 + 0 = 10/21 → NoQuorum → self-fence (NICs down on cluster interfaces, mgmt pauses VMs). The side with witness scores 10 + 0 + 1 = 11/21 → quorum (just barely!) → mgmt promotes its DRBD primaries, starts the peer's pet VMs cold. When connectivity returns, fenced side unfences, sees its peer ran VMs while it was dark → destroys paused stale copies → DRBD secondary, resyncs from peer. |

For the **all-isolated case** (e.g. shared-switch power failure
takes everyone out at once): every node fences and pauses. When
the switch returns, election runs, the same leader as before is
re-elected (log indices equal), every node resumes its paused VMs.
**Total downtime ≈ pause window. Zero VMs restarted.**

This (and §3's transient-blip case) are where pause-not-shutdown
genuinely beats the field.

### Scenario C: One node truly dies (hardware failure, no recovery)

| Product | Behavior |
|---|---|
| VMware HA | ~30-60 s detection, VMs restart on healthy hosts. |
| Nutanix AHV | ~60 s detection, VMs restart. Cassandra rebalances eventually. |
| Proxmox + Ceph | ~60-120 s, VM restart, Ceph rebalances OSDs. |
| Scale HC3 | ~30-60 s vendor claim. |
| **Bedrock** | ~10-15 s detection; mgmt on surviving leader does failover (DRBD promote, virsh start migrated VM); ~30-45 s end-to-end. |

Bedrock is competitive here. Genuinely-dead-node failover times are
similar across the board.

### Scenario D: Network partition into 2-2 with witness on one side (4-node)

| Product | Behavior |
|---|---|
| VMware HA | Side without datastore heartbeats has VMs PowerOff; side with both keeps running. (VMware's behavior here is well-tuned but complex.) |
| Nutanix AHV | Side without CVM majority halts. Side with majority keeps running. |
| Proxmox + Ceph | Side without corosync majority gets STONITH-ed. Side with majority keeps running. |
| Scale HC3 | Per vendor; assume similar. |
| **Bedrock** | Side without witness scores 20/41 → NoQuorum → self-fence. Side with witness scores 21/41 → keeps running. Verified working in our partition tests. |

We're behaviorally equivalent here. The math is different (we use
weighted votes, they use majority) but the outcome is the same:
the side without the witness/quorum bows out.

### Scenario E: Witness is compromised / lying

| Product | Behavior |
|---|---|
| VMware HA | Datastore heartbeat is a second channel; witness compromise alone doesn't cause split-brain. |
| Nutanix AHV | No external witness; Cassandra majority is internal. Compromise of CVMs would be needed. |
| Proxmox + Ceph | Multiple corosync nodes + qdevice (if used); single qdevice compromise possible but limited blast radius. |
| Scale HC3 | Vendor-internal; unclear. |
| **Bedrock** | A single witness today is a single point of arbitration. A compromised witness could lie on STATUS_LIST (claim peer alive when it isn't, or vice-versa), affecting election outcomes. **Mitigation path: signed payload in the Echo protocol** — see below. |

**The proper v1.x answer: untrusted witness via signed STATUS_LIST
contributions.** Each node's heartbeat carries its own (cluster-key-
signed) `(sender_id, last_index, last_hash, utc_ms)` tuple; the
witness stores those tuples and serves them back in STATUS_LIST.
Receiving nodes verify the signature and the freshness-bound on
`utc_ms` before trusting any entry. The witness becomes a passive
relay — it can stall (refuse to relay, or run slow), but it cannot
forge or rewrite a peer's contribution. Worst-case attack on a
compromised witness is denial of arbitration (failure-stop), not
false arbitration (split-brain trigger).

The data model already supports it — every node has the cluster
key, the Echo protocol carries 64-byte payloads, UTC time is
trivial. The piece to implement is the per-node sign/verify and
the freshness window. This is the right answer; 3-of-5 multi-
witness is a stop-gap until this lands.

---

## 9. Honest summary — what should we tell a customer?

**Bedrock is the right choice when:**
- 2-4 node clusters at MSP shops, on-prem.
- Operator wants a small, auditable system they can fix themselves.
- Workload tolerates pause-then-resume well (most workloads do).
- Budget is sensitive; vendor lock-in is a no-go.
- Storage needs fit DRBD pair/triple replication.

**Bedrock is the wrong choice when:**
- > 8 nodes, or growing fast toward enterprise scale.
- Need DRS / auto-placement / hot-migration policies.
- Need integrated backup and snapshot tooling.
- Need multi-cluster federation.
- Customer's compliance demands a vendor-supported HCL.

**The genuine wins worth marketing:**
1. Pause-not-restart on transient fence (real differentiator).
2. Hash-chained log + materialised views (forensic transparency).
3. Open source, no license cost, no vendor lock-in.
4. Small enough to read end-to-end (auditable).

**The genuine losses worth admitting:**
1. No DRS-like placement engine.
2. No native snapshot/backup story.
3. Single-witness SPOF until 3-of-5 ships.
4. Storage scale capped (~8 nodes practical).
5. Manual ops for the unhappy paths.

If we don't say these out loud, customers will discover them anyway
and feel deceived. Saying them up front turns "missing feature" into
"deliberate trade-off" — which is much easier to sell.

---

## 10. Things to watch — orthogonal edge cases

§3 covers the implementation contracts the orchestrator must keep.
These are unrelated items worth tracking before v1.0:

| Item | Why | Plan |
|---|---|---|
| Untrusted witness (single-witness SPOF) | Arbitration-only role — but if it lies, election outcomes can be skewed. | **Signed STATUS_LIST contributions** — see §8 scenario E. Witness becomes passive relay; can stall but can't lie. The actual v1.x mitigation. |
| TCP keepalive on slow links | 5/3/3 → ~14 s detection; on long-RTT VPN this could trip on transient congestion. | Operator-tunable via daemon.toml; document the trade-off. |
| Witness lies about smaller_id_alive | A witness claiming an out-of-partition peer is still-recent could block a legitimate promotion. | Cross-check: if peer is "alive at witness" but we haven't seen any TCP frame from them for 2× ttl on any link, ignore the witness's claim for the smaller-id-priority check. (Subsumed by the signed-payload path above.) |
| Boot orchestrator idempotency | mgmt restart mid-`start_local_services` must converge. | `systemctl start` no-ops if running; `drbdadm primary` on already-primary is a no-op; `virsh start` on running is a no-op. The contract holds; document it. |
