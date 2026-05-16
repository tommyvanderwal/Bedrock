# Bedrock

Local-infrastructure HA virtualization platform for Homelab, MSPs and small fleets.
One AlmaLinux 10 box → cluster of 1–N nodes running KVM VMs with per-VM DRBD
replication, live migration, witness-driven failover, and a built-in
dashboard.

No corosync. No PVE framework. Just plain libvirt + DRBD + LVM with
a thin orchestrator on top.

> **Status** — pushing v1.0. Storage tiers, cluster log + protocol, dashboard,
> VM lifecycle (cattle / pet / vipet), live migration, import/export, and
> backups (Kopia, S3 / S3-compatible / filesystem) are working end-to-end on
> the testbed. Hardening + power-yank validation are the remaining items.

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
│   ├── lib/rustfs-patches/     ← upstream RustFS bug-fix patches (kept for the
│   │                              issue at rustfs/rustfs#2795 — RustFS isn't
│   │                              shipped in v1.0 but may return in v1.1+)
│   ├── bedrock                 ← the operator CLI
│   └── mgmt.tar.gz             ← packaged dashboard (FastAPI + Svelte build)
├── mgmt/                       ← the dashboard service
│   ├── app.py                  ← FastAPI backend (REST + WebSocket)
│   ├── orchestrator.py         ← cluster reactor: log subscriber, fence
│   │                              responder, boot orchestrator, target reconcile
│   ├── backup.py               ← Kopia orchestration (LV snapshot + dd | kopia)
│   ├── tasks.py                ← in-process task registry for long ops
│   ├── ui/                     ← Svelte 5 frontend (build/ is shipped)
│   └── novnc/                  ← bundled noVNC for in-browser console
├── rust/bedrock-rust/          ← realtime cluster-protocol daemon (election,
│                                 fence, replicated log; written in Rust for
│                                 sub-second timing)
├── testbed/                    ← nested-KVM lab harness (spawn 1–4 sim nodes,
│                                 install repo HTTP server, e2e test)
└── dev-witness/                ← witness daemon for the dev box during testing
```

## How the pieces connect at runtime

```
    Operator's browser                Operator's CLI
            │                                │
            ▼                                ▼
    ┌───────────────┐               ┌───────────────┐
    │  /backups,    │  HTTP/WS      │  bedrock      │
    │  /vm/<name>,  │ ────────────▶ │  init / vm /  │
    │  /vms/new …   │               │  backup …     │
    └───────────────┘               └───────────────┘
                       \           /
                        ▼         ▼
                   ┌─────────────────┐    on every node
                   │ bedrock-mgmt    │    (FastAPI :8080)
                   │ (mgmt/app.py)   │
                   │  ┌───────────┐  │
                   │  │orchestrat.│  │ ── reactor reacts to
                   │  └───────────┘  │     each cluster-log entry
                   │  ┌───────────┐  │
                   │  │  backup   │  │ ── kopia + LV snapshots
                   │  └───────────┘  │
                   └─────┬───────────┘
                         │ Unix socket /run/bedrock-rust.sock
                         ▼
                   ┌─────────────────┐
                   │ bedrock-rust    │ ── replicated log, election,
                   │ (Rust daemon)   │     fence marker. Sub-second.
                   └─────┬───────────┘
                         │ TCP :8200 between peers
                         ▼
                  ┌──────────────────┐
                  │ libvirtd, DRBD,  │ ── unchanged stock pieces
                  │ qemu-kvm, LVM,   │
                  │ kopia (backup)   │
                  └──────────────────┘
```

The [docs/architecture.md](docs/architecture.md) page has a fuller component
+ port + data-flow diagram with everything mapped to file locations.

## Quick start (1-node lab)

```bash
# On a fresh AlmaLinux 9 minimal, as root:
curl -sSL http://<repo-host>:8000/install.sh | bash
bedrock init --name my-cluster
# Open http://<this-host>:8080
```

For multi-node, run `bedrock join` on the second/third node pointing at the
first. See `docs/actions/init-cluster.md` and `docs/actions/join-cluster.md`.

## Backups

v1.0 ships a **Kopia-based** backup engine integrated into the dashboard:

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
full v1 architecture and `docs/actions/backup-*.md` /
`docs/actions/vm-{backup,restore}.md` for per-action sequence diagrams.

## RustFS patches (v1.1 candidate)

`installer/lib/rustfs-patches/` holds the patch series for the RustFS
shared-lock leak we found and reported upstream
([`rustfs/rustfs#2795`](https://github.com/rustfs/rustfs/issues/2795)),
plus the reproducer + sweep results referenced from that issue.

RustFS is **not** shipped as a backend in v1.0 — Kopia + S3-compatible
target is the v1 path. RustFS is a candidate for v1.1+ once the upstream
fixes land (we're tracking the patches we applied locally). The artifacts
under `installer/lib/rustfs-patches/` and `docs/scenarios/rustfs-*.md` are
kept stable so the bug-report links keep resolving.

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

The testbed mirrors the v1 layout: each sim plugs into the LAN bridge
(DHCP-assigned `192.168.2.x`) plus three isolated mesh bridges
(`bedrock-mesh-{1,2,3}`). bedrock-net discovers per-NIC link-local
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
