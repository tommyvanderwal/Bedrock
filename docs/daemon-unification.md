# Daemon unification — single `bedrock-d` process

Goal: collapse every Bedrock-owned Python daemon into one process so
all state lives in one address space. No more file IPC, no more
"two daemons drifted" failure mode. CLI stays separate.

## In scope (folded into `bedrock-d`)

| Old daemon | Source | Role in unified daemon |
|---|---|---|
| `bedrock-net` | `installer/lib/netd.py` | netd thread (blocking loop) |
| `bedrock-mgmt` | `mgmt/app.py` + `mgmt/orchestrator.py` | asyncio (FastAPI + tasks) |
| `bedrock-mdns` | `installer/lib/mdns_responder.py` | mdns thread |
| `bedrock-redirect` | `installer/lib/http_redirect.py` | second uvicorn instance (port 80) on same loop |
| `bedrock-cert-refresh` | `installer/lib/cert_manager.py` | asyncio task (24h interval) |
| `bedrock-fence-watchdog` | bash | **removed** — operator can troubleshoot in alpha/beta |

## Stays separate (third-party or external-by-design)

- `bedrock-rqlited`, `bedrock-rqlited-arbiter` — cluster Raft store
- `bedrock-weed-{master,volume,filer,s3}` — SeaweedFS
- `bedrock-vm`, `bedrock-vl`, `bedrock-vmagent`, `bedrock-vlagent`
- `node_exporter`, `vm-exporter`, `cockpit`

## Architecture

```
bedrock-d (single Python process)
├── BedrockState (shared in-memory object, locked where needed)
├── netd thread        — netd_loop(state, stop_event)
│                         owns: Daemon (peer_liveness, neighbours, ws, …)
│                         decides: election, witness, no-quorum marker write
├── mdns thread        — mdns_responder.run(state, stop_event)
├── asyncio event loop (main thread)
│   ├── FastAPI / uvicorn on 8443 (HTTPS, LAN-reachable)
│   ├── FastAPI / uvicorn on 8080 (HTTP, loopback) [bedrock CLI dials this]
│   ├── HTTP redirect on port 80 (folded into FastAPI as catch-all)
│   ├── rqlite_subscriber task
│   ├── no_quorum_responder task
│   ├── boot_orchestrator task
│   ├── converge_retry task
│   ├── backup_scheduler task
│   └── cert_refresh_loop task (24h timer)
└── shutdown: SIGTERM → stop_event.set() → threads exit → uvicorn stops
```

## Shared state (`installer/lib/state_shared.py`)

```python
@dataclass
class BedrockState:
    # netd-owned (single-writer = netd thread)
    netd: Optional[netd.Daemon] = None          # the netd Daemon obj
    netd_lock: RLock                            # readers from asyncio

    # orchestrator-owned (single-writer = subscriber task)
    snapshot: dict                              # current rqlite snapshot
    prev_snapshot: dict                         # for reactor diffs
    last_log_idx: int = 0
    services_started: bool = False
    snapshot_lock: RLock                        # readers from FastAPI

    # cross-cutting (any subsystem writes/reads)
    no_quorum_marker_present: bool = False      # netd writes True, no_quorum_responder writes False
    self_node_name: str = ""
    self_loopback_ip: str = ""

    # Stop signaling
    stop_event: threading.Event
```

Locks are RLock; nothing holds them across `await` points. Readers
copy-out before processing.

## What dies (no longer needed in production)

- `/run/bedrock/mesh_neighbors.json`, `switch_neighbors.json`, `physical_topology.json` — these were netd→mgmt IPC. Now direct reads from `state.netd`.
- `/etc/bedrock/cluster.json`, `/etc/bedrock/state.json` — KEPT as on-disk caches for the `bedrock` CLI + post-crash recovery, but the **authoritative** source is now `state.snapshot` and `state.netd` in RAM. The subscriber still writes these files for back-compat.
- `/run/bedrock-no-quorum` — KEPT (it's a useful debugging signal and the orchestrator's `no_quorum_responder` still uses it). Becomes a write of `state.no_quorum_marker_present = True` AND a file-write for visibility.

## Steps — in order, each independently testable

1. **Create `BedrockState`** in `installer/lib/state_shared.py`. Wire the existing `netd.Daemon` as an attribute. No behaviour change yet.
2. **Refactor netd entry** — split `run_daemon()` into `init_daemon(state)` + `netd_loop(state, stop_event)`. The old `main()` keeps working (creates state, calls both).
3. **Refactor orchestrator** — replace `_SNAPSHOT`, `_PREV_SNAPSHOT`, `_LAST_LOG_IDX`, `_SERVICES_STARTED` globals with attributes on a `state` parameter. Each task function takes `state`. `start_all(state)` instead of `start_all()`.
4. **Create `installer/bedrock-d`** entry script. Composes everything: state, netd thread, mdns thread, FastAPI startup hook calls `orchestrator.start_all(state)`, uvicorn runs.
5. **Single systemd unit `bedrock-d.service`** — replaces `bedrock-net.service` + the implicit-via-dashboard_install `bedrock-mgmt.service`.
6. **install.sh / iso-build** — drop separate `bedrock-net`/`bedrock-mdns`/`bedrock-redirect`/`bedrock-cert-refresh` files + units; ship single `bedrock-d`.
7. **`mgmt_install` + `agent_install`** — enable `bedrock-d.service` once.
8. **e2e** — same `test_e2e_offline.sh` should pass with one daemon.

## Risk

Single process means one crash takes everything down. Per user
direction: no watchdog, no auto-reboot. We'll troubleshoot
crashes directly in alpha/beta. The kernel + systemd `Restart=on-failure`
on `bedrock-d.service` is enough — if Python segfaults or `OOMKilled`,
systemd restarts us.

## What survives a crash in v1 alpha

- rqlite (separate process) keeps the cluster state durable
- DRBD continues replicating
- libvirt / running VMs keep running
- weed / weed-volume keep serving data
- On `bedrock-d` restart: boot_orchestrator re-reads state from rqlite, converge to current cluster role, re-arm tasks. State.json + cluster.json on disk help bootstrap before rqlite is reachable.
