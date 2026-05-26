# `bedrock-d` — the unified Bedrock daemon (concept)

One Python process per node owns every Bedrock cluster-decision code
path. The CLI is a thin HTTP client.

```
bedrock-d process (1 per node)
├── BedrockState           — one shared object, locks where needed
├── netd thread            — mesh probes, election, witness IO
└── asyncio main loop
    ├── FastAPI (8443 HTTPS + 8080 loopback)
    └── tasks: rqlite_subscriber, no_quorum_responder,
                boot_orchestrator, converge_retry, backup_scheduler
```

**Cluster comm**: rqlite (separate Raft process per node). All
in-process subsystems read/write `BedrockState` directly — no
`/run/bedrock/*.json` IPC any more.

**Stays separate** (third-party or external by design):
rqlited, weed-*, vm/vl, vmagent/vlagent.

**At arm's length** (cosmetic, no cluster decisions):
bedrock-cert-refresh, bedrock-mdns, bedrock-redirect — their own
small systemd units. bedrock-d can lifecycle them via `systemctl`
when needed.

**No watchdog**: single-daemon design means we troubleshoot
a stuck bedrock-d directly via journalctl + systemctl restart.

**Failure model**: if `bedrock-d` crashes, systemd `Restart=on-failure`
brings it back. State recovers from rqlite + on-disk
state.json/cluster.json. VMs/DRBD keep running through the brief
gap.
