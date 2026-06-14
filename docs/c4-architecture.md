# Bedrock — C4 architecture (draft)

System and **container** levels only. Component-level (`bedrock-d` modules) is a
follow-up.

## Is Bedrock one system or several?

**One software system** — the cluster platform on each node. No external control
plane; whichever node holds the arbiter VIP acts as mgmt master.

| Layer inside Bedrock | Main runtime |
|----------------------|--------------|
| Control & orchestration | `bedrock-d`, `rqlited`, witness protocol |
| Block replication | DRBD, LVM thinpool |
| Compute | KVM / libvirt |
| Object store | SeaweedFS |
| Observability | VictoriaMetrics, VictoriaLogs, exporters |

**Outside Bedrock:** operator, guest VMs (workloads), witness/backup storage
(Echo appliance or shared fileshare — see below).

---

## Level 1 — System context

Overview: operator → Bedrock cluster; optional external store for quorum +
backups.

```mermaid
C4Context
    title Bedrock system context

    Person(operator, "Operator", "Browser dashboard HTTPS 8443 bedrock CLI SSH")

    System(bedrock, "Bedrock cluster", "N peer nodes same stack. Mesh election DRBD KVM rqlite dashboard. One node holds arbiter VIP at a time.")

    System_Ext(ext_store, "Witness and backup storage", "Echo UDP appliance OR shared fileshare NFS SMB. Fileshare may also host Kopia backup repo.")

    Rel(operator, bedrock, "Manage cluster and VMs")
    Rel(bedrock, ext_store, "Witness slots quorum tie-break", "Echo or fileshare")
    Rel(bedrock, ext_store, "Kopia backup target optional", "Often same fileshare")
```

### Witness backends (detail)

Configured in rqlite `witnesses` — one or more entries, mixed backends allowed:

| Backend | What it is | Quorum role |
|---------|------------|-------------|
| **Echo** | BedRock Echo on UDP 12321 — encrypted slot per node | Tie-break vote when node votes alone are tied |
| **Fileshare** | Same directory mounted at the same path on every node — `slot-NN.bin` files | Same slot protocol, disk-backed instead of UDP |

A **fileshare used for witness slots** is often the same NFS/SMB (or object mount)
already used as a **Kopia `kopia-fs` backup target** — two roles, one share. Echo
is separate hardware; it does not hold backup data.

See [`actions/witness-manage.md`](actions/witness-manage.md) and
[`actions/backup-target-set.md`](actions/backup-target-set.md).

### Logical layers (same product — not separate systems)

```mermaid
flowchart TB
    op["Operator"]
    subgraph bedrock["Bedrock cluster"]
        ctrl["Control — bedrock-d rqlite witness arbiter"]
        data["Data — DRBD LVM KVM"]
        obj["Object — SeaweedFS"]
        obs["Observability — VM VL exporters"]
    end
    ext["Witness and backup storage<br/>Echo or fileshare"]

    op --> ctrl
    ctrl --> data
    ctrl --> obj
    ctrl --> obs
    ctrl --> ext
    data --> obj
```

---

## Level 2 — Cluster overview

Typical **3-node** cluster. Every node runs `bedrock-d` + `rqlited`; only the
mgmt master runs arbiter rqlite + filer on the VIP. **No parentheses in diagram
labels** — Mermaid C4 parser is picky.

```mermaid
C4Container
    title Bedrock cluster containers overview

    Person(operator, "Operator")

    System_Boundary(cluster, "Bedrock cluster three nodes") {

        System_Boundary(na, "Node A mgmt master") {
            Container(bda, "bedrock-d", "Python", "netd election orchestrator dashboard")
            Container(rqla, "Extra on VIP", "rqlite plus filer", "arbiter voter weed filer")
        }

        System_Boundary(nb, "Node B compute") {
            Container(bdb, "bedrock-d", "Python", "Same binary follower role")
            Container(svb, "Standard stack", "mixed", "rqlited DRBD secondary libvirt weed volume")
        }

        System_Boundary(nc, "Node C compute") {
            Container(bdc, "bedrock-d", "Python", "Follower")
            Container(svc, "Standard stack", "mixed", "rqlited DRBD secondary libvirt weed volume")
        }

        ContainerDb(mesh, "Mesh underlay", "Linux routes", "100.64 loopback and 169.254 paths for DRBD and election")
    }

    System_Ext(store, "Witness and backup storage", "Echo UDP or fileshare Kopia optional")

    Rel(operator, bda, "HTTPS dashboard")
    Rel(bda, store, "Witness IO")
    Rel(bdb, store, "Witness IO")
    Rel(bdc, store, "Witness IO")
    Rel(bda, mesh, "DRBD replicate")
    Rel(bdb, mesh, "DRBD replicate")
    Rel(bdc, mesh, "DRBD replicate")
    Rel(bda, rqla, "Starts when holding VIP")
```

**Every node also runs** (collapsed in diagram): `bedrock-rqlited`, DRBD
resources, libvirt, weed volume + S3, VictoriaMetrics/Logs, exporters. See
[`architecture.md`](architecture.md) port list.

---

## Level 2 — Single node overview

Same **node image** everywhere; master-only pieces start when netd promotes
this host to arbiter.

```mermaid
C4Container
    title Bedrock single node overview

    Person(operator, "Operator")

    System_Boundary(node, "One Bedrock node") {

        Container(bd, "bedrock-d", "Python", "Unified daemon netd plus mgmt orchestrator")

        Container(rql, "bedrock-rqlited", "rqlite", "Always local Raft member")

        Container(data, "Data plane", "kernel plus libvirt", "DRBD LVM thinpool KVM domains")

        Container(weed, "SeaweedFS", "Go", "volume and S3 every node master if in Raft-3 set")

        Container(obs, "Observability", "Go Python", "VictoriaMetrics VictoriaLogs exporters")

        Container(vip_svc, "VIP-only services", "rqlite SeaweedFS", "arbiter rqlite filer when this node holds VIP")
    }

    System_Ext(store, "Witness and backup storage", "Echo or fileshare")

    Rel(operator, bd, "Dashboard and API")
    Rel(bd, rql, "Cluster state sagas")
    Rel(bd, data, "DRBD VM storage")
    Rel(bd, weed, "Lifecycle")
    Rel(bd, obs, "Metrics logs")
    Rel(bd, vip_svc, "Promote demote")
    Rel(bd, store, "Witness slots")
    Rel(vip_svc, data, "Filer on cluster DRBD mount")
```

CLI (`bedrock`) is a loopback HTTP client to `127.0.0.1:8001` — not a container.

---

## Level 3 preview — inside bedrock-d

Not formal C4 yet; shows how one process splits.

```mermaid
flowchart TB
    subgraph bd["bedrock-d one OS process"]
        subgraph netd["netd thread"]
            mesh["Mesh and routing"]
            elect["Election tick"]
            wit["Witness Echo and fileshare"]
            arb["Arbiter VIP takeover"]
        end
        subgraph mgmt["asyncio and uvicorn"]
            api["FastAPI 8443 and 8001"]
            orch["Orchestrator sagas converge failover"]
        end
        state["BedrockState shared memory"]
    end

    netd --> state
    mgmt --> state
    orch --> rql["rqlited"]
    arb --> drbd["DRBD mount systemd"]
    orch --> drbd
    orch --> virt["libvirt"]
    wit --> store["Witness and backup storage"]
```

---

## Deployment sketch

```mermaid
flowchart TB
    op["Operator"]
    subgraph ext["External optional"]
        echo["Echo witness ESP32"]
        share["Fileshare NFS SMB<br/>witness slots plus Kopia repo"]
    end

    subgraph cluster["Bedrock cluster LAN"]
        n1["Node A mini PC<br/>bedrock-d VIP holder"]
        n2["Node B bedrock-d"]
        n3["Node C bedrock-d"]
        mesh["Mesh links between nodes"]
    end

    op --> n1
    n1 --- mesh --- n2 --- mesh --- n3
    n1 & n2 & n3 -.-> echo
    n1 & n2 & n3 -.-> share
```

---

## Finetuning notes

- **L1** — kept to three boxes plus operator so Mermaid layout stays readable.
- **Witness** — Echo and fileshare are one external *role* (quorum store); fileshare
  often doubles as Kopia backup storage.
- **L2 cluster** — collapsed per-node services; expand into separate containers
  when documenting ports or failover paths.
- **Cockpit** — optional host UI, out of scope for these diagrams.
- **N equals 1** — solo rqlite, VIP on loopback, no cluster DRBD until promote.

---

## References

- [`architecture.md`](architecture.md)
- [`daemon-unification.md`](daemon-unification.md)
- [`storage-architecture.md`](storage-architecture.md)
- [`cluster-quorum-spec.md`](cluster-quorum-spec.md)
- [`actions/witness-manage.md`](actions/witness-manage.md)
- [`actions/backup-target-set.md`](actions/backup-target-set.md)
