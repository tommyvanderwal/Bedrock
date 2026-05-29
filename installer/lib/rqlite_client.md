# installer/lib/rqlite_client.py

A thin HTTP/JSON client for **rqlite**, Bedrock's cluster-state store. rqlite
speaks HTTP natively, so this wrapper is just the SQL pipe between Python and the
per-node rqlite instance on `127.0.0.1:4001`: writes (`execute`), reads
(`query`/`query_one`) at a chosen consistency level, a monotonic `revision()`
counter, and a poll-based `watch()` for change loops. It holds no schema
knowledge — what tables and columns mean lives in callers (`view_builder.py`, the
mgmt app handlers, CLI/install scripts). Two surfaces are provided so neither
side has to bridge event loops: a sync `RqliteClient` (CLI, install scripts) and
an async `AsyncRqliteClient` (FastAPI handlers, orchestrator asyncio tasks).
Module-level `apply_schema`/`bump_revision` cover schema bootstrap and the watch
counter.

## Functions / Classes

### `class RqliteError(Exception)` / `class RqliteRowError(RqliteError)`
`RqliteError` is raised on any non-2xx HTTP response or local connection failure.
`RqliteRowError` (a subclass) is raised when an individual statement inside a
`/db/execute` batch or a `/db/query` reports a per-row `error` field, surfacing
partial failure as an exception rather than silent partial success.

### `class RqliteClient(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT_S)`
Sync client; reuses one `httpx.Client` connection pool for the process lifetime.
Usable as a context manager (`with RqliteClient() as c:` → `close()`).
- **In:** `host` (default `127.0.0.1`), `port` (default `4001`), `timeout`
  (default `10.0` s).
- **Out:** an instance bound to `https://host:port` with mTLS if
  `/etc/bedrock/node.crt`, `node.key.pem`, and `ca.crt` all exist, else
  `http://host:port`. Side effect: opens an httpx connection pool. Raises
  `RuntimeError` via `_check_httpx()` if `httpx` is not importable.

### `RqliteClient.execute(statements, *, params=None, transaction=True) -> list[dict]`
Run one or more INSERT/UPDATE/DELETE/CREATE statements via `POST /db/execute`.
- **In:** `statements` in one of three shapes — a single SQL string; a single
  string plus positional `params`; or a batch where each element is either a bare
  SQL string or a `[sql, *params]` list. `transaction=True` (default) appends
  `?transaction` so the whole batch is one all-or-nothing Raft commit;
  `transaction=False` commits each statement separately.
- **Out:** the list of per-statement result dicts from rqlite. Raises
  `RqliteRowError` if any result carries an `error` field; `RqliteError` on
  transport/HTTP failure. Side effect: a write replicated through Raft to all
  rqlite peers.

### `RqliteClient.query(sql, params=None, *, level="weak") -> list[dict]`
SELECT helper via `POST /db/query?level=<level>`.
- **In:** `sql` and optional positional `params`; `level` is `'none'` (read
  locally, no leader consult, can be very stale), `'weak'` (default; local
  follower that recently heard from the leader), or `'strong'` (via the leader,
  linearizable — use when the read must reflect a just-completed write).
- **Out:** a list of dict rows built by zipping the response `columns` with each
  row in `values`. Empty list if there are no results. Raises `RqliteRowError` on
  a query-level `error`.

### `RqliteClient.query_one(sql, params=None, *, level="weak") -> Optional[dict]`
Convenience wrapper over `query` returning the first row or `None`.

### `RqliteClient.revision() -> int`
Read `bedrock_meta.revision`, the monotonic counter callers seed `watch()` with.
- **Out:** the current revision as `int` (weak read of `id = 1`), or `0` if the
  row is absent.

### `RqliteClient.watch(since_revision=0, *, interval_s=0.5, stop=None) -> Iterator[int]`
Generator that polls `revision()` and yields the new value whenever it advances.
- **In:** `since_revision` seed; `interval_s` poll period; `stop` optional
  zero-arg callable — the loop returns when it returns truthy.
- **Out:** yields each new revision `int` as it passes the last-seen value. A
  transient `RqliteError` during the poll is swallowed (sleep, retry), so a
  connection blip does not end the generator; otherwise it loops until `stop`.

### `RqliteClient.close()` / `__enter__` / `__exit__`
Close the httpx pool; context-manager support.

### `class AsyncRqliteClient(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT_S)`
Async mirror of `RqliteClient`. `execute`, `query`, `query_one`, `revision` are
coroutines with identical signatures and semantics; `watch` is an async generator
(`async for rev in client.watch(...)`). Lifecycle is `aclose()` / `async with`.
It always dials `http://host:port` (no TLS branch).

### `apply_schema(client, schema_sql_path) -> None`
Apply `bedrock_schema.sql` to the cluster; idempotent.
- **In:** a `RqliteClient`; a path to the schema SQL file.
- **Out:** none. Side effects: executes the parsed CREATE statements in one
  transaction; `INSERT OR IGNORE` the singleton `bedrock_meta(id=1, revision=0,
  schema_ver=1, bootstrapped_at=now)` row; runs additive `_add_column_if_missing`
  migrations for `vms.priority` (`TEXT NOT NULL DEFAULT 'normal'`) and
  `nodes.state` (`TEXT NOT NULL DEFAULT 'active'`). Intended to run once at
  install time on the elected master; Raft replicates it to all peers.

### `bump_revision(client) -> int`
Atomically increment `bedrock_meta.revision` and return the new value.
- **In:** a `RqliteClient`.
- **Out:** the new revision `int` (read back with `level="strong"`), or `0` if
  the row is absent. Side effect: `UPDATE bedrock_meta SET revision = revision + 1
  WHERE id = 1`. Every mutation watchers should see calls this; callers bump
  **last** so a watcher that reads the new revision then fetches rows sees the
  committed mutation.

### Private helpers
- `_check_httpx()` — raises a `RuntimeError` with install instructions if `httpx`
  is not importable.
- `_build_execute_payload(statements, params)` — normalises the three call shapes
  into rqlite's list-of-lists wire format; shared by both clients.
- `_request_with_retry()` (one per client) — the request + retry plumbing.
- `_add_column_if_missing(client, table, column, coldef)` — idempotent
  `ALTER TABLE ADD COLUMN` guarded by a `PRAGMA table_info` presence check.

## How it works

**Transport selection (sync).** At construction `RqliteClient` checks for the
per-node cert triple under `/etc/bedrock/`:

```
node.crt + node.key.pem + ca.crt all exist?
   yes → https://host:port, HTTPTransport(verify=<SSLContext>, retries=0)
          ctx = default_context(cafile=ca.crt); ctx.load_cert_chain(node.crt, node.key.pem)
   no  → http://host:port,  HTTPTransport(retries=0)
```

The SSL context is built explicitly and handed to the transport's `verify=`
because, with a custom `transport=` supplied, the Client-level `verify=` is
ignored and `cert=` does not reliably present the client cert for mTLS. Plain
HTTP is the early-bootstrap path before `cluster_ca` has run; once the cert files
exist they persist, so TLS is used from then on. `AsyncRqliteClient` always dials
HTTP.

**Wire format.** `_build_execute_payload` collapses all three call shapes into
rqlite's list-of-lists: a bare string stays a string in the array; a string +
`params` becomes `[[sql, *params]]`; a batch maps each element to either the
string or `list(s)`. `query` builds `[[sql, *params]]` or `[sql]` the same way,
then unpacks `results[0]` into dict rows via `zip(columns, row)` per value list.

**Retry on leader change.** Both clients route every request through
`_request_with_retry` (`RETRY_ATTEMPTS = 4`, backoff `(0.05, 0.15, 0.5, 1.0)`):

```
for attempt in 0..3:
    send request
    ├─ connect/read/timeout error → record, retry
    ├─ HTTP 5xx                   → record (leader-change blip), retry
    ├─ HTTP 4xx                   → raise RqliteError immediately (no retry)
    ├─ 2xx, resp.json() parses    → return body
    └─ 2xx, bad JSON              → raise RqliteError
    sleep RETRY_BACKOFF_S[attempt]  (skipped after the last attempt)
attempts exhausted               → raise RqliteError (includes last_exc)
```

5xx is retriable because rqlite forwards writes to the elected leader and a brief
leader election can return 503; a client-side 4xx is a hard error and fails fast.

**Batch result inspection.** After a successful `/db/execute`, both `execute`
implementations walk `results` and raise `RqliteRowError` on the first element
carrying an `error` key, so a transactional batch that committed but reported a
statement error never returns as silent success.

**watch loop.** `watch` seeds `last = since_revision`, then on each tick checks
`stop()`, reads `revision()`, yields and advances `last` when the counter grew,
and sleeps `interval_s`. The caller pulls the actually-changed rows itself
(typically `view_builder.fold_since(rev)`); this client only signals *that*
something advanced. It is poll-based, not push.

**Schema apply.** `apply_schema` strips `/* … */` block comments (DOTALL regex),
then `-- …` line comments (truncating each line at the first `--`, a heuristic
that assumes no `--` inside string literals), before splitting on `;` — so a
semicolon inside a comment cannot fragment a surrounding CREATE. Non-empty chunks
are re-terminated with `;` and submitted as one transaction, followed by the
`bedrock_meta` singleton insert and the additive column migrations.
`_add_column_if_missing` reads `PRAGMA table_info(table)` first and no-ops if the
column is present; any exception around the check/ALTER is swallowed because a
concurrent applier may add the column in between — the column existing is the
goal, not the path. The migrations are needed because `CREATE TABLE IF NOT
EXISTS` will not add a column to a table that already exists.

## Why

- **Local socket by default** (`127.0.0.1:4001`): every node runs its own rqlite
  instance, so localhost is always reachable and the client never fights the
  cluster `/32` routing during transients.
- **Intentionally thin**: keeping schema knowledge out of this file makes it a
  pure HTTP/SQL pipe and concentrates table/column meaning in its callers.
- **`level` exposed per-read**: `weak` is fast and fresh enough in steady state;
  `strong` lets a caller force a leader-linearizable read when it must observe its
  own just-committed write (e.g. the `bump_revision` read-back).
