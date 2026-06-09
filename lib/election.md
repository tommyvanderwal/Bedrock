# installer/lib/election.py

A pure, side-effect-free weighted-vote election for the cluster base layer. It
decides whether this node is **Leader**, **Follower**, or in **NoQuorum**, given
a snapshot of observable state (active node set, peer reachability, peer acks,
witness counts, current mgmt_master). It carries no rqlite dependency because it
is the thing that *recovers* rqlite: netd calls `compute()` on each election
tick and feeds the outcome into the arbiter-takeover path
(`lib/cluster_arbiter.py`), which is gated on a Leader result. The two marker
helpers manage a sticky on-disk override that forces NoQuorum.

## Functions / Classes

### `class Outcome(str, Enum)`
The three election results: `LEADER` (`"leader"`), `FOLLOWER` (`"follower"`),
`NO_QUORUM` (`"noquorum"`).

### `class Election` (frozen dataclass)
The full result of one `compute()` call.
- **Fields:** `outcome: Outcome`; `my_votes: int`; `total_votes: int`;
  `majority: int`; `should_set_mgmt_master: bool`; `reachable_peers: tuple[str, ...]`;
  `acking_peers: tuple[str, ...]` (default `()`); `reason: str` (default `""`, a
  human-readable explanation of the decision).
- `should_set_mgmt_master` is True only on a fresh self-promotion — it tells the
  caller to write self as `mgmt_master` in rqlite as post-takeover bookkeeping.
  It is NOT a takeover gate (the Leader outcome is).

### `compute(*, self_name, self_loopback, peer_liveness, node_loopbacks, current_mgmt_master, n_configured_witnesses=0, n_valid_witnesses=0, peer_acks=None, no_quorum_marker_path=NO_QUORUM_MARKER) -> Election`
One-shot election decision over the supplied snapshot. Pure: reads only its
arguments and (the existence of) the marker file; writes nothing.
- **In:**
  - `self_name` — this node's name.
  - `self_loopback` — this node's loopback `/32` IP; its last octet is the
    tiebreak key (falls back to `node_loopbacks[self_name]` if empty).
  - `peer_liveness` — `dict[node_name, bool]` reachability from netd's neighbour
    table. Should exclude self; an entry for self is tolerated and forced to True.
    Used only for the reachable set and the ack tally, never for the denominator.
  - `node_loopbacks` — `dict[node_name, loopback_ip]` of the cluster's ACTIVE
    nodes (already filtered to `state=='active'` and not maintenance by netd). Its
    key set ∪ self is the election denominator.
  - `current_mgmt_master` — name of the current mgmt_master, or `None` if unset.
  - `n_configured_witnesses` — count of every witness in the rqlite `witnesses`
    table; the full witness denominator term.
  - `n_valid_witnesses` — witnesses that are both valid (a slot for every active
    node) and confirmed (our own slot read back). Only these add to `my_votes`.
  - `peer_acks` — `dict[node_name, bool]`; True iff that peer has acked THIS node
    as master-to-be in its heartbeat (it lost the master AND found our advertised
    arbiter-UUID eligible). Defaults to `{}`.
  - `no_quorum_marker_path` — path checked for the sticky override (defaults to
    `NO_QUORUM_MARKER`).
- **Out:** an `Election`. No side effects.

### `set_no_quorum_marker(reason="") -> None`
Drops the sticky no-quorum marker so subsequent `compute()` calls return
`NO_QUORUM` regardless of vote tally.
- **In:** `reason` — text written into the file (defaults to
  `"election: no quorum\n"`).
- **Out:** None. Side effect: creates `NO_QUORUM_MARKER` (and its parent dir).
  **Idempotent** — if the file already exists it returns without touching it, so
  the file's mtime is preserved. OSErrors are swallowed.

### `clear_no_quorum_marker() -> None`
Removes the marker (tolerating its absence).
- **In:** none.
- **Out:** None. Side effect: unlinks `NO_QUORUM_MARKER`; a missing file is
  ignored.

### `_loopback_octet(ip) -> int` (private)
Parses the last dotted octet of an IP for the tiebreak; returns `9999` on any
parse failure, so an unparseable address never wins the lowest-octet race.

## Constants

- `NO_QUORUM_MARKER = Path("/run/bedrock-no-quorum")` — the sticky override file.
- `VOTES_PER_NODE = 100`, `VOTE_PER_WITNESS = 1` — the vote weights.

## How it works

Vote model is 100 per node, 1 per witness, so witnesses can break an exact
node-tie but never overrule a real node:

```
total_votes   = 100 * n_nodes + 1 * n_configured_witnesses
majority      = total_votes // 2 + 1
witness_votes = 1 * min(n_valid_witnesses, n_configured_witnesses)
```

`n_nodes` is the ACTIVE-node count: `members = set(node_loopbacks) ∪ {self}`,
i.e. every active node per rqlite plus self — NOT the heard-from set. This is the
load-bearing guard. A master that RESTARTS during a partition still reads all
active nodes from rqlite, so it sees `n_nodes = N`, stands alone below majority,
and falls to NoQuorum (safe) instead of faking a one-node cluster. `peer_liveness`
only feeds the reachable set (`members` whose liveness is True) and the ack tally.

ALL configured witnesses count in `total_votes`; only valid+confirmed ones add to
`my_votes`. A configured-but-invalid witness therefore *raises* the bar and biases
toward "do not fail over" (3 configured / 1 valid / 2 nodes: total=203,
majority=102, lone survivor 100+1=101 < 102 → no takeover → safe).

The decision tree, in order:

```
no-quorum marker present?
  └─ yes → NoQuorum (sticky; votes/totals all 0)
  no
   │
current_mgmt_master == self?
  └─ yes  my_votes = 100*|reachable| + witness_votes
           my_votes < majority → NoQuorum ("master but only X/Y")
           else                → Leader   ("already master")
   │ no
master_is_alive?  (current_mgmt_master set AND reachable)
  └─ yes → Follower ("following <master>")    [acks irrelevant in steady state]
   │ no   ── master gone / never set: the promote decision
   │
acking     = reachable peers (excl. self) whose peer_acks is True
node_votes = 100 * (1 + |acking|)
my_votes   = node_votes + witness_votes
  my_votes < majority → NoQuorum ("master gone; have X/Y acks+witness")
  else  ── have a quorum of acks → deterministic tiebreak:
            lowest-loopback-octet reachable contender proposes;
            anyone else → Follower ("deferring to lower-octet <winner>")
            self is lowest → Leader, should_set_mgmt_master=True
```

Two distinct vote semantics. While a master is alive (or self is already master)
the quorum check uses **reachability** (`100*|reachable|`) — Bedrock never seeks
quorum to unseat a live master. Only once the master is gone does it switch to
**active acks**: self plus each reachable peer that has explicitly acked this
node. A peer acks only after it too has lost the master and judged our arbiter
UUID eligible, so promotion requires affirmative agreement, not mere silence.

The octet tiebreak ensures two simultaneously-quorate candidates don't both
promote: the lowest-octet reachable contender is the sole proposer; everyone else
defers a tick and acks it instead, converging on a single Leader.

## Why

The election is a pure function so netd can re-evaluate it cheaply every tick and
so it has no rqlite dependency — it must keep working precisely when rqlite is the
unavailable thing being recovered. The 100/1 asymmetry keeps a fleet of witnesses
from ever overruling an actual node while still letting one tip an even split. The
sticky marker is mtime-stable on purpose: `vm_failover`'s suspend timer reads that
mtime as "when did this NoQuorum episode begin", so a per-tick rewrite would reset
the clock and the timer would never expire.
