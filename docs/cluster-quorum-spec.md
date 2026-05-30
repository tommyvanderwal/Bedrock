# Cluster quorum + witness — spec

## Purpose
The witness lets a 2-node cluster make a safe arbiter failover when the peer link is gone, and lets a single node cold-boot without overwriting data the cluster advanced behind its back. **All decisions are local to the requesting node.** Witnesses are passive K/V stores; they store and return slot bytes signed by the cluster — no logic, no clocks, no policy.

## Witness shape
- **One slot per cluster node.** Slot key = `node_id`, a single byte ∈ 1–250. `node_id` = last octet of the node's `100.X.Y.N/32` loopback (already deterministic in `rqlite_setup.py`).
- Keyspace: 1–250 = node slots (`witness.NODE_ID_MIN/MAX`; `_decode_slot` rejects anything outside this), 254 reserved for the arbiter VIP octet, 255 broadcast, 251–253 reserved, 0 unused. The arbiter's DRBD-UUID marker is NOT written under a slot[254]; it rides in the hosting node's OWN slot (1–250), which is what the takeover protocol reads.
- **Each node OWNS its slot.** The witness files the slot blob under the envelope's `n` (the AEAD-verified writer id) — it can't read the encrypted slot body, so it never compares the two. Reader-side, `drain_replies` drops a slot whose decrypted inner `n` ≠ the map key. Within a cluster, the cluster_key is the auth boundary; cross-node spoofing is out of the threat model.
- The witness has NO concept of master, election, claim, "bless", or "accept". It stores last-write per slot and returns all slots on every reply.
- Two backends are implemented: **bedrock-echo** (UDP 12321) and **fileshare** (a shared directory). `WitnessState.discovered` holds multiple Echo endpoints and `count_valid_confirmed` tallies each one independently — but ONLY those whose `echo_id` matches a configured `witness_id` (the identity binding below), so multiple Echo witnesses each add a vote while a rogue/removed endpoint cannot. Echo witnesses can be added by IP (directed unicast probe) or on the local L2 (broadcast). A **fileshare** witness is a directory the operator has mounted the same NFS/SMB/object share at on every node; each node writes its own `slot-<NN>.bin` there and reads the others' — `count_valid_confirmed` folds both backends into one distinct-`witness_id` tally (see `witness_file.py` and the fileshare section below). A true majority-of-witnesses quorum read is still designed-for but not yet built.

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
  kind:        u8,                 # 1 = drbd-arbiter-uuid (others reserved)
}
```

`ts_writer` is the **node's own clock**. A reader uses its **own** clock for freshness: `now_local_ms - ts_writer_ms ≥ 10_000` → stale (the witness has no clock and declares nothing). Cluster nodes are NTP-synced (chronyd); the 10 s threshold pairs with the 10-missed-heartbeat leader-loss detector (`netd.MASTER_LOSS_MISSES = 10`) so a reader treats the master's slot as gone at the same moment a survivor concludes leader-loss. Implemented as `witness.SLOT_STALE_MS = 10_000`.

`tag` as a bitflag (not a string) lets us add future flags (e.g. maintenance, draining) without a wire bump. Today only bit 0 = `lms` is defined.

`marker` is the "single most relevant state fingerprint" for what the slot guards. Today only the arbiter slot is exercised; its marker is the `cluster` singleton's DRBD `current-uuid`. The witness doesn't interpret marker bytes; readers do.

## Wire protocol (envelope)
Each node→witness packet is a UDP datagram on port 12321 (the fileshare backend writes the same slot blob to `slot-<NN>.bin` in a shared dir instead — same payload, no UDP framing). Either way:

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
  echo_id:   "<witness instance id — MUST equal the configured witness_id>",
  cu:        cluster_uuid,
  slots:     {                          # ALL slots the witness has for this cluster_uuid
    node_id: <encrypted-slot-payload-bytes>,
    ...
  },
})
```

The reader decrypts the envelope (proves cluster membership), then decrypts each slot (proves it was signed by a current cluster member). A reply that fails AEAD verification at either layer is silently dropped.

**Witness-identity binding (split-brain guard).** Cluster-key auth alone proves "some holder of the cluster secret answered" — NOT "a configured witness answered". So `drain_replies` admits and `count_valid_confirmed` counts a reply ONLY if its `echo_id` matches a configured `witness_id` (`netd` plumbs `WitnessState.configured_witness_ids` from the rqlite `witnesses` table each tick, like `member_ids`). This stops two ways an over-count → split-brain could happen: a **rogue** Echo holding the cluster key but reporting an unconfigured id, and a **just-removed** witness's still-fresh entry (which would otherwise vote until the 12 s freshness window expires). The convention is therefore **`echo_id == witness_id`** — provision each Echo with its configured id (the testbed runs the stub `--echo-id <witness_id>`). A mismatched id is dropped (fail-safe: under-count, never over-count) and `netd` logs it (rate-limited) so the misconfiguration is visible. `configured_witness_ids` falsy (None at early boot, or an empty set from a momentarily-lagging `level='none'` replica) = no filter, so a stale zero-read can't evict a live witness. A non-Python Echo packing `echo_id` as msgpack bytes/int is normalized to a string before the compare. *Future defense-in-depth:* cryptographic per-witness identity — sign the ack with the Echo's key, verify the stored `witness_pubkey` (column exists, unused today) — to also stop a rogue that KNOWS a configured id.

**Discovery (Echo-by-IP).** `netd` finds Echo witnesses by (a) a slot-less `broadcast_probe` to `255.255.255.255` (zero-config, local L2 only) AND (b) a directed `unicast_probe` to every configured Echo's `host:port` — so an Echo added BY IP that is routed/off the broadcast domain still gets probed and can vote. Replies key by `echo_id` in `discovered`, so the two paths dedupe to one endpoint. The directed-probe target must be an **IPv4 unicast literal** (the mgmt API + `_parse_echo_addr` enforce it): a hostname would block the single-threaded 1 Hz election tick on synchronous DNS, a multicast/broadcast addr would flood, and IPv6 is unreachable on the `AF_INET` witness socket.

## Slot lifecycle
- Each node writes its own slot every **1 s** with current `(ts_writer, tag, marker)`.
- The witness REPLACES on every accepted write. No history.
- A slot is **stale** (from the reader's POV) when `now_local_ms - slot.ts_writer ≥ 10 s`. 10 missed 1 s writes, pairing with the 10-missed-beat leader-loss detector (`netd.MASTER_LOSS_MISSES = 10`). Stale does NOT mean cleared — see INV-7 for `tag.lms`.
- The witness **retains** an unrefreshed slot for **72 h** before it may drop the entry from its own store (a backend storage-cleanup policy of the ESP32 firmware; the testbed stub `bedrock_echo_stub.py` keeps slots until restart). It is NOT a cluster-side LMS-clear mechanism — see INV-7.
- **Cluster-side membership filter**: a reader silently ignores any returned slot whose `node_id` is not an ACTIVE node (`state=='active'` and not in maintenance) in this reader's local rqlited's `nodes` table — netd plumbs that set onto `WitnessState.member_ids` each tick and `drain_replies` drops the rest. The `nodes` table is locally readable on every node via Raft-replicated rqlite (the cluster-wide source of truth). This is what makes "decommission the dead node" — a routine operator action that removes the row — also undo a stuck LMS belonging to that node, without ever touching the witness. The witness may still hold the entry until 72 h aging; the cluster just doesn't read it. (`member_ids=None` until netd first plumbs membership — no filtering, and the witness can't be `is_valid` either.)

## Arbiter takeover protocol (load-bearing — every step matters; no rqlite consensus)
The arbiter rqlited is THE service being recovered, so the gating uses **only** the witness + local commands (`drbdadm`, `ip`, `mount`, `systemctl`). The cluster's rqlite Raft is not consulted and cannot be — at N=2 it has no quorum until the arbiter is back up. The one rqlite touch is a `level='none'` LOCAL replica read (no quorum) to learn who the last master was and the cluster size; it is never a consensus call. Implemented in `cluster_arbiter._run_takeover_protocol()`.

P is a node whose `netd` election (mesh peer-liveness + acks) returned Leader. Before step 1, two short-circuits:
- **Steal-back guard** — if any peer's FRESH (≤ 2 s) heartbeat advertises ITSELF as master, P defers and does NOT take the role back (`_peer_claims_master_now`).
- **Fast path** — if there is no recorded master, or P is itself the recorded master, there is nothing to take over from; P proceeds (after the cold-boot UUID check + the N≥2 `COLD_BOOT_PATIENCE_S` wait) without the witness inspection below. The witness inspection (steps 1–5) only runs when P is taking the role FROM a different node.

When taking over from another node M, P:

1. **Identify the dying master M.** Read the last `mgmt_master` + its loopback from local rqlite (`level='none'`); M's node_id is the loopback's last octet (`_last_known_master_node_id()`). At N≤2 the witness must be reachable (a 5 s wait); at N≥3 P may proceed even without it (rqlite has natural quorum and the isolated old master self-demotes). Then inspect slot[M] from the most recent witness reply.
2. **Inspect M's slot in the most recent witness reply:**
   - No slot for M                            → worst-case assumed (the slot MIGHT have held `lms=1`). **REFUSE takeover.** Operator decommissions M from the rqlite `nodes` table or re-keys the witness — see INV-7.
   - Stale `(age ≥ 10 s)` AND `tag.lms == 0` → M died without going solo. Continue to step 3.
   - Stale AND `tag.lms == 1`                 → M went solo and never cleared the LMS bit. **REFUSE takeover.** LMS does not time out — see INV-7. Operator must clear M's LMS via the explicit override command (see `docs/operator-overrides.md`) before any peer can claim.
   - Fresh AND `tag.lms == 0`                 → M is alive, the mesh is just flapping locally. P stays follower.
   - Fresh AND `tag.lms == 1`                 → M is the legitimate last-man-standing. P stays follower.
3. **Local DRBD freshness check** — read the `cluster` resource's current-UUID (via debugfs `data_gen_id`, `drbdadm dump-md` fallback for a detached resource — `cluster_arbiter._read_local_drbd_uuid()`; note `drbdadm current-uuid` does not exist in DRBD 9.x). Decrypt M's slot, read `marker` (M's last-known DRBD UUID).
   - **EXACT MATCH** between P's local UUID and `slot[M].marker` → P's local data is identical to M's last witness-published state → safe to take over.
   - Mismatch → P missed writes M did before dying. **Refuse to promote.** Log clearly, surface to operator. Manual `drbdadm invalidate` on P (or wait for M to come back) is the only way out.
4. **Write own slot `tag.lms = 1`** with current local `(ts_writer, drbd_uuid)`.
5. **Read it back from the NEXT witness reply.** Wait ~1.5 s per attempt (one netd send+receive round-trip), then decrypt own slot and verify `tag.lms == 1` AND `marker == local drbd_uuid`. If not (write packet lost) → retry. Refuse to promote after 3 attempts (≈ 4.5 s).
6. **Promote, in order:**
   - `drbdadm primary cluster`
   - `mount $(drbdadm sh-dev cluster) /var/lib/bedrock/cluster`
   - `ip addr add 100.X.Y.254/32 dev lo`
   - `systemctl start bedrock-rqlited-arbiter`
   - start filer + s3.
7. **Rqlite quorum returns** as soon as the arbiter rqlited joins the surviving per-node rqlited (P's own). Only then — outside this protocol — can `set_mgmt_master(self)` be written through rqlite. The witness is not consulted again until the next state change.

Same protocol, abbreviated path, applies when the current master M itself loses peer-but-keeps-witness ("Scenario B" below): M is already at steps 6/7 done; it only needs steps 4+5 (flip own tag to lms, readback-verify).

## Arbiter self-demote protocol
A node currently arbiter-host self-demotes when its local election (mesh peer-liveness + witness reachability) concludes NoQuorum for `SELF_DEMOTE_MISSES = 9` consecutive election ticks (≈ 9 s — one tick before a survivor's `MASTER_LOSS_MISSES = 10`, so `.254` is released before any peer can take over), OR step 3 of takeover refuses with UUID mismatch on a fresh boot. The counter (`netd.noquorum_master_ticks`) also rides out the ~5 s daemon-startup window where neighbours=0 looks like NoQuorum, so a fresh restart never self-marks:
1. Stop filer + s3 → `ip addr del 100.X.Y.254/32` (released first, at every N, so a follower with a leftover `.254` never answers on the VIP) → stop `bedrock-rqlited-arbiter` → unmount → `drbdadm secondary cluster`. (At N=1, only filer + s3 + `.254` come down; there's no DRBD/arbiter to stop.)
2. Write own slot `tag.lms = 0`. This write requires the witness to be reachable. If the write fails (witness unreachable), the LMS bit stays set on the witness and the cluster is now in a stuck-LMS state — see INV-7. Retry on every subsequent election tick for as long as we are demoted-but-still-running. If we shut down or die before the write lands, operator intervention is required (see `docs/operator-overrides.md` "Clear stuck LMS"). Do not assume the slot will time out — it will not.

Order matters: filer + s3 (which use the mount) come down first, then `.254` is released before this node could ever be seen as still hosting. INV-1 forbids `.254` on two nodes simultaneously even for a tick — and the demoting node releases it a full second before any survivor's `MASTER_LOSS_MISSES` promote fires.

## Cold-boot protocol
Node N starts up with `/etc/bedrock/cluster.key` + `/etc/bedrock/state.json` present:
1. Send a probe + heartbeat. Wait up to 5 s for any reply.
2. If a reply landed: decrypt slot[self] (our OWN slot from a previous life) and compare its `marker` to the local `cluster` resource's current-UUID (debugfs `data_gen_id`). UUIDs are opaque — no ordering; the decision is equality + local 7-day history:
   - No own slot, or marker == local UUID → allowed (the legitimate first-promote / up-to-date case). Election still decides whether to take over.
   - Marker ≠ local UUID AND local UUID classifies SUPERSEDED in our own 7-day history (`state.classify_arbiter_uuid`) → the cluster advanced past us. Refuse to promote, stay follower until reconciled.
   - Marker ≠ local UUID but local UUID is not superseded (current/unseen) → allowed; re-publish via next heartbeat. Implemented in `cluster_arbiter._cold_boot_uuid_ok`.
3. No reply in 5 s AND peer reachable on mesh → defer to peer + normal election (witness optional at boot).
4. No reply AND no peer → refuse to promote. Wait.

At N≥2, even an eligible cold-booter holds off its FIRST promote for `COLD_BOOT_PATIENCE_S = 30 s` from process start (`cluster_arbiter._COLD_BOOT_AT`), so a slower peer that comes up can win the convergence cleanly instead of racing. N=1 promotes immediately.

## Scenarios (everything reduces to one of these)

### A — master loses peer AND witness ("isolated alone")
- M's writes stop landing on witness. M's slot ages.
- M's local election: 100 self of (100+100+1 = 201), majority 101 → NoQuorum → after the 9-tick (≈ 9 s) self-demote streak M demotes. M is now off; nothing running.
- Survivor P sees mesh peer M gone, slot[M] stale (≥ 10 s), `tag.lms == 0`. P promotes at `MASTER_LOSS_MISSES = 10` (~10 s) — one tick after M has released `.254` — and runs the takeover protocol. UUID match (P was a healthy Secondary up to M's last write) → P promotes.

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
- Local `cluster` current-UUID (debugfs `data_gen_id`) ≠ `slot[self].marker` AND the local UUID is SUPERSEDED in our own 7-day history → refuse to promote. Either sync from peer (DRBD does this automatically once the peer is reachable) or operator-reconcile.

## Invariants
- **INV-1** — at most one node holds `.254` at a time. Enforced by: takeover refuses on fresh-other-slot (step 2), refuses on UUID mismatch (step 3), requires own-write readback (step 5) before claiming `.254` (step 6); and the isolated master self-demotes (releases `.254`) at `SELF_DEMOTE_MISSES = 9`, one tick before a survivor promotes at `MASTER_LOSS_MISSES = 10`.
- **INV-2** — each node writes only its own slot. The witness files the blob under the AEAD-verified envelope `n` (so a node can only overwrite its own key); the reader additionally drops any slot whose decrypted inner `n` ≠ the map key it arrived under.
- **INV-3** — tag transitions are local-only. `lms` is set on this-node-decided-to-go-solo, cleared on this-node-self-demoted. Never copied from another slot.
- **INV-4** — `ts_writer` from the writer is the freshness reference; reader uses its own clock for comparison. No witness clock involved.
- **INV-5** — the takeover UUID comparison (step 3, against M's slot) is **exact equality**, never an ordering — any mismatch refuses. UUIDs carry no order. "Newer/older" exists only in the cold-boot self-slot check, and only as a local 7-day-history classification (`state.classify_arbiter_uuid`: superseded ⇒ refuse; current/unseen ⇒ allow), never as a numeric compare.
- **INV-6** — the takeover decision uses ONLY the witness + local commands; no rqlite CONSENSUS call is on the critical path (rqlite is the service being recovered). The lone exception is a `level='none'` LOCAL replica read to identify the last master + cluster size — no quorum required, never a write.
- **INV-7** — **`tag.lms = 1` never times out, and a missing slot is treated as worst-case.** The 10 s staleness rule and the 72 h witness-retention rule do not clear LMS; they only change interpretation. The only paths to a cluster state where a previously-set LMS no longer blocks takeover:
  - (a) The slot owner is alive and successfully writes `tag.lms = 0` to the witness (requires both online simultaneously).
  - (b) The operator decommissions the slot owner (`bedrock node leave …` / equivalent removes it from the rqlite `nodes` table — the cluster-wide source of truth). Surviving nodes then *ignore* any witness slot for that removed node-id because it is no longer a known cluster member. The witness still stores the slot until 72 h retention drops it, but nobody reads it.
  - (c) The operator re-keys the cluster's witness identity (new `cluster_uuid` and/or new `cluster.key`). Old encrypted slot payloads can no longer be decrypted by any cluster member; the slot map re-populates from current members' heartbeats within a few seconds.

  **Witness-loses-state case is NOT a clear.** If the witness reboots and comes back empty (or any individual slot is missing), readers must assume the worst case for that slot: the missing slot *might* have held `tag.lms = 1`. Until the slot is repopulated by a fresh heartbeat from the current cluster member it belongs to (and observed by the reader), the reader treats that slot as `lms = 1`-possible and refuses takeover. A slot belonging to a node that is in the rqlite `nodes` table but not currently writing remains "worst-case unknown" until operator intervention via (b) or (c).

  Reason: a node that died with LMS set may have written data after the last DRBD sync to its peer; allowing automatic peer takeover — including via "witness forgot" — would risk silent data loss. The safety-over-availability trade is intentional. Only the paranoid survive.

## Why no witness clock (and why the writer's clock works)
- A witness that generates timestamps must have an accurate clock. ESP32 + battery-backed RTC + drift handling on a small appliance is significant complexity for one job.
- Cluster nodes already NTP-sync (chronyd in every install). Drift between cluster members on a healthy LAN is sub-second.
- Reader uses its own clock. `now_local - slot.ts_writer ≥ 10 s` absorbs the worst plausible per-node skew with margin.
- Replay protection comes from AEAD nonces, not from timestamps. A stale-but-valid slot is fine: it just looks correctly old.

## Backends
| backend | status | mechanism | persistence | notes |
|---|---|---|---|---|
| `bedrock-echo` (UDP/12321) | implemented | ESP32 firmware or `testbed/bedrock_echo_stub.py`. AEAD packets, slot map in memory + flushed to flash on every accepted write. | Required in production. Testbed stub may be RAM-only. | LAN flap → all slots stale → cluster halts safely. |
| multiple Echo endpoints | implemented | Each discovered Echo holds its own slot cache (`EchoEndpoint.slots`), validated independently; `count_valid_confirmed` gives +1 per valid+confirmed witness whose `echo_id` matches a configured `witness_id` (capped at `n_configured`). | Each Echo's own. | Per-witness tally, bound to the configured set; a *majority* read across witnesses is not yet built. |
| Echo-by-IP (directed probe) | implemented | `netd` unicast-probes every configured Echo's IPv4 `host:port` alongside the L2 broadcast, so a routed/off-subnet Echo still votes. | n/a | IPv4 unicast only (no hostname/DNS on the election tick, no multicast/broadcast, no IPv6 on the AF_INET socket). |
| fileshare (path-based) | implemented | `witness_file.py`: each node writes its own `slot-<NN>.bin` via `tmp + os.replace` and reads every `slot-*.bin` — the dir IS the central store (no UDP framing). Same AEAD slot blob + the EXACT Echo predicates. Driven by a dedicated netd thread (`_witness_file_worker`) every 3 s, OFF the 1 Hz election tick so share latency can't stall mesh+election; the verdict is cached in `ws.file_witnesses` and folded by `count_valid_confirmed`. | The fileshare itself. | Operator mounts an NFS/SMB/object share at a dir on EVERY node (`backend='fileshare'`, `addr=<dir>`); a node that can't write leaves its slot absent → the witness stays at 0 votes (never a miscount). A node's own valid+confirmed verdict depends only on its OWN clock. `os.replace` atomicity assumed (POSIX/NFS; some CIFS servers need a compliant mount). |
| native managed SMB / S3 | not yet built | Bedrock mounts/authenticates the share itself (creds in a per-witness `0600` env file like backup targets). Today: mount it yourself + use the path-based fileshare backend. | The store itself. | Will reuse the backup storage-endpoint shape. |
| multi-witness quorum read | not yet built | A read returns a slot only if a majority of configured witnesses agree. | Each backend's own. | Wire protocol already permits multiple endpoints. |

## Timing knobs (one place for tuning)
| event | value | rationale |
|---|---|---|
| slot refresh interval | 1 s | matches election tick |
| stale threshold | 10 s | 10 missed 1 s writes; **pairs with the 10-missed-beat leader-loss detector** (`netd.MASTER_LOSS_MISSES = 10`) so a reader treats the master's slot as gone exactly when a survivor concludes leader-loss. Implemented as `witness.SLOT_STALE_MS = 10_000`. |
| takeover-readback retries | 3 × ~1.5 s ≈ 4.5 s | covers transient UDP loss |
| cold-boot witness wait | 5 s | bounded; healthy witness replies in < 100 ms |
| NoQuorum self-demote streak | `SELF_DEMOTE_MISSES = 9` ticks (≈ 9 s) | one tick before `MASTER_LOSS_MISSES = 10`, so the isolated master releases `.254` before any survivor promotes (INV-1) |
| witness-reachable vote window | `WITNESS_FRESHNESS_S = 12 s` | `is_alive` — a reply within 12 s makes the witness count toward the vote; distinct from the 10 s slot-stale rule |
| AEAD nonce size | 12 bytes (96 bits) | ChaCha20-Poly1305 default |
| Poly1305 auth tag | 16 bytes | standard |

## Implementation
- `installer/lib/witness.py` — passive K/V slots (`slots: dict[int, Slot]`); `set_own_slot(ws, *, marker, tag=0, kind=…)` writer, `read_slot(ws, node_id)` / `own_slot(ws)` readers against the latest reply; `is_valid` / `is_confirmed` / `count_valid_confirmed` vote predicates; AEAD-msgpack wire format on UDP 12321. Drops slots whose node_id isn't in `member_ids` (INV-7 path b).
- `installer/lib/election.py` — `compute(...)` decides Leader/Follower/NoQuorum from mesh peer-liveness + acks + witness votes; weights `VOTES_PER_NODE = 100`, `VOTE_PER_WITNESS = 1`; sticky NoQuorum via `/run/bedrock-no-quorum`.
- `installer/lib/cluster_arbiter.py` — `promote_to_arbiter_host()` runs `_run_takeover_protocol()` (steps 1–5) then the step-6 DRBD-primary + mount + `.254` + arbiter rqlite + filer; `mgmt_master` is written only AFTER hosting is confirmed (never on the critical path). `demote_arbiter_host()` reverses it and writes `tag.lms = 0` at the end. `ensure_lms_if_last_standing()` sets LMS on the Scenario-B last-man path.
- `installer/lib/netd.py` — the `_election_tick` that ships the witness heartbeat each second, runs `compute()`, drives promote/demote, and counts `noquorum_master_ticks` to `SELF_DEMOTE_MISSES`.
- `testbed/bedrock_echo_stub.py` — Python BedRock Echo witness for the testbed: a pure encrypted K/V slot server, no claim/bless logic.

The transport is AEAD (ChaCha20-Poly1305) over the msgpack body with the nonce in the envelope; the witness is a passive per-node K/V slot store; each node writes only its own slot; takeover gates on peer-slot inspection + exact local UUID equality + own-slot readback; freshness uses the writer's clock compared against the reader's own clock; and the takeover path consults nothing in rqlite — it is pure witness + local commands. (The witness uses AEAD; mesh probes/adverts on UDP 7732/7733 use HMAC-SHA256 over the cluster_key — different transports for different jobs.)

## Out of scope (today)
- Majority-quorum reads across multiple witnesses (the wire + per-witness tally already support multiple endpoints; the majority-agreement read is not yet built).
- Automated DRBD-divergence recovery. Step 3 surfaces; operator runs `drbdadm invalidate` manually.
- Per-node sub-keys. The cluster_key is the auth boundary; within-cluster impersonation is not in the threat model.
- Slot history / audit log on the witness. The witness keeps the latest write only; cluster nodes' bedrock-d logs are the audit trail.

## Why the witness is "critical only at failover and cold boot"
- Steady-state: nodes refresh slots, but no decision depends on the slot contents — the running master keeps running, followers keep following. Mesh probes do all the work.
- Failover: the takeover protocol is the SOLE path to becoming arbiter-host. Witness reachability + slot readback is mandatory.
- Cold boot: the slot's `marker` is the only third-party record of the current dataset generation. Without it, a stale boot would silently overwrite peer progress.

Between those two moments, the witness can be offline and the cluster keeps running. This is what makes a low-cost ESP32 a viable third observer for production HA: it has to be right at two crisp moments, not continuously.
