# Cluster quorum + witness — spec

## Purpose
A witness is a third-party observer that gives a 2-node cluster a safe failover decision and lets any node verify it has the current DRBD dataset before becoming arbiter-host. Witnesses are **passive K/V stores**. They do not "bless", "accept", or arbitrate. All decisions are local in the requesting node.

## Witness shape
- **One slot per cluster node.** Identified by `node_id` ∈ 1–250 (one byte). `node_id` = last octet of the node's `100.X.Y.N/32` loopback.
- 254 = arbiter VIP, 255 = broadcast, 251–253 reserved. 0 unused.
- **Each node OWNS its slot.** Writes are HMAC-authenticated with `/etc/bedrock/cluster.key`. The witness rejects a write whose body `n` field disagrees with the sender's HMAC-verified node identity.
- The witness has **no concept of master, election, claim, or bless.** It stores last-write per slot and returns all slots on every reply.

## Slot payload
```
{
  drbd_uuid: hex string   # tier-critical `drbdadm current-uuid` this node last saw
  tag:       "normal"|"lms"
}
```

The witness ALSO tracks `ts_ms` per slot (the witness's local clock when the write was accepted) — **never trust the writer's clock** for freshness math. The witness returns `age_ms = witness_now_ms - slot.ts_ms` to readers, so all freshness logic uses one clock.

### `drbd_uuid`
DRBD's `current-uuid` is a 64-bit monotonic generation marker that bumps on every write-pattern change. The arbiter-host node writes its current value into the slot every tick. Other nodes compare it against their own local `drbdadm current-uuid tier-critical` to decide whether they hold the current dataset.

### `tag`
- `normal` — default. Set by: any healthy node (master or follower with peer up; node booting; node that has just self-demoted).
- `lms` — "last man standing". Set by: a node that has decided to operate alone because (a) it lost its peer AND (b) it had witness contact at the moment of the decision.

Tag transitions are **local-only**:
- `normal → lms` on peer-loss-with-witness, OR as step 4 of the takeover protocol below.
- `lms → normal` on self-demote (recovery from solo back to multi-node operation).

## Wire protocol
Msgpack + HMAC, UDP/9501. Identical envelope to today's bedrock-echo so existing firmware works:

Probe / heartbeat (node → witness):
```
b"BREC" | msgpack({
  v: 1, t: "hb",
  cu: cluster_uuid,
  n:  node_id,                       # 1 byte
  drbd_uuid: hex32, tag: "normal"|"lms",
  ts: epoch_ms,                      # informational, NOT used for freshness
  nonce: 8 random bytes,
  hmac:  HMAC_SHA256(cluster_key, canonical_body_without_hmac)[:16]
})
```

Reply (witness → node):
```
b"BREC" | msgpack({
  v: 1, t: "hb_ack",
  echo_id: "...",  cu: cluster_uuid,
  ts: witness_now_ms,                # witness clock; reference for age_ms
  slots: {
    node_id: {drbd_uuid, tag, age_ms},
    ...
  },
  nonce: <echo of caller's>, hmac: ...
})
```

A reply that doesn't HMAC-verify is silently dropped. Multiple clusters can share one witness (HMAC keys differ).

## Slot lifecycle
- Each node writes its slot every **1 s** with the current `(drbd_uuid, tag)`.
- The witness REPLACES on every accepted write. No history kept.
- A slot is **stale** when `age_ms ≥ 15_000`. 15 s = 14 missed ticks + 1 grace, tuned to absorb LAN flaps without false-positive takeover.

## Takeover protocol (load-bearing — every step matters)
P is a node whose local election says it should become arbiter-host (peer believed gone, P is the lowest-octet survivor). Before any DRBD or service action, P MUST:

1. **Confirm peer is dead via witness** — examine `slot[M]` for the known/last master M:
   - `age_ms < 15_000 AND tag == "normal"` → cluster still healthy elsewhere. P stays follower. (M is alive on witness link; mesh hiccup.)
   - `age_ms < 15_000 AND tag == "lms"`    → M is the legitimate last-man-standing. P stays follower.
   - `age_ms ≥ 15_000`                    → M is gone. Continue to step 2.
2. **DRBD freshness check** — local `drbdadm current-uuid tier-critical` MUST be ≥ `slot[M].drbd_uuid`. If lower → P's data is stale; **refuse to promote**, surface to operator. (This catches "M ran solo, advanced data, then died; P never received those writes" — without this check, P would silently overwrite M's solo progress on a later resync.)
3. **Write own tag `normal → lms`** — send a heartbeat carrying the new tag.
4. **Read back the next reply.** Verify `slot[self].tag == "lms"` in the witness's response. The reply uses witness clock, so this is proof the write actually landed. If still `"normal"`, retry from step 3. Refuse to promote after 3 failed retries.
5. **NOW** perform the promotion: `drbdadm primary tier-critical`, `mount /var/lib/bedrock/cluster`, `ip addr add 100.X.Y.254/32 dev lo`, `systemctl start bedrock-rqlited-arbiter`, start filer + s3, then `set_mgmt_master(self)` in rqlite.

The same protocol applies for a current master that loses its peer (Scenario B below) — it's just step 3+4 with the rest already done.

## Self-demote protocol
A node currently arbiter-host self-demotes the moment its local election concludes NoQuorum (no peer AND no witness for ≥ 15 s, OR step 2 of takeover refused):
1. Stop filer + s3, stop bedrock-rqlited-arbiter, remove `.254` VIP, unmount, `drbdadm secondary tier-critical`.
2. Best-effort write `tag: lms → normal` to witness. (May fail if witness unreachable; that's fine — slot will go stale and a survivor will take over normally.)

## Cold-boot protocol
Node N starts with `cluster.key` + `state.json` present:
1. Send a probe. Wait up to **5 s** for any reply.
2. If reply received: examine `slot[self].drbd_uuid` vs local `drbdadm current-uuid tier-critical`:
   - Equal or local is newer → up-to-date, eligible for arbiter-host once election permits.
   - Local is older → cluster has advanced without us; refuse to promote, stay follower until peer or operator reconciles.
3. If no reply in 5 s AND peer reachable → defer to peer + normal election.
4. If no reply AND no peer → refuse to promote. Wait.

## Scenarios (everything reduces to one of these)

### A. Master loses peer AND witness ("isolated alone")
- M's writes stop landing on witness → slot ages.
- M's local election: `my_votes = 10`, `majority = 11` (2 nodes + witness). NoQuorum → after ≈ 5 s streak, M self-demotes (no service running on M).
- After 15 s of M not writing, `slot[M].age_ms ≥ 15_000` from any other node's view.
- Survivor P sees: peer M gone via mesh; witness reachable; `slot[M]` stale + `tag == "normal"` (M didn't have time/path to mark `lms`). P runs the takeover protocol above. Promotes.

### B. Master loses peer only, keeps witness ("last man standing")
- M sees peer down via mesh. M flips own slot `tag: normal → lms`, reads back confirmed.
- M keeps hosting and keeps refreshing slot every tick (`tag == lms`, drbd_uuid bumps on writes).
- P sees peer M gone via mesh, but `slot[M]` is fresh and `tag == "lms"` → P stays follower (takeover protocol step 1).
- No failover. M is the legitimate solo owner.

### C. Both nodes lose mesh to each other, both keep witness (split with witness)
- Both attempt Scenario B's flip-to-`lms`.
- Whichever node's `lms` write lands first is seen as fresh-and-`lms` by the other in the very next reply.
- The loser's takeover protocol step 1 sees the winner's `lms` slot → stays follower. If the loser had pre-emptively written `lms` itself, it doesn't matter — the OTHER node's slot is what gates the loser's behaviour.
- Race resolution time is one round-trip (≤ 2 s on a healthy LAN).

### D. Witness gone, peer alive (no failover-relevant decision needed)
- Both nodes lose witness contact. Their own slot writes fail silently.
- Election math: 10+10 = 20, majority = 11. Each side has the other reachable. Both stay in their current role. Cluster keeps running. (Witness availability is NOT a continuous quorum requirement.)
- New takeovers blocked until witness returns. Existing master keeps hosting.

### E. Both nodes lose everything (full split, no witness)
- N=2 without witness: each side has 10 of 20, below majority. Both NoQuorum → both self-demote. Cluster halts safely. Operator must intervene.

### F. Cold boot of a single node into an existing-but-unreachable cluster
- N boots. Witness reachable. `slot[self].drbd_uuid` exists from before reboot. N compares to local `drbdadm current-uuid`. If matches → safe to participate. If local lags → cluster advanced without us → refuse to promote.

## Invariants
- **INV-1** — at most one node holds `.254` at a time per cluster. Enforced by takeover steps 1+2+4 (refuse on fresh-other-slot; refuse on UUID-lag; require witness readback before promote) plus self-demote step 1 (stop services before any cluster-state write).
- **INV-2** — a node writes ONLY its own slot. Witness rejects mismatched `n`.
- **INV-3** — tag transitions are local-only. Never copied from another slot.
- **INV-4** — slot staleness (`age_ms` from witness clock) is the SOLE liveness signal. Never use slot.ts (writer's clock).
- **INV-5** — drbd_uuid comparison is local: P compares its OWN `drbdadm current-uuid` to slot's `drbd_uuid`. Never compare slot to slot.

## Backends (interchangeable behind the same slot semantics)
| backend | mechanism | persistence | failure mode |
|---|---|---|---|
| `bedrock-echo` (current) | UDP/9501, ESP32 / Python stub | persistent (SD on appliance); RAM in testbed stub | LAN flap → all slots stale → cluster halts safely |
| fileshare (SMB / NFS / S3) | one file `slot-NN.bin` per slot, atomic tmp+rename | the fileshare | network flap → same |
| multi-witness (post-v1.0) | quorum-of-N writes + reads | each backend's own | minority loss tolerated |

Production echo MUST persist slots across restart. Testbed stub may be RAM-only.

## Timing knobs (all in one place)
| event | value | note |
|---|---|---|
| slot refresh interval | 1 s | matches election tick |
| stale threshold | 15 s | absorbs 14 dropped ticks + 1 grace |
| takeover readback retries | 3 | covers transient UDP loss |
| cold-boot witness wait | 5 s | bounded; healthy witness replies in < 100 ms |
| NoQuorum self-demote streak | 5 ticks ≈ 5 s | shorter than stale threshold so demote happens before another node could take over |
| witness HMAC truncation | 16 bytes | from SHA-256; sufficient for UDP frames |

## What this spec replaces in the current code
Today's code uses a "claim → witness blesses one master → 15 s holddown" model. Same protection in different vocabulary. The semantic upgrade in this spec:

| concept | today | target (this spec) |
|---|---|---|
| witness role | active arbiter (`blessed_master`) | passive K/V slot store |
| who writes what | every node `send_claim` competes for `blessed_master` | every node writes its own slot |
| takeover gate | wait for bless to age out (15 s) | inspect peer slot + flip own tag + readback |
| extra rejection logic | echo stub has accept/reject logic + bless holddown | echo stub has none — just K/V |
| cold-boot DRBD check | not implemented | step 2 of cold-boot uses slot.drbd_uuid |

Files to change:
- `installer/lib/witness.py` — replace `blessed_*` fields with `slots: dict[int, Slot]`. Replace `send_claim` with `write_slot(tag, drbd_uuid)`. Provide a `read_slot(node_id) -> Slot|None` helper backed by the latest reply.
- `installer/lib/election.py` — drop `witness_blessed_master` / `witness_blessed_at_ms` / `bless_holddown_ms` parameters. Add a callback (or pass-in dict) of `slots`. Decision logic becomes the takeover-protocol table above.
- `installer/lib/cluster_arbiter.py` — `promote_to_arbiter_host` runs takeover protocol steps 1–5 BEFORE any DRBD or service action. `demote_arbiter_host` writes tag `normal` at the end.
- `testbed/bedrock_echo_stub.py` — strip all `claim` / `blessed_*` logic. Becomes pure K/V: receive heartbeat → store `(drbd_uuid, tag, ts_witness)` in `slots[node_id]` → reply with all slots and their `age_ms`. ~50 lines shorter.
- `bedrock-echo` (ESP32 firmware) — same shape; persistent storage on flash or SD.

## Out of scope (today)
- Multi-witness quorum reads + writes (D-17 / v1.1). Wire protocol already supports multiple Echo endpoints; the per-node `WitnessState.discovered` list of endpoints will become "send to all, require majority of replies to agree" once we have ≥ 2 witnesses configured.
- Automated DRBD-divergence recovery. Step 2 of takeover currently just refuses and logs; operator runs `drbdadm invalidate` manually.
- Per-slot signature (separate node sub-key) — the cluster.key is the auth boundary; node-impersonation by anyone with the key is out of the threat model.

## Why the witness is "critical only at failover and cold boot" (per D-18)
- During steady-state: each node refreshes its own slot but no decision depends on the slots. Election math counts the witness as +1 vote, but losing that vote at N=2 (peer alive) still leaves 20/20 = no quorum loss because no master change is being proposed.
- At failover: the takeover protocol is the SOLE path to becoming arbiter-host. Witness reachability is mandatory.
- At cold boot: the slot's `drbd_uuid` is the only third-party record of which side has the latest dataset. Without it, a stale boot could overwrite peer progress.

Outside those two moments, the witness can be offline and the cluster keeps running.
