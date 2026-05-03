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

Risk side: this only works because DRBD's protocol-C quorum prevents
dual-primary. If we ever miscompute that, we'd be in worse shape
than the cold-restart guys (whose cold restart is, by construction,
incapable of split-brain because the source side was killed).

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

| Product | Mechanism | Failure mode |
|---|---|---|
| VMware vSphere HA | Datastore heartbeats as a second channel beyond network. ESXi has APD (All Paths Down) and PDL (Permanent Device Loss) detection. | Pretty robust; vSAN cluster partition handling has had bugs over the years (CVE/KB list). |
| Nutanix AHV | CVM Cassandra requires majority quorum for writes. Loss of CVM majority → cluster halts. | Robust by design; expensive (each host runs a CVM with 30+ GB RAM overhead). |
| Proxmox + Ceph | corosync quorum (Totem) + STONITH. Without STONITH → known split-brain risk; with STONITH → reboot enforces. Ceph monitors require quorum for writes. | STONITH via IPMI is standard but operators often skip it ("works fine for me until it doesn't"). 2-node cluster needs qdevice or stretches. |
| Scale HC3 | Internal voting + fencing; appliance-level guarantees. | Closed; trust the vendor. |
| **Bedrock** | Weighted-vote (10 per node + 1 per witness, strict majority). DRBD protocol-C quorum below us. Self-fence on lease loss. | Tested for 2/3/4-node, partition + witness scenarios. **Single witness is a single point of arbitration** — if witness lies (compromised, buggy), bad things happen. v1.0 mitigation: 3-of-5 multi-witness. |

**Where Bedrock is genuinely stronger:**
- Hash-chained log = forensically clean. If divergence ever happens,
  the chain pinpoints the exact entry. No competitor exposes this.
- Single-writer model — only the master can append. Every other
  product has a distributed-write model and trusts internal
  consensus.

**Where Bedrock is weaker:**
- Single witness. VMware has 2 channels (network + datastore). Nutanix
  doesn't need a separate witness because Cassandra-quorum *is* the
  witness. Proxmox has corosync 3-node quorum or qdevice. We have
  one Echo UDP service — if it lies, we have no second source.
- DRBD protocol-C is robust but it's been around long enough to have
  edge cases (especially around resync after multiple failures).
  Ceph's CRUSH map + monitors is a more mature data layer.

---

## 6. Storage scaling — "what about >4 nodes?"

| Product | Storage scale | Failure tolerance |
|---|---|---|
| VMware vSAN | Tens to hundreds of hosts; FTT (failures to tolerate) configurable per VM. | Loses N hosts, still serves data; rebalances. |
| Nutanix AHV | Same scale; Cassandra + Curator handle large clusters well. | RF=3 standard; loses 2 hosts gracefully. |
| Proxmox + Ceph | Hundreds of hosts; Ceph erasure coding + replication. | Configurable; can tolerate many failures. |
| Scale HC3 | Up to ~8 nodes practically (vendor-recommended). | RF=2 typical. |
| **Bedrock** | DRBD pairs/triples per resource. Practically capped at 3-4 active replicas per tier; total cluster size ~8 nodes is reasonable. | Per-tier replica count (2 for bulk/critical default). |

**This is Bedrock's biggest architectural disadvantage.** DRBD is
pair-replicated (or 3-way for critical); we don't have an erasure-
coded cluster-wide pool. For deployments that want "lose any 3 of 16
nodes and keep going", Bedrock is the wrong tool.

For our actual target (MSP shops, 2-4 node sites, on-prem), this
doesn't bite. But we should be honest that Bedrock won't grow into
an enterprise vSAN or Ceph replacement.

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

**Where Bedrock is dumber than competitors today:**
1. **No automatic placement.** Operator decides which node hosts
   which pet/cattle. Competitors' placement engines are a real
   value-add.
2. **No snapshot story.** Competitors' VM snapshots (consistent
   across nodes, fast clone, off-cluster backup integration) are
   table-stakes for their target market.
3. **No multi-cluster.** Each Bedrock cluster is its own island.
4. **No support for hardware HCL.** Competitors qualify hardware;
   we say "AlmaLinux 9 + a NIC + a disk" and trust the operator.
5. **Manual recovery procedures.** When something goes wrong,
   `bedrock` CLI is shell-script-ish. Competitors have
   one-click "rebuild this node" workflows.

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

### Scenario A: One node loses the cluster cable (still has mgmt LAN)

| Product | Behavior |
|---|---|
| VMware HA | Network heartbeats fail; datastore heartbeats may still work. Host enters "Network Isolated" state. Per `das.isolationaddress` ping check, host runs Isolation Response (PowerOff). VMs killed; restarted on surviving hosts. |
| Nutanix AHV | CVM loses gossip; cluster votes node out; VMs killed and restarted. |
| Proxmox + Ceph | corosync fails; STONITH or watchdog reboots the node. VMs restarted on survivors. |
| Scale HC3 | Vendor handling; expected to fence + restart. |
| **Bedrock** | Lease-loop loses witness (witness on mgmt LAN; reachable). Lease loop loses peer (cluster cable down). Quorum check: TCP-visible peers = 0; with witness = 11/21 (2-node) or 11/41 (4-node) → **NoQuorum** → self-fence. mgmt pauses VMs, drops NFS. When cable restored → unfence → resume VMs. **No VM restart.** |

### Scenario B: Power failure on the cluster switch (all nodes affected)

| Product | Behavior |
|---|---|
| VMware HA | All hosts isolated simultaneously. Each runs Isolation Response. **All VMs power-off everywhere.** When switch returns, vCenter restarts everything cold. Major outage. |
| Nutanix AHV | All CVMs lose gossip; cluster has no quorum; cluster halts. Manual recovery often needed. |
| Proxmox + Ceph | corosync down everywhere; pve-ha-manager fences (potentially all nodes via watchdog). VMs killed; cold restart on resume. |
| Scale HC3 | Similar — total cluster down. |
| **Bedrock** | All nodes fence simultaneously, all pause VMs. When switch returns, election runs, same node is leader (lowest sender_id, log indices equal), every node resumes its paused VMs. **Total downtime ≈ pause window. Zero VMs restarted.** |

This is the scenario where Bedrock's design pays off the most.

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
| **Bedrock** | **Single point of failure if only 1 witness configured.** A lying witness could feed bad STATUS_LIST data (e.g., claim a peer is alive when it isn't, blocking promotion). 3-of-5 multi-witness mitigates but isn't yet implemented. |

This is a legitimate concern. Mitigation path: 3-of-5 witnesses, each
with own key. We have the data model for it (`Vec<WitnessSpec>`); the
quorum-of-witnesses logic isn't implemented yet (today we trust the
freshest reading from any witness).

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

## 10. Things to watch — known edge cases in our design

These are not hypothetical worries; they are places where the design
*could* be doing something dumb that we should test for before v1.0:

| Risk | Why | Mitigation |
|---|---|---|
| Witness compromise (single witness) | One signal, one source of truth for arbitration | Ship 3-of-5 multi-witness with quorum-of-witnesses logic before v1.0 GA |
| TCP keepalive too aggressive on slow links | 5/3/3 → ~14 s detection; on a flaky 100 Mbit link this could falsely trip | Operator-tunable in daemon.toml; document the trade-off |
| DRBD protocol-C reconnect after both-dark | We rely on DRBD to figure out who's primary on reconnect | Scripted regression test against our specific reconnect scenarios |
| Pause window too long | If we pause for 10+ minutes, guest TCP connections die anyway, OS clocks drift | Cap the unfence window; if cluster doesn't recover within N minutes, escalate to "treat as real failover" — kill paused VMs, peer takes over |
| Witness lie about smaller_id_alive | A witness reporting an out-of-partition peer as recent could block promotion | Cross-check with our own peer.rs registry: if smaller_id_alive at witness but I haven't been able to reach them via TCP for 2× ttl, treat as gone |
| Boot orchestrator races mgmt-restart | If mgmt restarts during DRBD startup, half-started services | Idempotent start_local_services + clear ordering |

We should test 3 + 5 of these before committing to v1.0; the others
go in the lessons-log as known limitations to revisit.
