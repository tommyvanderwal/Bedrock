# Bedrock

Local-infrastructure HA virtualization platform for Homelab, MSPs and small fleets.
One AlmaLinux 10 box → cluster of 1–N nodes running KVM VMs with per-VM DRBD
replication, live migration, witness-driven failover, and a built-in
dashboard.

No corosync. No PVE framework. Just plain libvirt + DRBD + LVM with
a thin orchestrator on top.

Cluster state lives in rqlite. The mesh/election/witness protocol, dashboard,
VM lifecycle (cattle / pet / vipet), live migration, import/export, and backups
(Kopia to S3 / S3-compatible / filesystem) all run as one Python daemon per node.

## What's in here

```
.
├── BEDROCK.md                  ← project reference (design, target market, roadmap)
├── docs/                       ← engineer-facing operational + design docs
│   ├── README.md               ← entry point — start here
│   ├── architecture.md         ← whole stack on one page
│   ├── snapshots-and-backup.md ← v1 backup design (Kopia, hash floor)
│   ├── actions/                ← per-button / per-API-call walkthroughs
│   ├── scenarios/              ← failure modes (power loss, split-brain, …)
│   ├── components/             ← per-service references
│   └── reference/              ← logs, ports, files, HTTP API
├── installer/                  ← OOB install flow served over HTTP
│   ├── install.sh              ← `curl … | bash` bootstrap
│   ├── lib/                    ← Python libraries (state, daemon_setup, etc.)
│   ├── bedrock                 ← the operator CLI
│   └── mgmt.tar.gz             ← packaged dashboard (FastAPI + Svelte build)
├── mgmt/                       ← dashboard service (runs in bedrock-d)
│   ├── app.py                  ← FastAPI backend (REST + WebSocket); :8443 HTTPS
│   │                              (LAN, operator-authed) + 127.0.0.1:8001 (local CLI)
│   ├── orchestrator.py         ← calm reactor: rqlite subscriber, no-quorum
│   │                              responder, boot orchestrator, reconcile
│   ├── backup.py               ← Kopia orchestration (LV snapshot + dd | kopia)
│   ├── tasks.py                ← in-process task registry for long ops
│   ├── ui/                     ← Svelte 5 frontend (build/ is shipped)
│   └── novnc/                  ← bundled noVNC for in-browser console
├── testbed/                    ← nested-KVM lab harness (spawn 1–4 sim nodes,
│                                 install repo HTTP server, e2e test)
└── dev-witness/                ← Python Bedrock-Echo witness for local dev; the
                                  production target is the Bedrock-Echo ESP32
                                  firmware (passive UDP/12321 AEAD K/V store)
```

The whole node runs as one Python daemon, `bedrock-d`: the realtime netd
(mesh, election, witness, `.254` arbiter, routing) on its own thread, plus
the asyncio mgmt/orchestrator. See
[`docs/daemon-unification.md`](docs/daemon-unification.md).

## How the pieces connect at runtime

```
    Operator's browser                Operator's CLI
            │                                │
            ▼                                ▼
    ┌───────────────┐               ┌───────────────┐
    │  /backups,    │  HTTPS/WS     │  bedrock      │
    │  /vm/<name>,  │ ────────────▶ │  init / vm /  │
    │  /vms/new …   │  :8443        │  backup …     │
    └───────────────┘               └───────────────┘
                       \           /
                        ▼         ▼
                  ┌──────────────────────┐   on every node
                  │   bedrock-d          │   (single Python process)
                  │  ┌────────────────┐  │
                  │  │ mgmt FastAPI   │  │ ── :8443 HTTPS (LAN, operator-authed)
                  │  │ (mgmt/app.py)  │  │     + 127.0.0.1:8001 (local CLI)
                  │  └────────────────┘  │
                  │  ┌────────────────┐  │
                  │  │ orchestrator   │  │ ── calm loop: rqlite-driven
                  │  │ (calm loop)    │  │     reconcile, capacity, placement
                  │  └────────────────┘  │
                  │  ┌────────────────┐  │
                  │  │ netd thread    │  │ ── critical loop: mesh probes,
                  │  │ (1 Hz)         │  │     election, witness, takeover
                  │  └────────────────┘  │
                  └─────┬────────────────┘
                        │
                        ▼
                  ┌─────────────────────┐
                  │ rqlite (state) +    │ ── cluster state in rqlite (per-node
                  │ bedrock-echo        │     + arbiter on the `cluster` DRBD
                  │ (witness, passive)  │     singleton); witness on UDP/12321
                  └─────┬───────────────┘     (passive AEAD K/V slot store)
                        ▼
                  ┌──────────────────┐
                  │ libvirtd, DRBD,  │ ── stock pieces
                  │ qemu-kvm, LVM,   │
                  │ SeaweedFS,       │
                  │ kopia (backup)   │
                  └──────────────────┘
```

The [docs/architecture.md](docs/architecture.md) page has a fuller component
+ port + data-flow diagram with everything mapped to file locations.

## Quick start (1-node lab)

```bash
# On a fresh AlmaLinux 10 minimal, as root:
curl -sSL http://<repo-host>:8000/install.sh | bash
bedrock init --name my-cluster
# Open https://<this-host>:8443   (http://<this-host> redirects there)
```

For multi-node, run `bedrock join` on the second/third node pointing at the
first. See `docs/actions/init-cluster.md` and `docs/actions/join-cluster.md`.

## Backups

A **Kopia-based** backup engine is integrated into the dashboard:

- **Set a target** at `/backups` — S3 / S3-compatible (Wasabi, B2, R2, MinIO,
  QNAP-S3, …) or a filesystem path. Encryption password is set **once**;
  storing it externally is the operator's responsibility.
- **Back up** a VM with the yellow `Backup` button on the VM page. Bedrock
  takes an LV thin snapshot, streams it through kopia (`dd | kopia
  snapshot create --stdin-file=disk0.img`) — block-fidelity, no temp files.
  Kopia content-addresses on **BLAKE2B-256** (≥256-bit floor enforced at
  every connect; weaker repos are refused).
- **Restore** with the `Restore` button on either the per-VM card or the
  cluster-wide list at `/backups`. FUSE-mounts the snapshot, dd's the
  bytes back to the LV — byte-identical restore.
- **List & delete** snapshots from the same UI; deletes drop the kopia
  manifest, GC of underlying chunks happens at the next `kopia
  maintenance run` from the master.

See [`docs/snapshots-and-backup.md`](docs/snapshots-and-backup.md) for the
architecture and `docs/actions/backup-*.md` /
`docs/actions/vm-{backup,restore}.md` for per-action sequence diagrams.

## Documentation

The [docs/](docs/) directory is structured around three axes:

- **[Actions](docs/actions/)** — what every dashboard button / API call does,
  in order, with failure modes and recovery.
- **[Scenarios](docs/scenarios/)** — failure modes (power loss, split-brain,
  network partition, node rejoin) — what bedrock does, and where to look
  when it doesn't.
- **[Reference](docs/reference/)** — log lines, ports, files, HTTP/WS API.

The design baseline is in [`BEDROCK.md`](BEDROCK.md) and the architecture
overview in [`docs/architecture.md`](docs/architecture.md).

## Development

The repo is built and tested on the `testbed/` nested-KVM harness:

```bash
cd testbed
sudo ./spawn.py prereqs   # one-time: libvirt + image
sudo ./spawn.py up 4      # spawn 4 sim nodes
nohup ./serve.py &        # serve installer over :8000
./test_e2e.sh             # full multi-scenario validation
```

The testbed mirrors the production layout: each sim plugs into the LAN bridge
(static `192.168.2.20x`, above the router's DHCP pool) plus three isolated mesh
bridges (`bedrock-mesh-{1,2,3}`). bedrock-net discovers per-NIC link-local
addresses via NetworkManager, builds the cluster path table, and
installs metric-ordered host routes for the per-cluster `/24` it
derives from `cluster_uuid` inside RFC 6598 (`100.64.0.0/10`). See
[`docs/06-mesh-network.md`](docs/06-mesh-network.md) for the full
mesh design.

## License

Bedrock is currently licensed under the **MIT License**.

**Future relicensing:**  
The copyright holder (Tommy van der Wal) reserves the right to relicense the entire project (including all contributions) to the GNU General Public License version 2 (GPLv2) at any time.  
By contributing code, documentation, or any other material to this repository, you explicitly agree that your contributions may be relicensed under GPLv2 in the future.

You are free to use, modify, and distribute Bedrock under the current MIT terms until any such change occurs.

## Contributing

By submitting any contribution (code, documentation, pull requests, issues, etc.), you acknowledge and agree to the following:

- Your contribution is licensed under the current MIT License.
- The copyright holder (Tommy van der Wal) reserves the irrevocable right to relicense your contribution (and the entire project) under the **GNU General Public License version 2** (GPLv2) at any time in the future.

No separate CLA document is required. Submitting a contribution constitutes your agreement to these terms.
