# Bedrock — Project Reference

## What it is
Local infrastructure HA platform. One single, 2 in HA or more in HA x86 nodes running KVM/QEMU on AlmaLinux 10, with per-VM DRBD block device replication, live migration via temporary dual-primary, and a simple witness-based failover orchestrator. No corosync, no PVE, no cluster frameworks. Just assembled LEGO from mature Linux components.

## Target market
MSPs shipping a single or HA infrastructure to small/medium businesses. VMware refugees with two-server setups. The 90% that don't need Nutanix-scale but need better than Proxmox-on-two-nodes.
Growth path from 1 single box into 2 with HA is crucial for the 1.0 release version. Just put a box there to run an app. Later if it ever did go down or needs increased uptime: Add another box + interconnect cable.

## Core design principles
- When the orchestrator fails, nothing changes state. VMs keep running, DRBD keeps replicating.
- Each DRBD pair is an independent failure domain. 100 VMs means 100 pairs, not one big cluster. No cluster-wide blast radius.
- The state machine is tiny: both healthy, one down with witness confirming, failed node returning, admin-requested migration. Four or five states. Keep it there.
- Say NO to unneeded complexity. Frameworks only work if you use them as intended — building on plain components avoids framework fights.
- **All cluster orchestration goes through rqlite as sagas.** Every long-running operation (VM create, disk grow, DRBD attach, node join, cluster-DRBD membership change, weed-master reshuffle, SeaweedFS replica fix-up after a node returns, …) writes intent → executes idempotent steps → writes "done" — each step durable in rqlite. Power-loss at any step is recoverable: on boot, pick up where the `operation_steps` log says we left off. The ONE exception is recovering the rqlite arbiter itself, which is what the witness + arbiter-takeover protocol exists for. See [`docs/cluster-quorum-spec.md`](docs/cluster-quorum-spec.md) and [`docs/storage-architecture.md`](docs/storage-architecture.md).

## Hardware — 0.1 Lab
- 2x GMKtec Zen4 mini PC, 32GB RAM, 1TB NVMe, 2x 2.5gbit NIC each
- MikroTik 8-port 2.5gbit switch + 2x SFP+ (management/VM network only)
- Direct ethernet cable between second NIC on each box (DRBD replication — no switch in this path)
- PiKVM for initial OS install
- Separate box running Claude Code for development

## Software stack
- **Base OS:** AlmaLinux 10.1. (Earlier drafts targeted 9 because DRBD-kmod against the 10.0 kernel had open issues; ELRepo's `kmod-drbd9x-9.3.x` against el10_1 resolves that.)
- **Hypervisor:** KVM/QEMU/libvirt (standard AlmaLinux packages)
- **Storage:** **One VG per node (`bedrock-vg`), one thinpool (`thinpool`).** Cluster singletons (rqlite arbiter + SeaweedFS filer + S3 IAM) live on a DRBD-replicated LV pair capped at 3 peers; per-VM DRBD LV pairs live alongside; the local SeaweedFS volume server gets one LV (NOT DRBD — SeaweedFS handles file-level replication via collections). Boot needs ~1.5 GB outside the VG (EFI + /boot); everything else is thin-provisioned and freely re-allocatable. TRIM/discard end-to-end. **One thin meta LV per DRBD resource**. See `docs/storage-architecture.md` for the full layout.
- **Networking:** br0 bridge on management NIC for VM traffic. Mesh-aware overlay (`bedrock-net` daemon, on every node) for cluster-internal traffic — every NIC is a path candidate, the kernel routes per-peer through the best available physical link, DRBD multi-paths over the real per-NIC addresses. See `docs/06-mesh-network.md`. Cluster identity lives in RFC 6598 Shared Address Space (`100.64.0.0/10`, derived `/24` per cluster from `cluster_uuid`); per-NIC link addresses come from RFC 3927 IPv4 link-local assigned by NetworkManager. Operator plugs any cable into any port and the system figures out the rest.
- **Orchestrator:** Python based. Most code should be python based. Only realtime critical items should potentially be rust components. e.q. a custom docker DRBD witness on Mikrotik, would probably be rust.

## Why AlmaLinux (not Debian, not Ubuntu)
- RHEL machine type ABI stability for safe live migration across updates
- 10-year lifecycle (to 2035 for version 10)
- Binary-compatible upgrade path to RHEL if commercial support needed
- Conservative repos prevent cowboys from `apt install`-ing random stuff on the hypervisor
- DRBD/LINBIT explicitly recommend AlmaLinux as the CentOS replacement

## Why not Proxmox
- Corosync required for clustering — can't do clusterless live migration
- Fighting the framework is harder than building a thin layer on plain libvirt
- PVE's opinions on storage/networking/HA don't align with our DRBD-per-VM architecture

## Storage architecture — critical decisions
**Authoritative reference:** `docs/storage-architecture.md`. Summary:

- **One VG per node (`bedrock-vg`), one thinpool
  (`thinpool`)** holding everything: the DRBD-replicated
  cluster-singleton LV pair (capped at 3 peers), DRBD LV pairs per
  VM disk, and the local SeaweedFS volume LV. SeaweedFS does its
  own file-level replication via collection policy; LVM doesn't
  need to slice by tier.
- **One DRBD resource per VM disk**, one thin data LV +
  one thin meta LV per resource. `max-peers=7` baked at create-md.
  QEMU opens `/dev/drbd/by-res/vm-name-disk0/0` as raw block.
  Online grow = `lvextend meta` (if needed) + `lvextend data` +
  `drbdadm resize`; no downtime.
- **`.254/32` cluster-singleton VIP** on loopback at all N
  (including N=1). Hosts rqlite arbiter, SeaweedFS filer:8888,
  mgmt HTTPS:8443. Failover moves the VIP + DRBD primary +
  filer + rqlite-arbiter atomically.
- **SeaweedFS** for shared file/object storage: filer singleton on
  `.254` (DRBD-backed leveldb3); weed-master Raft-3 on three
  regular nodes (NOT on `.254`); weed-volume + weed-s3 on every
  node bound `0.0.0.0`; every node FUSE-mounts the filer at
  `/mnt/bedrock` pointing at `.254:8888`. Three collections —
  `scratch` (replication 000), `standard` (001, default),
  `critical` (002, 3 copies). S3 IAM identities live inside the
  filer DB.
- **TRIM/discard end-to-end:** guest FS → QEMU
  (`discard=unmap`) → DRBD (`discard-zeroes-if-aligned`) → LVM
  thin (`thin_pool_discards=passdown`) → NVMe.
- **No artificial fill cap.** Monitoring warns at 70 %, alarms at
  80 %; writes are NEVER refused at those thresholds.
- **Swap is opt-in.** Default none — swap-on-thin can panic the
  kernel when the pool fills.
- **No NFS or cluster filesystem in the VM-disk path.** Every node
  with a replica already has every byte via DRBD.

## Live migration — how it works
1. VM runs on node1. DRBD resource is Primary/Secondary.
2. Temporarily enable dual-primary: `drbdadm net-options --allow-two-primaries=yes <resource>`
3. Promote node2 to primary. Both nodes now have local read-write access. No data copying.
4. `virsh migrate --live` moves RAM state only. QEMU on node2 reads/writes its local DRBD device.
5. Migration completes. Demote node1 to secondary. Disable dual-primary.
6. Zero storage I/O over the network during migration. Only RAM transfer.

## HA failover — how it works
**Authoritative reference:** `docs/cluster-quorum-spec.md`. Summary:

- **Witness is a passive K/V slot store** (a "bedrock-echo" device:
  ESP32 firmware or a tiny container on a MikroTik). UDP/12321,
  AEAD-encrypted with the cluster's shared key. Each node owns
  one slot, writes its own slot every 1 s, reads every other
  node's slot on each reply.
- **No "blessing", no claim acceptance, no holddown.** The witness
  has zero logic — it stores last-write per slot and returns all
  slots on every reply. All decisions are local to each node.
- **Weighted-vote election** (`100 × N_active_nodes + 1 per valid
  witness`) decides Leader / Follower / NoQuorum on every node's
  1 Hz tick. A witness only adds its vote when it is reachable AND
  its reply reflects our own last write (valid + confirmed).
- **Arbiter takeover protocol:** before becoming the new
  `.254`-host, the candidate node inspects the previous master's
  slot, verifies its local `drbdadm current-uuid` for the `cluster`
  singleton exactly matches the slot's `marker` field, flips its
  own slot's `lms` bit, reads back from the next witness reply, then
  promotes. Refuses to promote on UUID mismatch.
- **Self-demote on NoQuorum:** the current `.254`-host stops
  services and releases the VIP after `SELF_DEMOTE_MISSES = 9`
  consecutive NoQuorum ticks (≈ 9 s) — one second before a survivor
  promotes at `MASTER_LOSS_MISSES = 10` (≈ 10 s), so the VIP and
  arbiter rqlite are released before any survivor takes them. There's
  never a window where two nodes both hold `.254`.
- **No automatic failback.** Failed node returns as secondary,
  re-syncs DRBD, and waits for the calm orchestrator's
  reconciliation pass.
- **The calm orchestrator** (slower, deliberate) handles
  arbiter-set membership changes, weed-master Raft re-shuffles,
  and capacity-driven decisions — none of those are on the
  critical failover path.

## Version roadmap
- **0.1:** Manual install, scripts, working live migration + HA failover, Linux + Windows VMs. This document.
- **0.5:** Reliability hardened. Power-down testing across all state machine paths. No backup yet.
- **0.6:** Extensive random power-down tests across 2, 3, 4 node configurations with persistent storage.
- **0.7:** PBS backup integration (full-read to start, dirty-bitmap optimization later if needed).
- **1.0:** Production-ready. API, local dashboard, VM import (virt-v2v), VM export. Support offering.
- **1.5:** ARM nodes for stateless/container workloads only (no live migration, no DRBD on ARM). x86 stays for pets.
- **2.0:** SAN support mode (same orchestrator/witness, but storage from existing SAN instead of DRBD). Multi-site dashboard.

## Build phases for 0.1
1. **Base OS** — AlmaLinux 9 minimal on both nodes. Root SSH, static management IP, NTP, SELinux permissive, firewall off.
2. **Networking** — Mgmt LAN over br0 (bridge over the first NIC, via MikroTik). All intra-cluster traffic (DRBD, SeaweedFS, libvirt migration) targets the per-node loopback /32 in the cluster's CGNAT /24 (100.X.Y.0/24, derived from `cluster_uuid` — see `installer/lib/cluster_addr.py`); the mesh layer (`bedrock-net`) routes those packets over whichever physical NIC has the best path.
3. **Hypervisor** — Install KVM/QEMU/libvirt on both nodes. Verify libvirtd running.
4. **Storage foundation** — LVM thin pool on NVMe. DRBD from ELRepo. Load kernel module.
5. **First replicated volume** — Thin LV on both nodes, DRBD resource config, initialize and sync over direct link.
6. **Linux VM on raw DRBD** — virt-install pointing QEMU at DRBD block device. Install guest, verify networking and guest agent.
7. **Live migration** — Define VM on both nodes. Enable dual-primary, promote both, virsh migrate --live, demote source, disable dual-primary.
8. **Script migration** — Single command wrapping the dual-primary/migrate/demote sequence. Test both directions under load.
9. **HA failover** — Watchdog script using MikroTik as witness reference. Test by pulling power on active node. VMs restart on survivor.
10. **Windows VM** — New DRBD resource, virtio drivers during install, validate live migration and power-yank failover.
10.5. **TRIM verification** — Write/delete/fstrim in guest, confirm thin pool space reclaimed on host.

## Competitive landscape
- **Proxmox:** Corosync dependency makes two-node HA painful. Good product but opinionated framework.
- **Elemento (AtomOS):** Italian startup, KVM-based, RHEL-compatible. Uses Ceph on 2 nodes (questionable). C4 peer discovery is interesting. Multi-cloud focus, not local HA focus. Young physics PhD team, no grizzled infra engineers.
- **Nutanix:** Data locality principle similar to ours (reads local, writes replicate). But minimum 3 nodes, cluster-wide blast radius from DSF, expensive licensing. Enterprise scale, not our market.
- **VMware:** The thing everyone's fleeing from. Broadcom pricing. Our import story (virt-v2v) targets their refugees.

## Key architectural differences from Ceph/Nutanix
- DRBD pairs don't share metadata, consensus, or placement algorithms. No cluster-wide failure mode.
- Adding nodes never increases blast radius. Node 101 doesn't make nodes 1-100 more vulnerable.
- The orchestrator is the only shared logic, and it's KISS — if it crashes, everything freezes in last known-good state.

## Future considerations
- **Backup:** PBS with proxmox-backup-client. Full-read re-chunk for 0.7, dirty-bitmap tracking if needed later. fsfreeze for consistency.
- **VM import/export:** virt-v2v for VMware/Hyper-V import. qemu-img convert for export to VMDK/VHD/VHDX. Offramp documented prominently.
- **Application services:** Start with Elestio BYOVM for managed open-source apps. Build native modules only for fundamental infra (PostgreSQL, MinIO, Redis, reverse proxy). Stay away from long tail.
- **Multi-architecture:** ARM for stateless containers/immutable VMs only. No live migration on ARM. Cross-arch app replication only at logical level (pg_dump, not WAL streaming).
- **Dashboard layers:** Local orchestrator API is ground truth. Business dashboard aggregates site APIs. Upper layer observes and alerts, never decides failover. Local layer never depends on anything above it.
- ** local dashboard:** Very soon after this a webinterface needs to become available. Showing a no-nonsense management interface in a browser to initiate action manually. All action will also be available via API for further automation.
