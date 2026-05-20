# Cluster quorum + witness spec

Single source of truth for HA failover state machine. Telegram style — facts only.

## Vocabulary
- **node** — Bedrock member. Runs `bedrock-d`, per-node rqlite, mesh, mgmt.
- **peer** — another node visible on mesh.
- **arbiter-host** — node holding `.254/32`, mounting `tier-critical` DRBD as Primary, running `bedrock-rqlited-arbiter`, running filer+s3. Cluster-singleton role.
- **mgmt_master** — node whose name is in rqlite `cluster_info.mgmt_master`. Same node as arbiter-host (single role).
- **witness** — external `bedrock-echo` Replica. UDP/9501. Blesses ONE master per cluster UUID.
- **bless** — `(blessed_master, blessed_drbd_uuid, blessed_at_ms)` triple stored on witness. Returned on every reply so every node hears it.

## Vote math
- per node: **10** votes
- per witness: **1** vote each
- `total_votes = 10 * N_known_nodes + 1 * N_alive_witnesses`
- `majority   = total_votes / 2 + 1`
- "leader" iff `my_votes >= majority` AND `witness_blessed_master ∈ {self, ""}`

`N_known_nodes` = nodes in `cluster.json` that we've EVER seen in our peer table (filters not-yet-discovered joiners).

## Heartbeat cadence
| signal | period | freshness limit | constant |
|---|---|---|---|
| mesh probe per NIC | 1 Hz | n/a | — |
| witness heartbeat | 1 Hz | 10 s = `WITNESS_FRESHNESS_S` | `witness.py` |
| peer logged_up rise | 5 s hysteresis | — | `UP_HYSTERESIS_S` |
| NoQuorum holddown | 5 ticks ≈ 5 s | — | `NOQUORUM_HOLDDOWN_TICKS` |
| bless holddown | 15 s | — | `bless_holddown_ms` (election) and `CLAIM_HOLDDOWN_MS` (echo stub) |
| **lone-master watchdog** | — | **28 s** | new — see INV-4 |

## Invariants
- **INV-1** — at most one node holds `.254` at a time per cluster.
- **INV-2** — a node sending a fresh witness claim announces its UUID. If the witness rejects (or its next reply still reflects a peer as blessed_master), `election.compute` on the next tick maps that to `NoQuorum + self-demote`. The claim itself is fire-and-forget at promote time; the demote path is unified through INV-3. Doing a synchronous "wait for claim_ack and rollback" inside the promote causes thrash when the witness's CLAIM_HOLDDOWN_MS (15 s) is still active for the previous master.
- **INV-3** — if `witness_blessed_master ∉ {self, ""}` (fresh bless), I am NOT leader. Demote singletons within 1 tick.
- **INV-4** — if I am arbiter-host AND no fresh peer heartbeat AND no fresh witness heartbeat for `28 s`, demote unconditionally ("lone-master watchdog"). Belt for the case where INV-3 can't fire (witness gone).
- **INV-5** — claim UUID must equal stored `blessed_drbd_uuid` OR bless must have aged ≥ `CLAIM_HOLDDOWN_MS`. Otherwise REJECTED.

## States (per-node)
| state | how we got here | exit |
|---|---|---|
| **BOOTED** | bedrock-d starting | cluster.key + state.json present → eligible |
| **FOLLOWER** | `current_mgmt_master != self`, master alive | master gone → CANDIDATE; bless flips to me → LEADER |
| **CANDIDATE** | master gone, I'm lowest reachable octet, `my_votes ≥ majority` | claim ACCEPTED → LEADER-PROMOTING; REJECTED → FOLLOWER |
| **LEADER-PROMOTING** | claim ACCEPTED but arbiter not yet started | promote_to_arbiter_host done → LEADER |
| **LEADER** | hosting arbiter, holding `.254`, mgmt_master == self | quorum lost → NOQUORUM; bless flips away → DEMOTING; watchdog 28 s → DEMOTING |
| **NOQUORUM** | `my_votes < majority` | streak ≥ 5 → FENCED+DEMOTING; quorum back → previous state |
| **DEMOTING** | mid-`demote_arbiter_host()` | done → FOLLOWER or NOQUORUM |
| **FENCED** | `/run/bedrock-cluster.fence` present | `fence_responder` clears marker once quorum returns |

## Transitions (event → action)

```
on each 1 Hz election tick:
    if fence_marker_present:                 → FENCED, no-op
    refresh peer_liveness from mesh table
    refresh witness reply via drain_replies (updates blessed_*)
    compute election

    if outcome == NO_QUORUM:
        streak += 1
        if streak >= 5:
            write fence marker
            if hosting: demote_arbiter_host()  # INV-1
        return

    if witness_blessed_master not in (self, ""):
        if bless age < bless_holddown_ms:
            if hosting: demote_arbiter_host()  # INV-3
            return FOLLOWER

    if outcome == LEADER and not hosting:
        try cluster_arbiter.promote_to_arbiter_host()
        if promote OK: witness.send_claim(uuid_hex)
            if claim REJECTED:                # INV-2
                demote_arbiter_host()
                return FOLLOWER

    if hosting:                               # INV-4 watchdog
        if (now - last_peer_hb) > 28 and (now - last_witness_hb) > 28:
            demote_arbiter_host()

    if outcome == LEADER and not currently mgmt_master:
        set_mgmt_master(self) via rqlite
```

## Failover scenarios — two and only two outcomes

The witness arbitrates by **bless aging**, not by vote count alone. The current master keeps refreshing its claim every tick AS LONG AS it can reach the witness. The bless is fresh iff the claim is being refreshed. There are two scenarios; everything reduces to one of them.

### Scenario A — master loses peer AND witness ("isolated alone")
- Master M can no longer reach peer P **and** can no longer reach witness W.
- M's vote: only 10 (self). Total = 21 (2 nodes + witness). Majority = 11. **NoQuorum.**
- After `NOQUORUM_HOLDDOWN_TICKS` ≈ 5 s (tighten to 15 s if needed for slow WAN), M self-demotes:
  - release `.254`, `drbdadm secondary tier-critical`, stop `bedrock-rqlited-arbiter`, stop filer + s3.
- M's witness claim stops being refreshed (M can't reach W). Bless ages out after `CLAIM_HOLDDOWN_MS` = 15 s.
- P (survivor) sees: peer gone via mesh; witness still reachable. P's vote: 10 + 1 = 11 of 21. Leader.
- P's election: `witness_blessed_master == M`, bless age increasing. Once age > 15 s, election lets P promote (line 199–217 of `election.py`).
- P calls `cluster_arbiter.promote_to_arbiter_host()`, sends claim, witness accepts (M's bless is stale).
- Cluster runs on P, M is fully off.

This is exactly what `5c` exercises (1-of-4 isolated; same math, larger N).

### Scenario B — master loses peer but keeps witness ("last man standing")
- Master M can no longer reach peer P, but **still reaches witness W**.
- M's vote: 10 + 1 = 11 of 21. **Leader** by exact majority.
- M continues hosting `.254` + arbiter + filer.
- M keeps sending claims to W every tick. Bless never ages out. W reflects `blessed_master = M` on every reply.
- P sees: peer gone; witness reachable; W's reply has `blessed_master = M`, age fresh (< 15 s).
- P's election (line 199–217): `witness_blessed_master == M`, age < `bless_holddown_ms` → return FOLLOWER with reason "witness blesses M". P **backs off**, does not promote.
- No failover. M stays master. P stays follower until mesh reconverges.

**Test `8b` exercises Scenario B**, not a failover. The current test expectation (P must take over) is incorrect under this design — M is the legitimate last-man-standing because it still has witness contact.

### Tie cases collapse to one of the above
- **Witness gone but peer alive** — both sides see same peer, no witness vote, election math `my_votes = 10` of `total_votes = 20`. Below majority (= 11). Both NoQuorum. Both demote. Cluster dies safely. (Scenario A's math with N=2; covered by `8d`.)
- **Mesh split, both can reach witness** — both sides try Scenario-B-style "last man standing". The first one to claim wins because the witness rejects later claims while the bless is fresh. The loser sees `blessed_master = other` and backs off via INV-3. Exact 11-vs-11 vote tie is broken by witness arbitration, not by vote count.
- **DRBD divergence** — if both sides force-primary'd before the witness arbitrated, their UUIDs diverge. The loser's later re-claim is REJECTED by INV-5 even after holddown expiry. Operator intervention required.

## Why the bless ages out the way it does
The blessed master refreshes the bless every tick (via `netd._election_tick` calling `_witness.send_claim` when `current_master == self`). The echo stub updates `blessed_at_ms` on every accepted claim. So:
- **Master alive on witness link** → bless is always < 1–2 s old → fresh.
- **Master dead OR can't reach witness** → claims stop → bless ages → at 15 s, the next claim from a competitor wins.

No separate "last-man-standing" flag is needed; the **refresh-or-die** pattern of the bless IS the flag.

## What changed for the unification push
1. **`netd.set_mgmt_master` writability probe** — switched from non-existent `store.raft.leader.addr` to `SELECT 1` via `/db/query?level=strong`. The strong query forces a Raft round-trip; succeeds only with a reachable leader.
2. **Witness claim at promote time** — `cluster_arbiter.promote_to_arbiter_host` calls `witness.send_claim` directly via `SHARED_STATE.netd_ws`. Was previously gated on `current_master == self in cluster.json`, which lags `set_mgmt_master` by one subscriber roundtrip.
3. **Election outcome publish** — `state.last_election_outcome` exposed for fence_responder + cluster_arbiter (replaces stale `cluster.json["role"]` reads).
4. **Lone-master watchdog (NEW, INV-4)** — see `installer/lib/netd.py:_election_tick`.

## Code map (where each invariant lives)
| invariant | file | function |
|---|---|---|
| INV-1, INV-3 | `installer/lib/election.py` | `compute()` |
| INV-1 demote action | `installer/lib/netd.py` | NoQuorum branch in `_election_tick` |
| INV-2 promote→claim | `installer/lib/cluster_arbiter.py` | `promote_to_arbiter_host()` |
| INV-3 demote action | `installer/lib/netd.py` | bless-mismatch branch in `_election_tick` |
| INV-4 watchdog | (not implemented) | NoQuorum self-demote at 5 s already covers; only worth adding if a witness-flap edge case proves it necessary |
| INV-5 | `testbed/bedrock_echo_stub.py` + real `bedrock-echo` | claim handler |

## Open
- **Multi-witness quorum** — current code assumes 1 witness. With N witnesses, accept claim requires majority of ACCEPTs. Not implemented.
- **DRBD-UUID drift on force-primary** — both nodes force-primary'ing in a partition can produce divergent UUIDs that the same-UUID re-claim path doesn't recognise. Resolution = operator merges via `drbdadm invalidate` on the loser side.
