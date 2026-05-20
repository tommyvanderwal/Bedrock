# Cluster quorum + witness — spec

## Witness
- **Passive K/V store, one slot per node.** Not an arbiter, does not "bless".
- Each node owns its slot (node_id, 1 byte, 1–250). Other nodes only read.
- Backends are interchangeable: `bedrock-echo` (UDP/9501), fileshare (S3/SMB/NFS), etc.

## Slot payload (per node)

| field | size | meaning |
|---|---|---|
| node_id | 1 byte | 1–250, the node's cluster index |
| drbd_uuid | hex | tier-critical `current-uuid` this node last saw |
| tag | enum | `normal` \| `lms` (last man standing) |
| ts_ms | int | when this node wrote the slot |

## Liveness
- Each node refreshes its own slot every **1 s**.
- A slot is **stale** when `now − ts_ms ≥ 15 s`.

## Scenarios

### A — master loses peer AND witness ("isolated alone")
1. Master M's slot stops being refreshed.
2. M's local election: `my_votes = 10`, `majority = 11`. NoQuorum.
3. After NoQuorum streak ≈ 5 s, M self-demotes: release `.254`, `drbdadm secondary tier-critical`, stop arbiter rqlited, stop filer + s3. Set own tag back to `normal`.
4. Survivor P sees: peer gone, M's slot still readable but stale (≥ 15 s) with tag `normal`.
5. P flips its own tag `normal → lms`, reads its slot back, sees `lms` confirmed.
6. P promotes: `drbdadm primary`, mount, `.254`, start arbiter rqlited, start filer + s3.

### B — master loses peer only, keeps witness ("last man standing")
1. M sees peer down via mesh.
2. M flips own tag `normal → lms`, reads back confirmed.
3. M keeps hosting and keeps refreshing slot every tick.
4. P sees: peer M gone via mesh, M's slot fresh, tag = `lms` → P stays follower.

### Cold boot of a single node
1. Node N starts. Reads its own slot.
2. Compares slot `drbd_uuid` against local `drbdadm current-uuid tier-critical`.
3. Match → safe to join. Mismatch → block, surface to operator (DRBD divergence).

## Invariants

- **INV-1** — exactly one node holds `.254`. Enforced by: takeover requires own-slot tag flip to `lms` AND read-back confirms `lms` landed.
- **INV-2** — each node only writes its own slot. No node "blesses" another.
- **INV-3** — on self-demote, the demoting node flips own tag back to `normal` so it can rejoin cleanly.
- **INV-4** — slot staleness is the **single** quorum-loss signal. No coupling to other fields.

## Timing budget (1 ms ticks, all knobs at the top)

| event | budget |
|---|---|
| slot refresh interval | 1 s |
| stale threshold | 15 s |
| NoQuorum → self-demote | ≈ 5 s streak |
| max wall-clock old-master-still-hosting after isolation | ≤ 20 s |
| expected end-to-end failover | ≤ 30 s |

## Witness backends

| backend | mechanism | failure mode |
|---|---|---|
| `bedrock-echo` (current testbed) | UDP/9501, HMAC over cluster_key | LAN flap → all slots go stale equally; both sides demote |
| fileshare (S3 / SMB / NFS) | per-slot file `slot-<node_id>.json`, atomic write + rename | same |
| multi-witness (future) | quorum-of-N reads, write to majority | minority loss tolerated |

The witness is critical **only** at: failover handoff, and cold boot. Steady-state, it's idle K/V traffic.

## Code map (target — current code DOES NOT yet match)

| concept | file | what changes |
|---|---|---|
| slot codec | `installer/lib/witness.py` | drop `claim` / `blessed_master`; write `(node_id, drbd_uuid, tag, ts_ms)` to own slot, read others' slots |
| election | `installer/lib/election.py` | replace `witness_blessed_master` holddown with: read peer slot, check stale + tag |
| promote | `installer/lib/cluster_arbiter.py` | before `drbdadm primary`: flip own tag `normal → lms`, write, readback. Refuse if readback fails. |
| demote | `installer/lib/cluster_arbiter.py` | after `drbdadm secondary`: flip own tag back to `normal` |
| echo stub | `testbed/bedrock_echo_stub.py` | become pure K/V slot server; drop the bless model |

## Migration note
This spec describes the **target**. Today's code uses a "witness blesses one master" model with `CLAIM_HOLDDOWN_MS`. The two models converge on Scenario A; Scenario B is what differs (today: holddown-based bless; target: explicit `lms` tag).
