# Bedrock — product C4 overview (draft)

**Status:** draft — product-facing system map for GitHub. Complements the
engineer-oriented [`../c4-architecture.md`](../c4-architecture.md) and
[`../architecture.md`](../architecture.md). Level-3 inside `bedrock-d` here
reflects the **current** netd / cluster thread split.

## Positioning

Bedrock is an **open-source local HA virtualization platform**: AlmaLinux 10
nodes running KVM, per-VM DRBD, live migration, and a built-in dashboard —
without Corosync or a Proxmox-style framework.

It targets **VMware refugees and MSPs** who need:

| Pillar | What first-class means |
|--------|------------------------|
| **1 → N growth** | Start on one box; add a second for HA; scale further |
| **2-node HA** | Two servers + a cheap witness is a supported topology, not an afterthought |
| **Integrated backup** | Kopia into S3 / S3-compatible / filesystem, from the same control plane |
| **Integrated object store** | SeaweedFS S3 on every node; cluster filer on the arbiter VIP |
| **Thin blast radius** | One DRBD resource per VM disk — not one shared storage cluster for all VMs |

---

## Level 1 — System context

Operator manages a Bedrock cluster. External storage is optional but central
to **2-node quorum** (witness) and **durable backups** (often the same share
or an S3 bucket).

```mermaid
C4Context
    title Bedrock system context

    Person(operator, "Operator", "Dashboard HTTPS 8443, bedrock CLI, SSH")

    System(bedrock, "Bedrock cluster", "1 to N AlmaLinux nodes. KVM, per-VM DRBD, mesh election, rqlite, SeaweedFS S3, Kopia backups. One node holds arbiter VIP.")

    System_Ext(witness, "Witness store", "Bedrock Echo UDP appliance or shared fileshare. Passive AEAD slot store for quorum tie-break.")

    System_Ext(backup_tgt, "Backup target", "S3, S3-compatible, or filesystem. Often same fileshare as witness.")

    System_Ext(guests, "Guest VMs", "Workloads. Cattle, pet, or vipet HA tiers.")

    Rel(operator, bedrock, "Manage cluster, VMs, backups, storage")
    Rel(bedrock, witness, "Write and read AEAD witness slots", "UDP 12321 or slot files")
    Rel(bedrock, backup_tgt, "Kopia snapshot repository")
    Rel(bedrock, guests, "Run and fail over VMs", "KVM libvirt")
```

### Why the witness is first-class for 2-node HA

Without a third voting node, classic majority quorum leaves two nodes stuck
or split-brained. Bedrock uses **weighted votes** (node = 100, witness = 1)
so a witness is a **pure tie-breaker**: decisive only on an exact even split,
never able to override a node majority. See
[`../cluster-quorum-spec.md`](../cluster-quorum-spec.md).

---

## Level 1b — Product capabilities map

Same system, viewed as capability planes rather than processes.

```mermaid
flowchart TB
    op["Operator"]

    subgraph bedrock["Bedrock cluster"]
        direction TB
        ctrl["Control plane<br/>bedrock-d · rqlite · arbiter VIP"]
        mesh["Mesh underlay<br/>100.64 loopbacks · multipath routes"]
        compute["Compute<br/>KVM · libvirt · live migrate"]
        block["Block HA<br/>LVM thin · per-VM DRBD"]
        obj["Object / S3<br/>SeaweedFS master · volume · filer · S3"]
        bak["Backup<br/>Kopia LV snap → stream"]
        obs["Observability<br/>VictoriaMetrics · VictoriaLogs"]
    end

    wit["Witness<br/>Echo or fileshare"]
    tgt["Backup target<br/>S3 or filesystem"]
    vm["Guest VMs"]

    op --> ctrl
    ctrl --> mesh
    ctrl --> compute
    ctrl --> block
    ctrl --> obj
    ctrl --> bak
    ctrl --> obs
    ctrl --> wit
    bak --> tgt
    compute --> vm
    block --> compute
    obj -.-> bak
```

---

## Level 2 — Cluster containers (2-node HA highlighted)

The **smallest production HA** shape: two compute nodes + one witness.
Three-plus nodes still use the same stack; the witness remains useful but is
less often pivotal.

```mermaid
C4Container
    title Bedrock two-node HA cluster

    Person(operator, "Operator")

    System_Boundary(cluster, "Bedrock cluster") {

        System_Boundary(na, "Node A mgmt master") {
            Container(bda, "bedrock-d", "Python", "Mesh, election, API, sagas, backup scheduler")
            Container(rqla, "rqlite plus VIP services", "rqlite SeaweedFS", "Local Raft voter plus arbiter rqlite, filer, S3 on VIP")
            Container(dataa, "Data plane A", "kernel libvirt", "DRBD LVM KVM")
        }

        System_Boundary(nb, "Node B compute") {
            Container(bdb, "bedrock-d", "Python", "Same binary, follower until failover")
            Container(rqlb, "rqlite local", "rqlite", "Always-on Raft member")
            Container(datab, "Data plane B", "kernel libvirt", "DRBD secondary, KVM ready to promote")
        }

        ContainerDb(underlay, "Mesh underlay", "Linux routes", "Per-node 100.64 slash32 on lo, 169.254 multipath for DRBD and control")
    }

    System_Ext(wit, "Witness", "Echo or fileshare. Breaks 1v1 election tie.")
    System_Ext(bak, "Backup target", "S3 or filesystem for Kopia")

    Rel(operator, bda, "HTTPS dashboard on VIP or node")
    Rel(bda, wit, "Witness slots")
    Rel(bdb, wit, "Witness slots")
    Rel(bda, bak, "Kopia backups")
    Rel(bdb, bak, "Kopia backups from home node")
    Rel(bda, underlay, "Control and DRBD paths")
    Rel(bdb, underlay, "Control and DRBD paths")
    Rel(dataa, datab, "Per-VM DRBD replication", "via mesh paths")
    Rel(bda, rqla, "Promote demote with VIP")
```

**On every node (collapsed above):** weed volume + S3 endpoint, exporters,
Victoria stack where configured, libvirt. The **arbiter VIP**
(`100.X.Y.254`) hosts the extra rqlite voter, SeaweedFS filer, and the
operator-facing sticky endpoint after failover.

---

## Level 2 — Single node containers

Identical image on every host. VIP-only services start when this node is
elected / holds the arbiter role.

```mermaid
C4Container
    title One Bedrock node

    Person(operator, "Operator")

    System_Boundary(node, "One Bedrock node") {

        Container(bd, "bedrock-d", "Python", "Unified daemon: mesh thread, cluster thread, asyncio mgmt")

        Container(rql, "bedrock-rqlited", "rqlite", "Local Raft store for cluster state and sagas")

        Container(vip, "VIP-only services", "rqlite SeaweedFS", "Arbiter rqlite, weed filer, sticky S3 IAM when holding .254")

        Container(weed, "SeaweedFS volume and S3", "Go", "Object data plane on every node")

        Container(data, "Block and compute", "DRBD LVM KVM", "Thinpool, per-VM DRBD, libvirt domains")

        Container(kopia, "Kopia", "CLI", "Invoked by bedrock-d for backup and restore")

        Container(obs, "Observability", "Victoria exporters", "Metrics and logs")
    }

    System_Ext(ext, "Witness and backup storage", "Echo, fileshare, external S3")

    Rel(operator, bd, "Dashboard and CLI via API")
    Rel(bd, rql, "State and saga durability")
    Rel(bd, data, "VM lifecycle and failover")
    Rel(bd, weed, "Lifecycle and health")
    Rel(bd, vip, "Arbiter promote demote")
    Rel(bd, kopia, "Snapshot create restore")
    Rel(bd, obs, "Scrape targets")
    Rel(bd, ext, "Witness IO and backup repo")
    Rel(vip, data, "Filer DB on cluster DRBD mount")
    Rel(kopia, ext, "Repository I/O")
```

---

## Level 3 — Inside `bedrock-d` (current)

One OS process, three cooperative loops, shared `BedrockState`. No
file-based IPC for live control-plane decisions.

```mermaid
flowchart TB
    subgraph bd["bedrock-d — one Python process"]
        direction TB
        state["BedrockState<br/>netd_lock · cluster_lock · snapshot_lock"]

        subgraph netd["netd thread — mesh"]
            hello["UDP hellos / adjacency"]
            rib["TCP mesh RIB"]
            routes["Route install"]
            l2["ICMP · LLDP/MNDP · ARP defense"]
        end

        subgraph cluster["cluster thread — quorum"]
            hb["Protocol-4 election HB"]
            wit["Witness Echo / fileshare"]
            elect["Election · fence_view"]
            arb["Arbiter VIP promote / demote"]
        end

        subgraph aio["asyncio — mgmt"]
            api["FastAPI :8443 and :8001"]
            orch["Orchestrator · sagas · converge"]
            backup["Backup scheduler · Kopia"]
        end
    end

    netd --> state
    cluster --> state
    aio --> state

    state --> rql["rqlited"]
    arb --> vip[".254 · arbiter rqlite · filer"]
    orch --> virt["libvirt"]
    orch --> drbd["DRBD / LVM"]
    backup --> kop["Kopia → S3 or FS"]
    wit --> echo["Witness store"]
```

| Thread | Owns | Must not own |
|--------|------|--------------|
| **netd** | Neighbours, RIB, routes, L2 extras | Election / VIP takeover |
| **cluster** | HB, witness, quorum, arbiter | Route table mutation |
| **asyncio** | API, sagas, backup, calm reconcile | Realtime election timing |

---

## Level 2 — Data planes (backup + S3 + block)

How the three storage stories sit next to each other.

```mermaid
flowchart LR
    subgraph block["Block HA — VM disks"]
        guest["Guest FS"] --> qemu["QEMU raw"]
        qemu --> drbd["/dev/drbd by-res"]
        drbd --> lv["LVM thin data + meta"]
    end

    subgraph obj["Object — SeaweedFS"]
        s3api["weed-s3 every node"]
        vol["weed-volume every node"]
        filer["weed-filer on VIP<br/>DB on cluster DRBD"]
        master["weed-master Raft min 3,N"]
        s3api --> vol
        s3api --> filer
        filer --> master
    end

    subgraph bak["Backup — Kopia"]
        snap["LVM thin snap"] --> stream["dd stream"]
        stream --> repo["Kopia repo"]
        repo --> tgt["S3 / S3-compat / filesystem"]
    end

    lv -.-> snap
```

| Plane | Unit of HA | Failure domain |
|-------|------------|----------------|
| VM disk | Per-VM DRBD (pet 2-way / vipet 3-way) | That VM only |
| Cluster singleton | `cluster` DRBD `min(3,N)` | Arbiter + filer + S3 IAM |
| Object data | SeaweedFS collections | Per-collection replica policy |
| Backup | External Kopia repo | Independent of live cluster |

---

## 2-node failover sketch

```mermaid
sequenceDiagram
    participant A as Node A master
    participant B as Node B
    participant W as Witness
    participant V as Arbiter VIP services

    Note over A,B: Healthy: A holds .254, both write witness slots
    A--xB: Partition or A dies
    A->>W: Slots go stale / lose HOSTING
    B->>W: Read slots — witness tip breaks 100 vs 100
    B->>B: Election → Leader
    B->>V: Promote cluster DRBD, claim .254, start arbiter/filer
    Note over B,V: Dashboard and S3 sticky endpoint follow VIP
    B->>B: Start pet/vipet VMs per failover_order
```

Safety hinges on: **node-majority always wins**, witness only on exact ties,
and arbiter takeover gated on **exact DRBD current-UUID** match against the
dying master's witness marker — not on "whoever shouts loudest."

---

## What this is deliberately not

- Not an external control plane (no separate "manager appliance" required)
- Not a shared cluster filesystem on the VM disk path
- Not Corosync / Pacemaker / PVE HA manager
- Not Nutanix-scale distributed storage for every byte

---

## Related docs

| Doc | Role |
|-----|------|
| [`../architecture.md`](../architecture.md) | Ports, components, orientation |
| [`../c4-architecture.md`](../c4-architecture.md) | Earlier C4 draft (L3 partially stale) |
| [`../c4-scenarios.md`](../c4-scenarios.md) | Runtime failure flows |
| [`../cluster-quorum-spec.md`](../cluster-quorum-spec.md) | Witness vote math + takeover |
| [`../storage-architecture.md`](../storage-architecture.md) | LVM / DRBD / SeaweedFS |
| [`../snapshots-and-backup.md`](../snapshots-and-backup.md) | Kopia backup design |
| [`../daemon-unification.md`](../daemon-unification.md) | `bedrock-d` process model (update for cluster thread) |
| [`BEDROCK.md`](../../BEDROCK.md) | Product reference + principles |
