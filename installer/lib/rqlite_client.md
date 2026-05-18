# `rqlite_client.py`

**Module purpose.** Thin sync HTTP client for the per-node rqlite
on `127.0.0.1:4001` (and the arbiter on `.254:4011` when that
unit is up). Wraps `httpx` with the retry, leader-follow, and
batched-transaction semantics rqlite expects.

Used by every mutation in `bedrock_state.py`, every read in
`view_builder.build_snapshot`, and the `watch()` generator the
orchestrator's `rqlite_subscriber` consumes.

## Constants

- `DEFAULT_HOST = "127.0.0.1"` / `DEFAULT_PORT = 4001` — per-node
  HTTP. (The arbiter on .254 is reached via the per-node forwarder,
  not directly; only the master ever runs the arbiter unit.)
- `DEFAULT_TIMEOUT = 5.0` s, `DEFAULT_RETRIES = 4` — retry-with-
  backoff knobs.
- `apply_schema_sql` — embedded contents of `bedrock_schema.sql`
  (loaded at module import) so `apply_schema()` can run without
  touching disk.

## Classes

- `RqliteError(Exception)` — wraps HTTP non-2xx and timeout.
  `repr()` includes the URL + truncated body.
- `RqliteClient(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=...)`
  — context-manager-compatible. Holds an `httpx.Client` for
  connection re-use. Closes on `__exit__`.

  Methods:
  - `query(sql, params=None, *, level="strong") -> dict` — single
    SELECT through `POST /db/query?level=strong`. Returns the
    parsed `results[0]`. Raises `RqliteError` on non-2xx after
    retry.
  - `execute(statements, *, transaction=False) -> dict` —
    multi-statement write. `statements` is a list of
    `[sql_string, *params]` lists; if `transaction=True`, sent
    via `?transaction` so it's atomic. Caller in
    `bedrock_state.py` always passes `transaction=True`.
  - `revision() -> int` — `SELECT revision FROM bedrock_meta WHERE
    id=1` shortcut. Used by `watch()` and the orchestrator's
    revision-change detector.
  - `watch(*, since_revision, interval_s=0.5, stop=None) ->
    Iterator[int]` — generator that polls `revision()` every
    `interval_s` and yields the new value whenever it advances.
    `stop` is an optional zero-arg callable for clean shutdown.
    Transient `RqliteError`s pause + retry instead of bubbling
    (rqlite leader-step-down windows are short and self-resolving).

## Module-level helpers

- `apply_schema(client, path=None)` — split the schema SQL on
  `;\n` and run each statement via `execute(transaction=True)`.
  Idempotent: every `CREATE TABLE` uses `IF NOT EXISTS`.

## Internal retry plumbing

- `_request_with_retry(self, method, url, json=None) -> httpx.Response`
  — captures connect errors + 5xx, sleeps with linear backoff
  (0.25 s × attempt), retries up to `DEFAULT_RETRIES`. Reformats
  the final exception as `RqliteError` with the URL + body for
  the caller's log.
- `_redirect_if_needed(self, response) -> response` — rqlite
  returns `301` with the leader's URL when a follower is asked
  to write; we replay the request against the redirected URL.
- `_should_retry(self, exc, response) -> bool` — predicate
  encoding which failure shapes are worth a retry (network
  blip, 503 "leader not found", 504). Hard failures (4xx other
  than 503) bubble immediately.

## Lifecycle note

The orchestrator's `rqlite_subscriber._subscriber_pass` opens
one `RqliteClient` per outer pass and keeps it alive for the
duration of the `watch()` generator. `bedrock_state.py` mutations
open a per-call client unless one is passed in. Multiple clients
on the same node share the local rqlite process but each holds
its own httpx connection pool — fine for v1.0 scale.
