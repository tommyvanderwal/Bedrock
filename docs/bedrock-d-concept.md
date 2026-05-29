# `bedrock-d` — the unified Bedrock daemon (concept)

One Python process per node owns every Bedrock cluster-decision code
path. The CLI is a thin HTTP client to `127.0.0.1:8001`.

```
bedrock-d process (1 per node)
├── BedrockState           — one shared object, locked where needed
├── netd thread            — mesh probes, election, witness IO,
│                            .254 arbiter, routing
└── asyncio main loop
    ├── FastAPI: :8443 HTTPS (dashboard + LAN mgmt API)
    │            127.0.0.1:8001 HTTP (local CLI, auth-exempt)
    └── orchestrator tasks: rqlite_subscriber, boot_orchestrator,
        no_quorum_responder, converge_retry, backup_scheduler,
        cluster_tier_watcher, saga_resume, self_heal
```

**Shared state, no file IPC**: every in-process subsystem reads/writes
the single `BedrockState` object directly. Live cluster decisions cross
no `/run/bedrock/*.json` boundary.

**Cluster-wide state** lives in rqlite — a separate Raft process per
node (`bedrock-rqlited`, mTLS HTTPS 4001 / Raft 4002). `bedrock-d`
reads it via `cluster_state.load_cluster()` (read level `none`, so it
works without quorum).

**Stays separate** (third-party or external by design):
`bedrock-rqlited`, `bedrock-weed-*`, vm/vl, vmagent/vlagent.

**At arm's length** (cosmetic, no cluster decisions):
`bedrock-cert-refresh.timer`, `bedrock-mdns`, `bedrock-redirect`
(:80→:8443) — their own small systemd units; `bedrock-d` neither
imports nor owns them, and can lifecycle them via `systemctl` if a
need arises.

**No watchdog**: with one daemon, a stuck `bedrock-d` is diagnosed
directly via `journalctl -u bedrock-d` + `systemctl restart`.

**Failure model**: on crash, systemd `Restart=on-failure` (3 s) brings
`bedrock-d` back. Cluster topology rehydrates from rqlite via
`cluster_state.load_cluster()`; this node's identity/role from on-disk
`/etc/bedrock/state.json`. VMs and DRBD keep running through the gap.
A netd-thread crash is logged but does not kill the process — the mgmt
loop keeps serving the dashboard so the operator can diagnose.
