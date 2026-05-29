# installer/lib/state.py

Owns this node's local on-disk state file, `/etc/bedrock/state.json`. Beyond
per-node identity fields (`node_name`, `loopback_ip`, `cluster_uuid`,
`bootstrap_done`, …) it carries two cluster base-layer facts that must survive
reboot without rqlite, because they are what *recovers* rqlite: `believed_master`
(who this node last thought was mgmt master, read on cold boot before quorum) and
`arbiter_uuid_history` (a 7-day rolling log of observed arbiter-DRBD current-UUIDs
that drives election eligibility). Writes are atomic and crash-durable, and a lost
file self-heals from the local `cluster.json`. Called on the boot path by netd and
`rqlite_setup`, and by the election/witness code for the UUID split-brain guard.

## Functions / Classes

### `load() -> dict`
Read and parse `state.json`.
- **In:** none.
- **Out:** the parsed state dict; `{}` if the file is missing, 0-byte, truncated, or corrupt JSON (treated as missing so callers self-heal rather than crash). Does not unlink a corrupt file.

### `save(state: dict)`
Atomically and durably persist a state dict.
- **In:** `state` → the full dict to write.
- **Out:** none. Side effects: writes `state.json` via tempfile + `os.fsync` + `os.replace`, then fsyncs the parent directory; creates the parent dir if absent. Raises `RuntimeError` if `state` has neither `bootstrap_done` nor `node_name` (refuses to persist a corrupt/empty dict).

### `recover_identity_from_cluster_json(state: dict | None = None) -> dict`
Reconstruct lost identity fields from the local `cluster.json` + hostname.
- **In:** `state` → a pre-loaded dict, or `None` to `load()` it.
- **Out:** the (possibly repaired) state dict. Side effect: if it recovered the full identity AND something changed, `save()`s the repaired dict; otherwise writes nothing.

### `load_or_recover() -> dict`
Boot-path entry point: `load()`, self-healing from `cluster.json` if identity is incomplete.
- **In:** none.
- **Out:** a state dict with identity fields filled in where recoverable. Side effect: may `save()` via the recovery path.

### `get_believed_master(state: dict | None = None) -> str | None`
Return who this node last believed was mgmt master.
- **In:** `state` → pre-loaded dict, or `None` to `load()`.
- **Out:** the node name string, or `None`. Pure read.

### `set_believed_master(node_name: str | None, state: dict | None = None) -> dict`
Persist the believed master.
- **In:** `node_name` → the master's name (or `None` to clear); `state` → pre-loaded dict to fold into, or `None` to `load()`.
- **Out:** the saved dict. Side effect: `save()`s.

### `record_arbiter_uuid(uuid: str, state: dict | None = None, now: float | None = None) -> dict`
Record an observation of the arbiter (`cluster` singleton) DRBD current-UUID.
- **In:** `uuid` → observed UUID (any format; normalized); `state` → pre-loaded dict or `None`; `now` → unix time override or `None` for `time.time()`.
- **Out:** the saved dict. Side effect: `save()`s on a change. No-op (returns the dict unsaved) on a blank UUID or when the UUID already matches the newest entry.

### `classify_arbiter_uuid(uuid: str, state: dict | None = None, now: float | None = None) -> str`
Classify a candidate's advertised arbiter UUID against this node's history.
- **In:** as `record_arbiter_uuid`.
- **Out:** one of `UUID_CURRENT`, `UUID_UNSEEN`, `UUID_SUPERSEDED`. Pure read — never mutates or persists.

### `is_uuid_eligible(uuid: str, state: dict | None = None, now: float | None = None) -> bool`
Vote eligibility shortcut over `classify_arbiter_uuid`.
- **In:** as above.
- **Out:** `True` for current/unseen, `False` for superseded. Pure read.

### Private helpers
- `_summarize_stack() -> str` — three-frame caller-stack snippet (`file:lineno <- …`) used in the empty-save `RuntimeError` to point at the mis-saving caller.
- `_read_cluster_json() -> dict` — parse `cluster.json`; `{}` on any error.
- `_normalize_uuid(uuid: str) -> str` — lower-case, strip a `0x` prefix, strip whitespace and a trailing `;`, so UUIDs compare equal regardless of source format (matches `cluster_arbiter._read_local_drbd_uuid()`).
- `_prune_history(history, now) -> list[dict]` — drop entries older than 7 days (age measured from `ts_superseded` if set, else `ts_seen`); the newest entry is always kept.

### Module constants
`STATE_FILE` = `/etc/bedrock/state.json`; `CLUSTER_JSON_FILE` = `/etc/bedrock/cluster.json`; `_IDENTITY_KEYS` = `("cluster_uuid", "node_name", "loopback_ip")`; `UUID_HISTORY_RETENTION_S` = `7*24*3600`; classification strings `UUID_CURRENT` / `UUID_UNSEEN` / `UUID_SUPERSEDED`.

## How it works

**Durable write.** `save()` never does a plain `write_text`. A concurrent reader
(`rqlite_setup --render-env` reads `state.json` on every rqlited restart) must
never see a partial file, and a power loss must never leave a 0-byte file. So it:

```
mkstemp(.state.json.*.tmp in same dir)
  └─ write JSON → flush → fsync(file)      # data durable first
os.replace(tmp, state.json)                # atomic swap vs readers
fsync(parent dir fd)                       # the rename itself durable
(on any error: unlink tmp, re-raise)
```

The directory fsync is load-bearing: without it an unclean reboot can journal the
rename but lose the tmp file's data blocks, yielding a 0-byte `state.json`. Before
any of this, `save()` refuses a dict lacking both `bootstrap_done` and `node_name`,
raising with a caller-stack snippet — so a caller that loaded `{}` (missing file)
and tried to write it back fails loudly instead of cementing the corruption.

**Self-heal.** A node needs `cluster_uuid`, `node_name`, `loopback_ip` to bring up
netd + rqlite. `load_or_recover()` (the boot entry point) loads, and if any of
those three is missing, calls `recover_identity_from_cluster_json()`:

```
state.json present & identity complete ──> use it
state.json missing / 0-byte / partial  ──> read cluster.json
                                            ├─ node_name   = hostname (os.uname)
                                            ├─ cluster_uuid/_name = cluster.json top-level
                                            ├─ loopback_ip = nodes[hostname].loopback_ip
                                            ├─ role        = nodes[hostname].role
                                            └─ bootstrap_done = True (default)
                                            save() ONLY if identity now complete
                                                    AND something changed
```

`cluster.json` is the heal source because it is written once at init/join (not on
every netd tick) and survives the rename window that can lose `state.json`. The
hostname is the Bedrock `node_name` and never changes, so it is the lookup key.
Recovery-only fields are deliberately not restored: `believed_master` stays unset
(the election re-derives it; a stale hint is worse than none), and
`arbiter_uuid_history` restarts empty (an empty history classifies candidates as
UNSEEN/votable — acceptable because the hard promotion gate is a live `drbdadm
current-uuid` match against the witness marker, not this file).

**Arbiter UUID history → election eligibility.** The history is an ordered list,
newest last, each entry `{uuid, ts_seen, ts_superseded}`. A new generation arrives:

```
record_arbiter_uuid(new):
  norm = normalize(new)
  if blank: no-op
  if history[-1].uuid == norm: no-op            # already current
  else:
    history[-1].ts_superseded = now             # close the old generation
    history.append({uuid: norm, ts_seen: now, ts_superseded: None})
    prune >7-day entries (always keep newest)
    save()
```

A voter then classifies a candidate's advertised UUID against its own pruned
history:

```
newest entry matches       -> CURRENT     (votable)
not in history at all       -> UNSEEN      (assume newer -> votable)
present but superseded      -> SUPERSEDED  (REFUSE — stale generation)
blank candidate             -> UNSEEN
```

`is_uuid_eligible()` collapses that to votable-vs-refused. Pruning measures age
from `ts_superseded` when set (the generation is over) else `ts_seen`, and always
keeps the newest entry so the current generation can never expire out from under
the node.

## Why
The two non-identity facts live here, not in rqlite, precisely because they must
be readable on cold boot before quorum exists — they are the inputs that let a
node recover rqlite rather than depend on it. The 7-day cap on UUID history stops
a long-dead arbiter generation from vetoing elections forever.
