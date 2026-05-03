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

The cold-restart competitors have one thing going for them: by
killing the source-side VM, they make split-brain *structurally
impossible*. We give that property up to keep state, and we have
to earn it back through other mechanisms. Specific failure modes
to be honest about:

**R1. Dual-primary at DRBD reconnect (the big one).**
2-node case: A is DRBD primary on bulk/critical. A's cable cuts.
A fences (NICs down) but does NOT demote — qemu's file descriptors
are still open on /dev/drbd*, so `drbdadm secondary` would fail
EBUSY anyway. Meanwhile B sees A's witness STATUS_LIST entry age
out, gains quorum, mgmt promotes B to DRBD primary on those
resources, B starts the failover copies. Cable comes back: both
sides reconnect with both believing they're primary. Our DRBD
config has `after-sb-2pri disconnect`, so DRBD detects this and
halts replication — no data corruption, but **manual operator
recovery is required**.

Mitigation paths, all worth picking from:
  a. `virsh save` instead of `virsh suspend` in the fence cleanup —
     dumps RAM to disk, kills qemu, frees the DRBD device.
     `drbdadm secondary all` then succeeds. On unfence, `virsh
     restore` from the saved state. Cost: save file is ≈ guest RAM
     size on local disk; save itself takes seconds-to-minutes.
  b. Use DRBD's `--force` semantics or detach-while-paused flags to
     demote even with open FDs. Less clean.
  c. Accept it. Document that a fence-during-active-pet-VM may
     require operator intervention on the formerly-fenced side
     after reconnect. For cattle workloads (no DRBD), this risk
     doesn't apply.

  Today the code does (c) implicitly. **This is a real, named
  hazard.** For v1.0 GA we should land (a) at least for pet VMs.

**R2. Pause window has no upper bound.**
A 30-second pause is invisible to a user. A 30-minute pause means
guest TCP connections die, NTP drifts, retry storms hit on resume,
healthchecks alarm. Resuming a 6-hour-old VM is operationally
worse than restarting it cold.

  Mitigation: cap unfence-wait. If `_wait_for_role` doesn't return
  leader/follower within (say) 10 minutes, escalate to "treat as
  real failover" — `virsh destroy` paused VMs locally, let the
  cluster's takeover path do its job. **Not implemented.**

**R3. Watchdog timer is inside the process being watchdogged.**
The 270-second cleanup budget is `asyncio.wait_for` inside
fence_responder. If mgmt itself crashes mid-cleanup, the timer
dies with it. systemd will restart mgmt; on restart it sees the
marker and runs cleanup again, but with the timer reset to 270 s.
A pathological crash loop could keep the node dark indefinitely
while never exceeding the timer.

  Mitigation: make the watchdog a **separate** systemd timer/service
  that checks the marker's age and `systemctl reboot`s if it
  exceeds 5 min — independent of whether mgmt is alive.
  **Not implemented.**

**R4. Fence flap.**
Flaky NIC, periodic. Each flap → fence → cleanup → unfence cycle.
VMs get repeatedly paused and resumed. App-level havoc; transient
TCP connections drop on every flap.

  Mitigation: per-node "fence-flap" counter with backoff. After N
  fences in M minutes, refuse to unfence; surface as a hard alert.
  Operator decides recovery. **Not implemented.**

**R5. NFS hangs on followers during a leader fence.**
Followers mount /var/lib/bedrock/mounts/* from the leader's NFS
export. When the leader fences, `exportfs -au` runs but the
follower's NFS mount sees ECONNRESET / hangs depending on mount
options. If a new leader takes over and re-exports, followers
need to remount (the new leader has a different IP).

  Mitigation: NFS mounts use `soft,intr,timeo=10,retrans=2` so
  hangs surface fast; reactor handles `tier_state.master` change
  by remounting against the new master's address. **Partially
  there — soft-mount is set; remount-on-master-change isn't.**

**R6. Witness STATUS_LIST staleness during the takeover window.**
Between fence (T=0) and witness `last_seen_ms > 2× ttl` (T=10 s),
the side that survives sees the fenced peer as "still recent" at
the witness even though the fenced peer has stopped heartbeating.
During this window the surviving side won't promote because
`smaller_id_alive_anywhere` is still true. Promotion happens
~10 s after fence, not ~1 s.

  This is by design — premature promotion is worse than slow
  promotion. But it's worth knowing the failover SLO is
  ttl + 2× ttl + DRBD promote ≈ 20 s, not the ~5 s some
  competitors claim. **Working as intended.**

**R7. Race: fence simultaneous with operator action.**
Operator runs `bedrock node leave sim-2`; while replication is
in flight, sim-2 fences for an unrelated reason. Sim-2's fence
cleanup runs against a snapshot that doesn't yet know it's been
unregistered. Mostly harmless (we pause our own VMs, peer takes
over per the unregister), but the post-unfence reconcile sees
"I'm not in cluster.json" → role unknown → services held →
eventually watchdog reboot. End result is correct (sim-2 leaves)
but the path is messy.

  Mitigation: `_cmd_node_leave` could SSH-stop bedrock-rust
  *before* appending node_unregister, ensuring no fence races.
  **Cosmetic; no data risk.**

If we ship v1.0 with R1's mitigation (a) and R2/R3/R4 implemented,
the pause-not-shutdown approach is genuinely safer than cold-restart
across the scenario space, not just in the happy case.

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

## 10. Things to watch — additional known edge cases

The pause-not-shutdown risks (R1-R7) are in §3. The items below are
unrelated edge cases worth tracking before v1.0:

| Risk | Why | Mitigation |
|---|---|---|
| Witness compromise (single witness) | One signal, one source of truth for arbitration. A buggy or compromised witness can feed bad STATUS_LIST data. | Ship 3-of-5 multi-witness with quorum-of-witnesses logic before v1.0 GA. The data model already supports `Vec<WitnessSpec>`; only the cross-witness agreement logic is missing. |
| TCP keepalive too aggressive on slow links | 5/3/3 → ~14 s detection; on a flaky 100 Mbit link or a long-RTT VPN this could trip on transient congestion. | Operator-tunable via daemon.toml; document the trade-off. |
| Witness lies about smaller_id_alive | A witness reporting an out-of-partition peer as still-recent could block legitimate promotion (we'd see `smaller_id_alive_anywhere=true` and stay Follower). | Cross-check with peer.rs registry: if a peer is "alive at witness" but we haven't seen any TCP frame from them for 2× ttl on any link, treat as gone for the smaller-id-priority check. |
| Boot orchestrator races mgmt-restart | If mgmt is restarted by systemd while `start_local_services` is mid-flight (e.g. drbd just started, libvirtd not yet), the next mgmt invocation must finish what was started. | `start_local_services` is already idempotent (systemctl start is no-op if running, drbdadm primary on already-primary is no-op, virsh start on running is no-op). Document this contract. |
