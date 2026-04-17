# Bedrock — Project Reference

## What it is
Local infrastructure HA platform. One single, 2 in HA or more in HA x86 nodes running KVM/QEMU on AlmaLinux 9, with per-VM DRBD block device replication, live migration via temporary dual-primary, and a simple witness-based failover orchestrator. No corosync, no PVE, no cluster frameworks. Just assembled LEGO from mature Linux components.

## Target market
MSPs shipping a single or HA infrastructure to small/medium businesses. VMware refugees with two-server setups. The 90% that don't need Nutanix-scale but need better than Proxmox-on-two-nodes.
Growth path from 1 single box into 2 with HA is crucial for the 1.0 release version. Just put a box there to run an app. Later if it ever did go down or needs increased uptime: Add another box + interconnect cable.

## Core design principles
- When the orchestrator fails, nothing changes state. VMs keep running, DRBD keeps replicating.
- Each DRBD pair is an independent failure domain. 100 VMs means 100 pairs, not one big cluster. No cluster-wide blast radius.
- The state machine is tiny: both healthy, one down with witness confirming, failed node returning, admin-requested migration. Four or five states. Keep it there.
- Say NO to unneeded complexity. Frameworks only work if you use them as intended — building on plain components avoids framework fights.

## Hardware — 0.1 Lab
- 2x GMKtec Zen4 mini PC, 32GB RAM, 1TB NVMe, 2x 2.5gbit NIC each
- MikroTik 8-port 2.5gbit switch + 2x SFP+ (management/VM network only)
- Direct ethernet cable between second NIC on each box (DRBD replication — no switch in this path)
- PiKVM for initial OS install
- Separate box running Claude Code for development

## Software stack
- **Base OS:** AlmaLinux 9 (NOT 10 — DRBD kmod has kernel crash bugs on 10's 6.12 kernel as of late 2025)
- **Hypervisor:** KVM/QEMU/libvirt (standard AlmaLinux packages)
- **Storage:** LVM thin pool on NVMe, one DRBD resource per VM (disk) as raw block device. DRBD from ELRepo (kmod-drbd9x). Synchronous replication.
- **Networking:** br0 bridge on management NIC for VM traffic. Dedicated private subnet on direct cable for DRBD replication.
- **Orchestrator:** Python based. Most code should be python based. Only realtime critical items should potentially be rust components. e.q. a custom docker DRBD witness on Mikrotik, would probably be rust.

## Why AlmaLinux (not Debian, not Ubuntu)
- RHEL machine type ABI stability for safe live migration across updates
- 10-year lifecycle (to 2032 for version 9)
- Binary-compatible upgrade path to RHEL if commercial support needed
- Conservative repos prevent cowboys from `apt install`-ing random stuff on the hypervisor
- DRBD/LINBIT explicitly recommend AlmaLinux as the CentOS replacement

## Why not Proxmox
- Corosync required for clustering — can't do clusterless live migration
- Fighting the framework is harder than building a thin layer on plain libvirt
- PVE's opinions on storage/networking/HA don't align with our DRBD-per-VM architecture

## Storage architecture — critical decisions
- **One DRBD resource per VM (disk), not a shared filesystem.** QEMU opens `/dev/drbd/by-res/vm-name-disk0/0` directly as raw block device.
- **LVM thin provisioning from day one.** Each VM disk is a thin LV backing a DRBD resource. Thin pool on each node's NVMe.
- **TRIM/discard must work end-to-end:** guest filesystem → QEMU (detect-zeroes, discard=unmap) → DRBD → LVM thin LV. Verify in phase 10.5.
- **No NFS, no cluster filesystem, no shared-nothing copy during migration.** Both nodes already have every byte via DRBD.

## Live migration — how it works
1. VM runs on node1. DRBD resource is Primary/Secondary.
2. Temporarily enable dual-primary: `drbdadm net-options --allow-two-primaries=yes <resource>`
3. Promote node2 to primary. Both nodes now have local read-write access. No data copying.
4. `virsh migrate --live` moves RAM state only. QEMU on node2 reads/writes its local DRBD device.
5. Migration completes. Demote node1 to secondary. Disable dual-primary.
6. Zero storage I/O over the network during migration. Only RAM transfer.

## HA failover — how it works
- Watchdog on each node monitors: other node reachable? witness (MikroTik) reachable? DRBD connected?
- Other node down + witness reachable = promote DRBD, start VMs on survivor.
- No automatic failback. Failed node returns as secondary, resyncs, waits for admin decision.
- Witness prevents split-brain: if you can't reach the witness either, assume YOU are isolated, don't promote.

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
2. **Networking** — Direct cable between second NICs for DRBD (10.99.0.x). First NIC through MikroTik with br0 bridge for management/VM traffic.
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
