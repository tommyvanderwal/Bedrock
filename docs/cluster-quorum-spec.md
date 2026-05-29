# Cluster quorum + witness — spec

## Purpose
The witness lets a 2-node cluster make a safe arbiter failover when the peer link is gone, and lets a single node cold-boot without overwriting data the cluster advanced behind its back. **All decisions are local to the requesting node.** Witnesses are passive K/V stores; they store and return slot bytes signed by the cluster — no logic, no clocks, no policy.

## Witness shape
- **One slot per cluster node.** Slot key = `node_id`, a single byte ∈ 1–250. `node_id` = last octet of the node's `100.X.Y.N/32` loopback (already deterministic in `rqlite_setup.py`).
- 254 = arbiter VIP slot key, 255 = broadcast, 251–253 reserved, 0 unused.
- **Each node OWNS its slot.** The witness rejects a write whose body's `n` field disagrees with the AEAD-verified sender. Within a cluster, the cluster_key is the auth boundary; cross-node spoofing is out of the threat model.
- The witness has NO concept of master, election, claim, "bless", or "accept". It stores last-write per slot and returns all slots on every reply.
- Backends are interchangeable: bedrock-echo (UDP), fileshare (one file per slot, atomic write+rename), multi-witness quorum (post-v1.0).

## Slot payload (the data the witness stores per slot)
The payload is **AEAD-encrypted with `/etc/bedrock/cluster.key`** (32 bytes). The witness only ever stores+returns the ciphertext; it does NOT decrypt and CANNOT decrypt. Other cluster nodes verify and decrypt locally.

Plaintext fields (msgpack):
```
{
  n:           node_id,            # 1 byte 1-250 — must match envelope's `n`
  ts_writer:   u64 epoch_ms,       # the writer's local clock, signed
  tag:         u8 bitflag,         # bit 0 = lms ("last man standing")
                                   # bits 1-7 = reserved (must be 0)
  marker:      bytes,              # "most relevant marker" for this slot's
                                   # purpose. For the arbiter slot: the
                                   # `cluster` resource's DRBD current-UUID
                                   # (hex), read via debugfs data_gen_id
  marker_kind: u8,                 # 1 = drbd-arbiter-uuid (others reserved)
}
```

`ts_writer` is the **node's own clock**. Readers use their **own** clock for freshness: `now_local_ms - ts_writer_ms ≥ 10_000` → stale. Cluster nodes are NTP-synced (chronyd); the 10 s threshold pairs with the 10-missed-heartbeat leader-loss detector (`netd.MASTER_LOSS_MISSES = 10`) so the witness declares the master's slot gone at the same moment a survivor concludes leader-loss. Implemented as `witness.SLOT_STALE_MS = 10_000`.

`tag` as a bitflag (not a string) lets us add future flags (e.g. maintenance, draining) without a wire bump. Today only bit 0 = `lms` is defined.

`marker` is the "single most relevant state fingerprint" for what the slot guards. Today only the arbiter slot is exercised; its marker is the `cluster` singleton's DRBD `current-uuid`. The witness doesn't interpret marker bytes; readers do.

## Wire protocol (envelope)
UDP/12321 (echo) or atomic file write `slot-<NN>.bin` (fileshare). Either way:

```
b"BREC"                                # 4-byte magic, lets receivers drop garbage cheaply
| 12-byte nonce                        # random per packet; replay-protected
| ChaCha20-Poly1305(cluster_key, nonce, plaintext)   # encrypt + auth in one
| 16-byte Poly1305 tag                 # included in the AEAD output
```

`plaintext` is `msgpack(envelope)`:
```
{
  v:        1,                  # protocol version
  t:        "hb",               # only message type from node → witness
  cu:       cluster_uuid,       # 16 bytes; segregates clusters sharing one witness
  n:        node_id,            # writer's claimed id (must match plaintext.n above)
  slot:     <encrypted-slot-payload-bytes from above>,
}
```

Witness reply:
```
b"BREC" | nonce | ChaCha20-Poly1305(cluster_key, nonce, plaintext) | tag

plaintext = msgpack({
  v:         1,
  t:         "ack",
  echo_id:   "<witness instance id>",
  cu:        cluster_uuid,
  slots:     {                          # ALL slots the witness has for this cluster_uuid
    node_id: <encrypted-slot-payload-bytes>,
    ...
  },
})
```

The reader decrypts the envelope (proves cluster membership), then decrypts each slot (proves it was signed by a current cluster member). A reply that fails AEAD verification at either layer is silently dropped.

## Slot lifecycle
- Each node writes its own slot every **1 s** with current `(ts_writer, tag, marker)`.
- The witness REPLACES on every accepted write. No history.
- A slot is **stale** (from the reader's POV) when `now_local_ms - slot.ts_writer ≥ 10 s`. 10 missed 1 s writes, pairing with the 10-missed-beat leader-loss detector (`netd.MASTER_LOSS_MISSES = 10`). Stale does NOT mean cleared — see INV-7 for `tag.lms`.
- The witness **retains** an unrefreshed slot for **72 h** before it may drop the entry from its own store. This is the witness's storage-cleanup policy, not a cluster-side LMS-clear mechanism — see INV-7.
- **Cluster-side membership filter**: a reader silently ignores any returned slot whose `node_id` does not correspond to a node currently present in this reader's local rqlited's `nodes` table (the cluster-wide source of truth, locally readable on every node via Raft-replicated rqlite). This is what makes "decommission the dead node" — a routine operator action that removes the row from the `nodes` table — also undo a stuck LMS belonging to that dead node, without ever touching the witness. The witness may still have the entry until 72 h aging; the cluster just doesn't care.

## Arbiter takeover protocol (load-bearing — every step matters, NO rqlite involved)
The arbiter rqlited is THE service being recovered, so this whole protocol uses **only** the witness + local commands (`drbdadm`, `ip`, `mount`, `systemctl`). The cluster's rqlite Raft is not consulted and cannot be — at N=2 it has no quorum until the arbiter is back up.

P is a node whose local election (mesh peer-liveness only) says it should become the new arbiter-host. P:

1. **Identify the dying master M.** Use the in-process record of "who was last writing the arbiter slot" from prior witness replies. (At cold boot with no record, M is unknown and step 2 examines the arbiter slot whoever owns it.)
2. **Inspect M's slot in the most recent witness reply:**
   - Stale `(age ≥ 10 s)` AND `tag.lms == 0` → M died without going solo. Continue to step 3.
   - Stale AND `tag.lms == 1`                 → M went solo and never cleared the LMS bit. **REFUSE takeover.** LMS does not time out — see INV-7. Operator must clear M's LMS via the explicit override command (see `docs/operator-overrides.md`) before any peer can claim.
   - Fresh AND `tag.lms == 0`                 → M is alive, the mesh is just flapping locally. P stays follower.
   - Fresh AND `tag.lms == 1`                 → M is the legitimate last-man-standing. P stays follower.
3. **Local DRBD freshness check** — read the `cluster` resource's current-UUID (via debugfs `data_gen_id`, `drbdadm dump-md` fallback for a detached resource — `cluster_arbiter._read_local_drbd_uuid()`; note `drbdadm current-uuid` does not exist in DRBD 9.x). Decrypt M's slot, read `marker` (M's last-known DRBD UUID).
   - **EXACT MATCH** between P's local UUID and `slot[M].marker` → P's local data is identical to M's last witness-published state → safe to take over.
   - Mismatch → P missed writes M did before dying. **Refuse to promote.** Log clearly, surface to operator. Manual `drbdadm invalidate` on P (or wait for M to come back) is the only way out.
4. **Write own slot `tag.lms = 1`** with current local `(ts_writer, drbd_uuid)`.
5. **Read it back from the NEXT witness reply.** Decrypt own slot. Verify `tag.lms == 1` AND `marker == local drbd_uuid` AND `ts_writer` is the one we just wrote. If not (write packet lost) → retry step 4. Refuse to promote after 3 failed retries.
6. **Promote, in order:**
   - `drbdadm primary cluster`
   - `mount $(drbdadm sh-dev cluster) /var/lib/bedrock/cluster`
   - `ip addr add 100.X.Y.254/32 dev lo`
   - `systemctl start bedrock-rqlited-arbiter`
   - start filer + s3.
7. **Rqlite quorum returns** as soon as the arbiter rqlited joins the surviving per-node rqlited (P's own). Only then — outside this protocol — can `set_mgmt_master(self)` be written through rqlite. The witness is not consulted again until the next state change.

Same protocol, abbreviated path, applies when the current master M itself loses peer-but-keeps-witness ("Scenario B" below): M is already at steps 6/7 done; it only needs steps 4+5 (flip own tag to lms, readback-verify).

## Arbiter self-demote protocol
A node currently arbiter-host self-demotes when its local election (mesh peer-liveness + witness reachability) concludes NoQuorum for ≥ 5 ticks (≈ 5 s), OR step 3 of takeover refuses with UUID mismatch on a fresh boot:
1. Stop filer + s3 → stop `bedrock-rqlited-arbiter` → `ip addr del 100.X.Y.254/32` → unmount → `drbdadm secondary cluster`.
2. Write own slot `tag.lms = 0`. This write requires the witness to be reachable. If the write fails (witness unreachable), the LMS bit stays set on the witness and the cluster is now in a stuck-LMS state — see INV-7. Retry on every subsequent election tick for as long as we are demoted-but-still-running. If we shut down or die before the write lands, operator intervention is required (see `docs/operator-overrides.md` "Clear stuck LMS"). Do not assume the slot will time out — it will not.

Order matters: services down before the network state changes. INV-1 forbids `.254` on two nodes simultaneously even for a tick.

## Cold-boot protocol
Node N starts up with `/etc/bedrock/cluster.key` + `/etc/bedrock/state.json` present:
1. Send a probe + heartbeat. Wait up to 5 s for any reply.
2. If a reply landed: decrypt slot[self]. Compare its `marker` against the local `cluster` resource's current-UUID (debugfs `data_gen_id`):
   - Match → up-to-date. Eligible for arbiter-host (election still decides whether to take over).
   - Local older → cluster advanced without us. Refuse to promote, stay follower until reconciled.
   - Local newer → we crashed mid-write before publishing. Allowed; re-publish via next heartbeat.
3. No reply in 5 s AND peer reachable on mesh → defer to peer + normal election (witness optional at boot).
4. No reply AND no peer → refuse to promote. Wait.

## Scenarios (everything reduces to one of these)

### A — master loses peer AND witness ("isolated alone")
- M's writes stop landing on witness. M's slot ages.
- M's local election: 100 self of (100+100+1 = 201), majority 101 → NoQuorum → after ≈ 5 s streak, M self-demotes. M is now off; nothing running.
- Survivor P sees mesh peer M gone, slot[M] stale, `tag.lms == 0`. P runs the takeover protocol. UUID match (P was a healthy Secondary up to M's last write) → P promotes.

### B — master loses peer only, keeps witness ("last man standing")
- M sees peer down via mesh. M runs steps 4+5 of takeover (it's already hosting): flip own slot `tag.lms = 1`, read back confirmed. Keeps hosting and refreshing each tick.
- P sees mesh peer M gone but slot[M] fresh + `tag.lms == 1` → P stays follower (takeover step 2).

### C — split with witness reachable from both sides ("symmetric")
- Both try Scenario-B's flip-to-lms. The first one whose write lands AND is reflected in the OTHER side's next reply wins by being seen as fresh+lms. Loser's takeover step 2 trips on the winner's slot → loser stays follower.
- Race resolution = one round-trip; typically < 2 s.

### D — witness gone, peer alive
- Both nodes lose witness contact; own-slot writes silently fail.
- Mesh election: 100+100 = 200 of 200+0 = 200, majority 101. Each side sees the other reachable → quorate. Cluster keeps running on current master. No takeover attempted (witness reachability is a precondition for the takeover protocol's step 5 readback).

### E — N=2, no witness, peer gone (full split, no witness)
- Each side: 100 of 200, below majority (101). Both NoQuorum → both self-demote. Cluster halts safely. Operator decides which side to bring up.

### F — cold-boot one node with cluster advanced elsewhere
- Local `cluster` current-UUID (debugfs `data_gen_id`) < `slot[self].marker` → refuse to promote. Either sync from peer (DRBD will do this automatically once peer reachable) or operator-reconcile.

## Invariants
- **INV-1** — at most one node holds `.254` at a time. Enforced by: takeover refuses on fresh-other-slot (step 2), refuses on UUID mismatch (step 3), requires own-write readback (step 5) before promoting (step 6). Self-demote stops services before any cluster-visible state change (step 1).
- **INV-2** — each node writes only its own slot. The witness checks `n_in_envelope == n_in_plaintext` and rejects mismatches.
- **INV-3** — tag transitions are local-only. `lms` is set on this-node-decided-to-go-solo, cleared on this-node-self-demoted. Never copied from another slot.
- **INV-4** — `ts_writer` from the writer is the freshness reference; reader uses its own clock for comparison. No witness clock involved.
- **INV-5** — UUID comparison for takeover is **exact equality**, never `≥`. Local newer-than-slot means we crashed mid-write OR we are looking at our own slot (the protocol skips that). Local older-than-slot means the cluster advanced without us — refuse.
- **INV-6** — the arbiter takeover protocol uses ONLY witness + local commands. No rqlite call is on the takeover critical path. rqlite is the service being recovered.
- **INV-7** — **`tag.lms = 1` never times out, and a missing slot is treated as worst-case.** The 10 s staleness rule and the 72 h witness-retention rule do not clear LMS; they only change interpretation. The only paths to a cluster state where a previously-set LMS no longer blocks takeover:
  - (a) The slot owner is alive and successfully writes `tag.lms = 0` to the witness (requires both online simultaneously).
  - (b) The operator decommissions the slot owner (`bedrock node leave …` / equivalent removes it from the rqlite `nodes` table — the cluster-wide source of truth). Surviving nodes then *ignore* any witness slot for that removed node-id because it is no longer a known cluster member. The witness still stores the slot until 72 h retention drops it, but nobody reads it.
  - (c) The operator re-keys the cluster's witness identity (new `cluster_uuid` and/or new `cluster.key`). Old encrypted slot payloads can no longer be decrypted by any cluster member; the slot map re-populates from current members' heartbeats within a few seconds. On a fileshare-backend witness this is equivalent to deleting the slot files.

  **Witness-loses-state case is NOT a clear.** If the witness reboots and comes back empty (or any individual slot is missing), readers must assume the worst case for that slot: the missing slot *might* have held `tag.lms = 1`. Until the slot is repopulated by a fresh heartbeat from the current cluster member it belongs to (and observed by the reader), the reader treats that slot as `lms = 1`-possible and refuses takeover. A slot belonging to a node that is in the rqlite `nodes` table but not currently writing remains "worst-case unknown" until operator intervention via (b) or (c).

  Reason: a node that died with LMS set may have written data after the last DRBD sync to its peer; allowing automatic peer takeover — including via "witness forgot" — would risk silent data loss. The safety-over-availability trade is intentional. Only the paranoid survive.

## Why no witness clock (and why the writer's clock works)
- A witness that generates timestamps must have an accurate clock. ESP32 + battery-backed RTC + drift handling on a small appliance is significant complexity for one job.
- Cluster nodes already NTP-sync (chronyd in every install). Drift between cluster members on a healthy LAN is sub-second.
- Reader uses its own clock. `now_local - slot.ts_writer ≥ 10 s` absorbs the worst plausible per-node skew with margin.
- Replay protection comes from AEAD nonces, not from timestamps. A stale-but-valid slot is fine: it just looks correctly old.

## Backends
| backend | mechanism | persistence | notes |
|---|---|---|---|
| `bedrock-echo` (UDP/12321) | ESP32 firmware or Python stub. AEAD-encrypted packets, slot map kept in memory + flushed to flash on every accepted write. | Required in production. Testbed stub may be RAM-only. | Failure mode: LAN flap → all slots stale → cluster halts safely. |
| fileshare (SMB / NFS / S3) | One file per slot, `slot-<NN>.bin`. Atomic `tmp + rename` per write. Same payload, same envelope, no UDP framing. | The fileshare itself. | Failure mode: network flap → same. Latency tolerance: writes must land in < 1 s. |
| multi-witness quorum | Send each write to all N configured witnesses. A read returns the slot only if a majority of witnesses agree. | Each backend's own. | Post-v1.0. Wire protocol already permits multiple endpoints in `WitnessState.discovered`. |

## Timing knobs (one place for tuning)
| event | value | rationale |
|---|---|---|
| slot refresh interval | 1 s | matches election tick |
| stale threshold | 10 s | 10 missed 1 s writes; **pairs with the 10-missed-beat leader-loss detector** (`netd.MASTER_LOSS_MISSES = 10`) so the witness declares the master gone exactly when a survivor concludes leader-loss. Implemented as `witness.SLOT_STALE_MS = 10_000`. |
| takeover-readback retries | 3 | covers transient UDP loss; 4 s total |
| cold-boot witness wait | 5 s | bounded; healthy witness replies in < 100 ms |
| NoQuorum self-demote streak | 5 ticks (≈ 5 s) | shorter than the stale threshold so demote precedes peer takeover |
| AEAD nonce size | 12 bytes (96 bits) | ChaCha20-Poly1305 default |
| Poly1305 auth tag | 16 bytes | standard |

## Implementation
This spec is implemented and in production. The pieces live in:
- `installer/lib/witness.py` — passive K/V slots (`slots: dict[int, Slot]`), `set_own_slot(tag, marker)` writer, `read_slot(node_id)` against the latest reply, AEAD-msgpack wire format on UDP 12321.
- `installer/lib/election.py` — decides leader/follower/NoQuorum from mesh peer-liveness + witness votes; vote weights `VOTES_PER_NODE = 100`, `VOTE_PER_WITNESS = 1`.
- `installer/lib/cluster_arbiter.py` — `promote_to_arbiter_host` runs takeover steps 1–6 in order (no `set_mgmt_master` on the critical path); `demote_arbiter_host` writes `tag.lms = 0` at the end.
- `testbed/bedrock_echo_stub.py` — Python BedRock Echo witness for the testbed: a pure encrypted K/V slot server, no claim/bless logic.

The transport is AEAD (ChaCha20-Poly1305) over the msgpack body with the nonce in the envelope; the witness is a passive per-node K/V slot store (no `blessed_master`, no claim accept/reject, no holddown); each node writes only its own slot; takeover gates on peer-slot inspection + exact local UUID equality + own-slot readback; freshness uses the writer's clock compared against the reader's own clock; and the takeover path consults nothing in rqlite — it is pure witness + local commands. (Note: mesh probes/adverts on UDP 7732/7733 still use HMAC-SHA256 over the cluster_key — only the *witness* moved to AEAD.)

## Out of scope (today)
- Multi-witness quorum reads + writes (planned post-v1.0; wire protocol already permits multiple endpoints in `WitnessState.discovered`).
- Automated DRBD-divergence recovery. Step 3 surfaces; operator runs `drbdadm invalidate` manually.
- Per-node sub-keys. The cluster_key is the auth boundary; within-cluster impersonation is not in the threat model.
- Slot history / audit log on the witness. The witness keeps the latest write only; cluster nodes' bedrock-d logs are the audit trail.

## Why the witness is "critical only at failover and cold boot" (D-18)
- Steady-state: nodes refresh slots, but no decision depends on the slot contents — the running master keeps running, followers keep following. Mesh probes do all the work.
- Failover: the takeover protocol is the SOLE path to becoming arbiter-host. Witness reachability + slot readback is mandatory.
- Cold boot: the slot's `marker` is the only third-party record of the current dataset generation. Without it, a stale boot would silently overwrite peer progress.

Between those two moments, the witness can be offline and the cluster keeps running. This is what makes a low-cost ESP32 a viable third observer for production HA: it has to be right at two crisp moments, not continuously.
