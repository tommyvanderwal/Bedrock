# Daemon unification — single `bedrock-d` process

The two daemons that own cluster decisions — `bedrock-net` (mesh,
election, witness) and `bedrock-mgmt` (FastAPI + orchestrator) — are
collapsed into one process so all live state shares a single address
space. No more file IPC, no more "two daemons drifted" failure mode.
The CLI stays separate (a thin HTTP client to `127.0.0.1:8001`).

## In scope (folded into `bedrock-d`)

| Old daemon | Source | Role in unified daemon |
|---|---|---|
| `bedrock-net` | `installer/lib/netd.py` | netd thread (blocking loop) |
| `bedrock-mgmt` | `mgmt/app.py` + `mgmt/orchestrator.py` | asyncio (FastAPI + tasks) |
| `bedrock-fence-watchdog` | bash | **removed** — operator can troubleshoot in alpha/beta |

## Stays separate (third-party, external-by-design, or cosmetic)

The cosmetic / browser-facing helpers are NOT cluster-decision code
paths, so they keep their own small systemd units; `bedrock-d` neither
imports nor owns them (it could `systemctl` them later if needed):

- `bedrock-mdns` (`installer/lib/mdns_responder.py`) — mDNS responder
- `bedrock-redirect` (`installer/lib/http_redirect.py`) — HTTP :80 → :8443
- `bedrock-cert-refresh` (`installer/lib/cert_manager.py`) — TLS cert renewal

Genuinely separate processes (third-party or external-by-design):

- `bedrock-rqlited`, `bedrock-rqlited-arbiter` — cluster Raft store
- `bedrock-weed-{master,volume,filer,s3}` — SeaweedFS
- `bedrock-vm`, `bedrock-vl`, `bedrock-vmagent`, `bedrock-vlagent`
- `node_exporter`, `vm-exporter`, `cockpit`

## Architecture

```
bedrock-d (single Python process)
├── BedrockState (shared in-memory object, locked where needed)
├── netd thread        — netd.run_daemon(shared_state=state)
│                         owns: Daemon (peer_liveness, neighbours, ws, …)
│                         decides: election, witness, no-quorum marker write
├── asyncio event loop (main thread)
│   ├── FastAPI / uvicorn on 8443 (HTTPS, LAN-reachable)
│   │     (8444 plain-HTTP bootstrap before a cert exists)
│   ├── FastAPI / uvicorn on 127.0.0.1:8001 (HTTP, loopback) [bedrock CLI dials this]
│   ├── rqlite_subscriber task
│   ├── no_quorum_responder task
│   ├── boot_orchestrator task
│   ├── converge_retry task
│   └── backup_scheduler task
└── shutdown: SIGTERM → stop_event.set() → netd thread exits → uvicorn stops
```

The loopback listener is `127.0.0.1:8001`, NOT 8080 — `weed-volume`
binds `0.0.0.0:8080` on every node, and `0.0.0.0` already covers
loopback, so any 8080 bind here would `EADDRINUSE`. HTTP :80 → :8443
redirect, mDNS, and cert renewal stay in their own units (see above),
so they are not tasks/threads on this loop.

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
- `/etc/bedrock/cluster.json` — GONE (deleted 2026-05-26). Cluster topology lives only in rqlite; the subscriber no longer writes this file and consumers query rqlite directly via `cluster_state.load_cluster()` (read level `none`, so it works without quorum).
- `/etc/bedrock/state.json` — KEPT as the only per-node on-disk cluster file: this node's identity + derived role + master URL, written crash-durably so cold boot has a role before rqlite is reachable. The subscriber still projects it on each revision.
- `/run/bedrock-no-quorum` — KEPT (it's a useful debugging signal and the orchestrator's `no_quorum_responder` still uses it). Becomes a write of `state.no_quorum_marker_present = True` AND a file-write for visibility.

## How it's wired

1. **`BedrockState`** lives in `installer/lib/state_shared.py` and holds the live `netd.Daemon` plus the orchestrator snapshot.
2. **netd entry** — `netd.run_daemon(shared_state=state)` runs the blocking mesh/election/witness loop on the netd thread; it observes `state.stop_event`.
3. **orchestrator** — `_SNAPSHOT` / `_PREV_SNAPSHOT` / `_LAST_LOG_IDX` / `_SERVICES_STARTED` are kept in lockstep with `state`; `orchestrator.attach_state(state)` wires it in, and the FastAPI startup hook spawns the tasks.
4. **`installer/bedrock-d`** entry script composes everything: build state, start the netd thread, `orchestrator.attach_state(state)` + `cluster_arbiter.attach_state(state)`, then `mgmt_app.serve_main()` runs uvicorn. (mDNS, redirect, and cert-refresh are NOT spun up here — they stay in their own units.)
5. **Single systemd unit `bedrock-d.service`** replaces `bedrock-net.service` + the implicit-via-`dashboard_install` `bedrock-mgmt.service`.
6. **install.sh / iso-build** ship the single `bedrock-d` executable + `bedrock-d.service`. The cosmetic units (`bedrock-mdns`, `bedrock-redirect`, `bedrock-cert-refresh`) still ship alongside it.
7. **`mgmt_install` + `agent_install`** enable `bedrock-d.service` once.
8. **e2e** — `test_e2e_offline.sh` passes with the one daemon.

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
- On `bedrock-d` restart: boot_orchestrator re-reads cluster state from rqlite (via `cluster_state.load_cluster()`), converges to the current cluster role, re-arms tasks. On-disk `state.json` supplies this node's identity/role before rqlite is reachable.
