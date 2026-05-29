# bedrock_d/cluster/rename.py

Two-step saga that changes the cluster's display tag (`cluster_info.cluster_name`)
in rqlite. The cluster's true identity is `cluster_uuid`, which is immutable, so this
is a pure rename, not a re-identity. It is registered under the saga kind
`cluster_rename` and run by the orchestrator's saga executor; the trigger is the
`bedrock cluster rename` CLI path, which sets `new_name` in the saga ctx. Daemons,
services, DRBD, and SeaweedFS all key off `cluster_uuid`, so nothing beyond the
display name needs to change.

## Functions / Classes

### `class ClusterRename` — `@saga("cluster_rename")`
A two-step saga that validates a proposed name, then writes it to rqlite.
- **In (ctx):** `new_name: str` — the desired display name (1..64 chars,
  `[A-Za-z0-9_.-]`), set by the caller.
- **Out:** no ctx outputs. Side effect: one UPDATE on the singleton `cluster_info`
  row plus a `bedrock_meta.revision` bump in rqlite. No files, services, or
  subprocesses touched directly.

#### `step("validate_request")`
Strips and validates `new_name`; rewrites `ctx["new_name"]` to the canonical
(stripped) value.
- **In:** `ctx["new_name"]`.
- **Out:** raises `ValueError` if empty, or if it fails `^[A-Za-z0-9_.-]{1,64}$`
  (`_NAME_PATTERN`). On success, stores the stripped name back into ctx. No external
  side effects — this is the cheap guard that runs before rqlite is touched.

#### `step("write_rqlite_cluster_info")`
Records the new name in rqlite.
- **In:** `ctx["new_name"]` (already canonicalised).
- **Out:** opens a `RqliteClient` context manager and calls
  `state.set_cluster_name(new_name, client=...)`, which issues a single
  `UPDATE cluster_info SET cluster_name = ?, updated_at = ? WHERE id = 1` and bumps
  `bedrock_meta.revision`. Returns nothing to ctx; logs the recorded name and the new
  rqlite revision (`set_cluster_name` returns the new revision int, logged only).

## How it works

Steps run in declaration order; their names are persisted to
`operation_steps.step_name` so the saga is crash-resumable.

```
  bedrock cluster rename <name>   (sets ctx.new_name)
            │
            ▼
  validate_request ──► strip + ^[A-Za-z0-9_.-]{1,64}$ ──► fail fast (ValueError)
            │  (no rqlite touched yet)
            ▼
  write_rqlite_cluster_info
            │  UPDATE cluster_info SET cluster_name=? WHERE id=1
            │  bump bedrock_meta.revision
            ▼
  rqlite (single writer)
            │  revision change fans out to every node's rqlite_subscriber
            ├──► ~2 s: re-project state.json (view_builder._state_view)
            └──► ≤60 s: mDNS responder re-reads state.json → TXT cluster_name field
```

Validation runs first and deliberately before any rqlite contact, so an empty,
over-long, or unsafe name fails without a partial write. The write step is a single
UPDATE against the `cluster_info` singleton (`WHERE id = 1`) followed by a revision
bump; that bump is what wakes every node's subscriber to re-project the new name into
`state.json`, from which the mDNS responder surfaces it in its TXT record on the next
refresh tick.

The write is idempotent: re-running with the same name produces the same row state
and at most one extra subscriber tick, which is harmless. `cluster_uuid` is never
written here.

## Why

The name is a tag, not an identity, so a rename never disturbs the things that depend
on a stable cluster identity (DRBD, SeaweedFS, services), which all bind to
`cluster_uuid`. The character class is restricted to what is safe in the mDNS TXT
record, log lines, and systemd unit paths, so the name never needs escaping anywhere
it is projected; operators wanting a fancier label can format it client-side from
`cluster_uuid`.
