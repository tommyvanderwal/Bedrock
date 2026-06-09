# installer/lib/state_shared.py

The single in-memory state object shared by every thread and task inside the
unified `bedrock-d` daemon. netd (mesh/election/witness), the mgmt FastAPI
handlers, and the orchestrator tasks all run in one process and read/write one
`BedrockState` instance instead of passing data through files. It defines the
state container, the two locks that guard it, and a pair of helpers that hand
out safe read snapshots to readers working outside the locks.

## Functions / Classes

### `class BedrockState` (dataclass)
The single source of truth for in-process Bedrock state. Constructed once at
startup (`state = BedrockState(stop_event=...)`); subsystems then attach their
own state objects onto it (e.g. `state.netd = netd.init_daemon(...)`).

Fields:
- `stop_event: threading.Event` — lifecycle stop signal.
- `self_node_name`, `self_loopback_ip`, `cluster_uuid: str` — per-node identity
  copied from `state.json` at startup; never changes. Convenience copies so
  subsystems don't re-read the file.
- `no_quorum_marker_present: bool` — sticky no-quorum semaphore. netd's election
  sets it True on NoQuorum + holddown; the orchestrator's `no_quorum_responder`
  sets it False once cleanup completes and quorum is back. Kept in lockstep with
  the on-disk `/run/bedrock-no-quorum` file for external debug tooling.
- `netd: Optional[Any]` — the `netd.Daemon` object (typed `Any` to avoid a
  circular import). Single-writer is the netd thread.
- `netd_lock: threading.RLock` — guards `netd` while the netd thread mutates it;
  asyncio readers copy-out under it.
- `last_election_outcome: str` — last election result string, for netd transition
  logging and the dashboard status.
- `netd_ws: Optional[Any]` — the netd `WitnessState` (socket + discovered Echo
  endpoints + slot cache + `own_marker`/`own_tag`). Published by `netd.run_daemon`
  so `cluster_arbiter` can read peers' slots and set its own LMS slot at promotion.
  `cluster_arbiter` owns `own_tag` (LMS); netd only refreshes `own_marker` each tick.
- `snapshot: dict`, `prev_snapshot: dict`, `last_log_idx: int` — the live cluster
  snapshot projected from rqlite by the subscriber task, the previous snapshot,
  and the last applied rqlite log index. FastAPI handlers read `snapshot` directly.
- `snapshot_lock: threading.RLock` — guards the snapshot dict while
  `rqlite_subscriber` replaces it.
- `services_started: bool` — rendezvous flag between `boot_orchestrator` and
  `no_quorum_responder`.
- `scheduled_inflight: set` — per-task transient state; touched only by its owning
  task, so no lock.

### `snapshot_copy(state) -> dict`
Return a deep copy of the current cluster snapshot for a reader about to work
outside the lock.
- **In:** `state` → the `BedrockState`.
- **Out:** `copy.deepcopy(state.snapshot)`, taken under `snapshot_lock`. No side
  effects.

### `netd_status_view(state) -> dict`
Return a JSON-serialisable view of netd's current state for the dashboard
`/api/mesh` and `/api/witness` endpoints.
- **In:** `state` → the `BedrockState`.
- **Out:** a dict built under `netd_lock`. If `state.netd` is None, returns
  `{"running": False}`. Otherwise `{"running": True, "me", "loopback_ip",
  "cluster_uuid", "election_outcome", "nics": {<nic>: {"addr", "neighbours": [
  {"peer_node", "peer_nic", "peer_loopback", "peer_link_addr", "logged_up",
  "rtt_us", "first_seen", "last_seen"}, …]}}}`. No side effects.

## How it works

One process, one `BedrockState`, two locks. Each lock protects a distinct slice
of the object and has exactly one writer:

```
            netd thread                         asyncio (mgmt + orchestrator)
                 │                                          │
   writes under  │                                          │  reads under
   netd_lock     ▼                                          ▼  netd_lock (copy-out)
        ┌──────────────────┐                       ┌────────────────────┐
        │ state.netd       │ ◀──── netd_lock ────▶ │ netd_status_view() │
        │ state.netd_ws    │                       └────────────────────┘
        └──────────────────┘
                                                              │ writes under
   rqlite_subscriber                                          │ snapshot_lock
        ┌──────────────────┐                       ┌────────────────────┐
        │ state.snapshot   │ ◀── snapshot_lock ──▶ │ snapshot_copy() /  │
        │ state.prev_…     │                       │ FastAPI handlers   │
        └──────────────────┘                       └────────────────────┘
```

`netd_lock` guards everything the netd thread owns (`netd`, neighbours, witness
state). `snapshot_lock` guards the rqlite-projected `snapshot` dict that
`rqlite_subscriber` swaps in. The discipline both helpers follow: take the lock,
copy out what you need (deep copy for the snapshot, field reads for the netd
view), release, then process outside. A lock is never held across an `await`.
The locks are `RLock` so a single thread (the asyncio task runs on the main
thread) can re-enter without deadlocking.

`netd_status_view` reads the `Daemon` defensively with `getattr` and per-field
defaults, so a half-initialised `Daemon` caught mid-startup still produces a
valid view rather than raising. It walks `nic_addrs`, and for each NIC collects
the neighbours whose `my_nic` matches that NIC (neighbours are keyed by the tuple
`(peer_node, peer_nic, my_nic)`), flattening each into a plain serialisable dict.

## Why

Holding cluster state in one shared object lets the netd thread and the asyncio
mgmt/orchestrator tasks exchange data directly within the single `bedrock-d`
process, without file-based IPC. The copy-out-under-lock pattern keeps lock hold
times short and away from `await` points, so a slow reader never stalls the
single-writer netd thread.
