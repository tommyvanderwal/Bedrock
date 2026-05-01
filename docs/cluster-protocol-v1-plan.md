# Cluster protocol v1 — implementation plan

Companion to [`docs/cluster-protocol-design.md`](cluster-protocol-design.md)
(the architecture doc the operator pasted). This file says **what code
to write, in what order, with what scope cuts**, so we ship a
testable v1 quickly without prematurely committing to the full design.

Scope of "v1": enough machinery to scale **1 → 2 → 3 nodes and back**
on the testbed, with a real Rust daemon driving an append-only
hash-chained log and talking Echo protocol to a witness on the LAN.
Anything not load-bearing for that scenario is a deferred phase.

---

## Status of the existing codebase (relative to the new design)

What we already have that the new design subsumes:
- `bedrock-failover.py` (HA orchestrator, polling-based) — older
  heartbeat design. **Will be replaced** by `bedrock-rust` lease loop.
- `bedrock-witness` podman container on MikroTik (HTTP `POST
  /heartbeat`, 611 KB Rust binary). **Will be replaced** by an Echo
  ESP32 witness — but the existing HTTP witness can serve as a
  software stand-in during early Rust development.
- All cluster state today lives in `/etc/bedrock/cluster.json` +
  `/etc/bedrock/state.json`, written ad-hoc by `tier_storage.py` and
  `mgmt_install.py`. **Will be replaced** by materialized views
  derived from the log.

Decision: v1 keeps the existing `cluster.json` / `state.json` writers
in place and adds the Rust log alongside as the *emerging* canonical
source. Fully removing the dual-write happens in phase 6, after the
log + replay is reliable enough to be the only writer.

---

## Phases

### Phase 0 — Foundation: workspace + dev-box network

| # | Task | Notes |
|---|---|---|
| 0.1 | Create Cargo workspace at `rust/` with three crates: `bedrock-rust` (daemon), `bedrock-witness` (ESP32-targeted firmware crate), `bedrock-protocol` (shared Echo + log types). | Workspace `Cargo.toml` at repo root or `rust/Cargo.toml`. |
| 0.2 | Bridge the dev box's physical NIC `enx0050b6924411` (192.168.2.121) into a Linux bridge `br0` so libvirt sims appear on the operator's LAN (192.168.2.0/24) instead of the libvirt-NAT'd 192.168.100.x. | Today: `br-bedmgmt` is a libvirt-managed NAT bridge; sim nodes get 192.168.100.x and aren't reachable from the operator's machine. **Operator approval needed before applying** — switching `enx0050b6924411` from a plain NIC to a bridge slave drops the host's IP for ~1 second; reconnect will be transparent if the script writes directly. |
| 0.3 | Update `testbed/networks/bedrock-mgmt.xml` to reference the host bridge `br0` (not libvirt's auto-created one). Re-define the libvirt network. Sim nodes then DHCP from the operator's home router. | Single `<bridge name='br0'/>` change; reload via `virsh net-define`. |
| 0.4 | Add a permanent witness host on the LAN (initially the dev box itself, port 9443). Update spawn.py's cloud-init template so sim nodes know the witness host. | The MikroTik witness in `docs/03-witness-and-orchestrator.md` is the production target; for development the dev box can serve. |

**Output:** sim nodes reachable from operator's laptop at LAN IPs; Cargo workspace ready.

---

### Phase 1 — Rust daemon: append-only log with hash chain

| # | Task | Notes |
|---|---|---|
| 1.1 | `bedrock-protocol` crate: `LogEntry { index: u64, epoch: u64, prev_hash: [u8;32], payload_kind: u8, payload: Bytes }` + `LogEntryHash { hash(entry) -> [u8;32] }`. | SHA-256, length-prefixed payload, MessagePack for payload encoding (per design §8). |
| 1.2 | `bedrock-rust::log::Log` — opens / creates a directory `/var/lib/bedrock/log/`, files `00000001.log` etc, segment-rolled at fixed size. Operations: `append(payload) -> (index, hash)`, `read(index) -> entry`, `range(from, to) -> iterator`, `latest() -> (index, hash)`, `replay()`. | No locking yet — single writer. fsync per append in v1; batch fsync deferred. |
| 1.3 | First entry hard-coded by `init`: payload `"Hello World! <cluster-uuid>"` with `index=1, epoch=1, prev_hash=zero`. Distinguishes a re-initialised cluster from a continued one (per design §4). | The cluster UUID stays the one Python already generates. |
| 1.4 | CLI shim: `bedrock-rust log {init,append,show,verify}`. Lets us drive the log from a shell during development. `verify` walks the chain checking hashes. | One binary, subcommands. |

**Test:** `bedrock-rust log init`, append 100 entries, `bedrock-rust log verify` reports all-good. Mutate one byte, verify reports the divergence point.

---

### Phase 2 — Python ↔ Rust IPC

| # | Task | Notes |
|---|---|---|
| 2.1 | Unix socket at `/run/bedrock-rust.sock`. Length-prefixed MessagePack frames. Request/response + server-push for committed-entry events. | One socket; multiple Python clients allowed. |
| 2.2 | Rust IPC server: `Append(payload) -> (index, hash, committed)` (blocks until log says committed — for v1 "committed" == fsynced locally; replication comes in Phase 3), `Read(from, to)`, `Subscribe()`, `Status()`, `Fence()`. | Bounded outbound queue per subscriber so a slow Python doesn't stall Rust. |
| 2.3 | Python client `bedrock.rust_ipc` — small lib: `with rust_ipc() as r: r.append(b"...")`. Used by the existing `tier_storage.set_tier_state(...)` and friends, **as a write-through** to the log. Existing JSON files keep being written too; the log is additive in v1. | Python crash → Rust unaffected, IPC reconnects on next start. |

**Test:** Python `bedrock storage init` writes to both `cluster.json` (today's path) AND the Rust log. `bedrock-rust log show` matches.

---

### Phase 3 — Rust daemon: peer transport + Echo witness client

| # | Task | Notes |
|---|---|---|
| 3.1 | `bedrock-rust::peer` — TCP listener on cluster network port (8200). Frames: `LogReplicate{from_index}`, `Heartbeat{epoch, last_index, last_hash}`, `Identify{node_id, pubkey}`. Multiple peer transports supported (per design §7); v1 starts with one per peer, multi-link in phase 7. | TCP for replication; heartbeat is also TCP for v1 (UDP/QUIC deferred). |
| 3.2 | Single-leader replication: leader's appended entries fan out to peers as `LogReplicate{entry}`; peers append in order; ack with their new `last_index`. `committed` advances when leader has acks from majority (in 2-node case, majority == both, broken by witness). | No batching yet. |
| 3.3 | **Echo protocol** spec (`bedrock-protocol::echo`): UDP, length-prefixed, fixed message types — `Hello{node_pubkey, cluster_uuid}`, `Heartbeat{epoch, last_index, last_hash, ttl_remaining_ms}`, `LeaderClaim{epoch, last_index, last_hash}`, `LeaderClaimReply{ok, peer_last_index, peer_last_hash}`. Witness records the most recent `Heartbeat` per node in memory only (no persistence — design §5). | Authenticated with the witness-specific cluster key (design §10). |
| 3.4 | Rust client implementation in `bedrock-rust::witness`. Sends Heartbeat every `heartbeat_ms` (default 1000); on `LeaderClaim` query, gets back what the witness last saw from each peer. | Client per witness; v1 supports a single witness, multi-witness in phase 7. |
| 3.5 | A throwaway Python "echo-witness" that speaks Echo, runs on the dev box. Lets us test 3.4 without ESP32 hardware. **Final v1 target is the same protocol on ESP32.** | Python implementation ~150 lines. |

**Test:** Two `bedrock-rust` processes (sim-1 + sim-2) replicate logs to each other; both heartbeat to the Python echo-witness; `LeaderClaim` from one returns the other's last-seen state.

---

### Phase 4 — Lease + self-fence

| # | Task | Notes |
|---|---|---|
| 4.1 | Lease at the witness: leader renews TTL on every heartbeat. If renewal fails for `ttl_seconds` (default 5s for direct-cable, 30s for internet-witness — per design §5), Rust starts the self-fence sequence. | Per-cluster knob. |
| 4.2 | Self-fence sequence (design §6): (1) `ip link set $iface down` on every cluster interface listed in config; (2) write `/run/bedrock-rust.fence` marker; (3) signal Python over IPC (`Fence{reason}` event); (4) wait up to 300s for Python to acknowledge cleanup; (5) `systemctl reboot`. | All cluster interfaces, no exceptions. |
| 4.3 | Python fence handler: on `Fence` event, kill VMs (`virsh destroy`), drbdadm secondary every tier, unmount, send `FenceComplete` back to Rust which can reboot earlier than the 300s timeout. | New file `installer/lib/fence_handler.py`. |
| 4.4 | Boot recovery: on startup, Rust checks for the fence marker, comes up in **secondary-only** mode, accepts replication but refuses to claim leadership unless Python explicitly says conditions are met (peer reachable + this node has highest known committed index, OR witness confirms peer-down). Booting nodes never unilaterally evict their peer (§6). | Python decides; Rust enforces. |

**Test:** Manually sever the witness link → leader self-fences within `ttl_seconds`; reboots; comes up secondary-only; rejoins cleanly.

---

### Phase 5 — Materialised views: log → cluster.json/state.json

| # | Task | Notes |
|---|---|---|
| 5.1 | Replace today's ad-hoc writes to `cluster.json` / `state.json` with: append a *log entry* describing the change, then have a Python `view_builder.py` that consumes log events and rewrites the JSON files as caches. | Keeps the file paths the same so consumers (mgmt UI, `bedrock storage status`) don't change. |
| 5.2 | Each tier-state mutation, node add/remove, master change, etc. becomes a typed log entry. The "Hello World!" entry kicks off the chain. Cluster bootstrap on join replays the log (§3) and the view_builder reconstructs the JSON files. | Replaces the workarounds we've been chasing in L28/L30. |
| 5.3 | `bedrock storage transfer-mgmt` no longer needs to manually push state.json updates — the log entry "mgmt-master-now-X" is replicated by Rust and every node's view_builder regenerates state.json identically. **L28's whole class of bug goes away.** | Same for L27's drbd_node_ids (entry "tier-X-drbd-id-assigned-Y-to-Z"). |

**Test:** `bedrock storage transfer-mgmt` followed immediately by `bedrock storage status` on every node — all show the new master without any cluster.json rsync step.

---

### Phase 6 — Cluster transitions through the log

| # | Task | Notes |
|---|---|---|
| 6.1 | `bedrock init`: writes Hello-World entry + the initial node-self entry to the log. No witness yet (single node). | One node, one log. |
| 6.2 | `bedrock join`: discovers via mgmt API, requests log replication from the current leader, writes its node-add log entry, joins as Secondary. | Operator runs `bedrock storage promote` afterwards; the promote becomes a series of log entries. |
| 6.3 | 2-node case (the design's hard case, §9): witness becomes load-bearing. Both nodes heartbeat to the witness; leader claim requires the witness's last-seen state from the peer to match this node's view. The witness is what breaks ties when the direct cable is down. | Builds on 4.1's lease. |
| 6.4 | 3+ nodes: majority-quorum (without witness) is sufficient. Witness still consulted for slow-path / re-join / parameter changes. | Majority math. |
| 6.5 | Leader-only-mode parameter change (§9): drain to one node, change parameter (TTL, witness add/remove, etc.), re-expand. Implemented as a state-machine in the leader: `enter-leader-only-mode` log entry → wait for peers to ack drained → apply param-change log entries → emit `exit-leader-only-mode` → peers resume replication. | One transition implemented end-to-end (TTL change is the smallest test case). |

**Test:** Full lifecycle 1 → 2 → 3 → back-to-1 with sentinel files preserved (the test we've been doing manually now driven by the log). Transfer-mgmt and remove-peer work without any of the L28/L30 workarounds.

---

### Phase 7 — deferred from v1, queued for v1.1

These are in the design doc but cut from v1 to keep scope tight:

- **Multi-link transport** (§7) — v1 uses one peer link; multi-link is a phase 7.x.
- **Multi-witness** (§5) — v1 supports a single witness; 3-of-5 voting is phase 7.x.
- **Snapshot + log compaction** (§8) — v1 keeps the full log; periodic snapshots come later.
- **Monthly key rotation** (§10) — v1 generates keys at install and uses them statically.
- **ESP32 firmware crate** — v1 tests against a Python echo-witness; ESP32 firmware is its own work.
- **Hardware watchdog** — v1 relies on self-fence-on-deadline only (§6).

---

## What needs operator input before we start

1. **Network change (Phase 0.2 / 0.3).** Bridging `enx0050b6924411` into `br0` will momentarily blip the host's network. SSH sessions from the operator's laptop will reconnect transparently if the change is one atomic `nmcli` operation. Approve before applying — once-per-session disruption is mild but worth confirming.

2. **Witness host for development (Phase 0.4).** Three options:
   - (a) Run the Python echo-witness on the dev box directly (192.168.2.121:9444). Simplest; no extra hardware.
   - (b) Run on the MikroTik already configured for the existing HTTP witness (192.168.2.252). Requires re-flashing or running alongside.
   - (c) Wait for the ESP32. Slowest path; blocks v1 until hardware is ready.
   I recommend (a) for v1 development. Final production target stays the ESP32.

3. **Replace existing witness immediately or run side-by-side?** The HTTP witness in `bedrock-failover.py` is currently load-bearing. v1 plan above introduces the Echo witness as additive. Once Phase 6.3 is solid, `bedrock-failover.py` and the HTTP witness can be retired. Confirm side-by-side is acceptable for the v1 dev period.

4. **Boot-time fence marker location.** Plan says `/run/bedrock-rust.fence` (tmpfs). On unclean reboot the fence-marker is gone and the node comes up clean. Acceptable, or do we need a persistent marker too?

---

## Build order for a single working session

If we do the first chunk now:

1. **Phase 0.1** — Cargo workspace skeleton (15 min)
2. **Phase 1.1 + 1.2** — Log + hash chain in `bedrock-rust` (1–2 hours)
3. **Phase 1.4** — `bedrock-rust log {init,append,show,verify}` CLI (30 min)
4. **Phase 3.3 + 3.5** — Echo protocol types + Python echo-witness (1 hour)
5. **Phase 3.4** — Rust witness client + a smoke test that one daemon heartbeats to the Python witness and `LeaderClaim` works (1 hour)

That's a coherent first-session deliverable: **a working Rust log + a working Rust ↔ Python-witness Echo round-trip on the LAN**. Phases 2, 4, 5, 6 land in subsequent sessions.

After this session the operator can:
- See the log file on disk.
- Use the CLI to verify the hash chain.
- Start the witness, start two `bedrock-rust` instances, watch heartbeats arrive at the witness.
- Issue a `LeaderClaim` from one of them and see it succeed/fail based on what the witness has seen.

---

## Questions the design doc leaves open (track here, decide as we hit them)

- **Cluster network port assignments**: 8200 peer-to-peer, 9444 witness Echo (UDP). OK?
- **Heartbeat frequency**: design says "periodic"; planning 1s for direct-cable, 5s for internet-witness. Confirm.
- **Lease TTL defaults**: 5s direct-cable, 30s internet-witness. Confirm.
- **Log segment size for rolling**: 64 MB initial. Compaction comes in phase 7.
- **What's in the payload** of the Hello-World entry beyond the cluster UUID? Plan: also include the initialising node's pubkey and the cluster's shared key (encrypted to itself, so re-replays can validate).
