# Single daemon — `bedrock-d`

One Python process per node owns every cluster decision: a **netd thread**
(mesh, election, witness, `.254` arbiter, routing) plus an **asyncio
mgmt/orchestrator** (FastAPI dashboard, saga executor, reactor). Both halves
share one in-memory `BedrockState` object, so there is no file-based IPC for
live decisions and no "two daemons drifted" failure mode. The `bedrock` CLI is
a separate executable that dials this process over HTTP on `127.0.0.1:8001`.

## What runs inside `bedrock-d`

| Subsystem | Source | Form |
|---|---|---|
| mesh / election / witness / arbiter / routing | `installer/lib/netd.py` | netd thread (blocking loop) |
| dashboard + mgmt API + orchestrator | `mgmt/app.py`, `mgmt/orchestrator.py` | asyncio (FastAPI + tasks) |

## What runs as its own systemd unit

Cosmetic / browser-facing helpers are not cluster-decision paths, so they keep
small standalone units; `bedrock-d` neither imports nor owns them:

- `bedrock-mdns` (`installer/lib/mdns_responder.py`) — mDNS responder
- `bedrock-redirect` (`installer/lib/http_redirect.py`) — HTTP `:80` → `:8443`
- `bedrock-cert-refresh.timer` (`installer/lib/cert_manager.py`) — TLS cert renewal

Third-party / external-by-design processes:

- `bedrock-rqlited`, `bedrock-rqlited-arbiter` — cluster Raft store
- `bedrock-weed-{master,volume,filer,s3}` — SeaweedFS
- `bedrock-vm`, `bedrock-vl`, `bedrock-vmagent`, `bedrock-vlagent`
- `node_exporter`, `vm-exporter`, `cockpit`

## Architecture

```
bedrock-d (single Python process)
├── BedrockState (installer/lib/state_shared.py — shared in-memory, locked)
├── netd thread        — netd.run_daemon(shared_state=state)
│                         owns: Daemon (neighbours, peer liveness, witness)
│                         decides: election, witness, no-quorum marker write
└── asyncio (main thread)
    ├── uvicorn :8443 HTTPS (LAN dashboard + mgmt API; :8444 plain-HTTP
    │     bootstrap until a cert exists) — separate thread, own loop
    ├── uvicorn 127.0.0.1:8001 HTTP (local CLI / intra-process, auth-exempt)
    │     — separate thread, own loop
    └── orchestrator tasks (mgmt/orchestrator.py start_all):
          rqlite_subscriber, no_quorum_responder, boot_orchestrator,
          backup_scheduler, converge_retry, cluster_tier_watcher,
          saga_resume, self-heal loop
shutdown: SIGTERM → state.stop_event.set() → netd thread exits →
          uvicorn returns → process ends (TimeoutStopSec=20s, then SIGKILL)
```

The loopback listener is `127.0.0.1:8001`, not `8080`: `weed-volume` binds
`0.0.0.0:8080` on every node and `0.0.0.0` already covers loopback, so an 8080
bind here would `EADDRINUSE`. The cert-less bootstrap listener uses a dedicated
`8444` for the same reason.

The 8443 and 8001 listeners run as two uvicorn instances in separate threads,
each with its own event loop. Both fire the same FastAPI `startup` hook on the
same `app`, so the hook and `orchestrator.start_all()` guard against
double-spawn under a lock (`_STARTUP_LOCK` / `_START_LOCK`); without it each
orchestrator task would start twice and the two `no_quorum_responder`s would
clobber each other's role-wait timing.

## Shared state (`installer/lib/state_shared.py`)

`BedrockState` is the single source of in-process truth. Key fields:

```python
@dataclass
class BedrockState:
    stop_event: threading.Event            # SIGTERM/SIGINT → set
    self_node_name / self_loopback_ip / cluster_uuid: str

    # netd-owned (single-writer = netd thread; asyncio copies out under lock)
    netd: Daemon | None                    # neighbours, peer liveness, witness
    netd_lock: RLock
    netd_ws: WitnessState | None           # Echo sock + slot cache; arbiter reads
    last_election_outcome: str

    # orchestrator-owned (single-writer = rqlite_subscriber)
    snapshot / prev_snapshot: dict         # current + previous rqlite projection
    last_log_idx: int
    snapshot_lock: RLock
    services_started: bool                 # boot ↔ no_quorum_responder rendezvous

    # cross-cutting
    no_quorum_marker_present: bool         # netd sets True; responder sets False
    scheduled_inflight: set                # backup_scheduler dedupe
```

Locks are `RLock`; nothing holds them across an `await`. Readers copy out under
the lock before processing. `netd_ws` is published by `netd.run_daemon` so
`cluster_arbiter`'s takeover protocol can read peers' Echo slots and set its own
LMS slot at the moment of promotion, without waiting for the slower election
path; the arbiter owns the LMS bit (`own_tag`), netd only refreshes `own_marker`
each tick.

## State, not files

- Cluster topology lives only in rqlite. Subsystems read it via
  `cluster_state.load_cluster()` (read level `none`, so it works without
  quorum); the netd `Daemon` view is read directly from `state.netd` via
  `state_shared.netd_status_view()` for the dashboard `/api/mesh` and
  `/api/witness` endpoints.
- `/etc/bedrock/state.json` is the only per-node on-disk cluster file: this
  node's identity + derived role + master URL, written crash-durably so cold
  boot has a role before rqlite is reachable. The subscriber re-projects it on
  each revision.
- `/etc/bedrock/cluster.json` is a local bootstrap file holding the rqlite peer
  list, written at init/join and read by `rqlite_setup --render-env` each boot
  (rqlite can't report its own peers before it starts). It is not a runtime
  state projection.
- `/run/bedrock-no-quorum` mirrors `state.no_quorum_marker_present` on disk for
  external debug tooling: netd's election writes it on sticky no-quorum,
  `no_quorum_responder` clears it once cleanup is done and quorum is back.

## Wiring

The `installer/bedrock-d` entry script:

1. Builds `BedrockState`, installs SIGTERM/SIGINT handlers that set
   `stop_event`.
2. Starts the netd thread (`netd.run_daemon(shared_state=state)`), a daemon
   thread that observes `stop_event`. Uncaught thread exceptions print a full
   traceback then `os._exit(1)` so systemd restarts the whole process.
3. `orchestrator.attach_state(state)` + `cluster_arbiter.attach_state(state)`,
   then sets `mgmt_app.app.state.bedrock = state`.
4. Calls `mgmt_app.serve_main()`, which binds the loopback `:8001` listener in a
   thread and the LAN listener (`:8443` with a cert, else `:8444`) on the main
   thread; FastAPI's `startup` hook fires `orchestrator.start_all()`.

`mgmt_install` and `agent_install` enable `bedrock-d.service` once
(`systemctl enable --now`). `install.sh` / iso-build ship the `bedrock-d`
executable, `bedrock-d.service`, and the standalone cosmetic units alongside it.
The SeaweedFS, rqlited, and weed units order themselves `After=bedrock-d.service`.

## Crash behaviour

`bedrock-d.service` runs `Restart=on-failure RestartSec=3s` with no
StartLimit and no external watchdog: single-process by design, the operator
troubleshoots a stuck daemon directly via `journalctl -u bedrock-d`.
`MemoryHigh=512M` / `MemoryMax=1G` catches a runaway leak. While `bedrock-d` is
down, rqlite keeps cluster state durable, DRBD keeps replicating, libvirt VMs
keep running, and weed keeps serving. On restart, `boot_orchestrator` re-reads
cluster state from rqlite, converges to the current role, and re-arms the
tasks; `state.json` supplies this node's identity and role before rqlite is
reachable.
