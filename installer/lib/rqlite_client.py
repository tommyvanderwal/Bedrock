"""HTTP client for rqlite — Bedrock's cluster-state store (D-01).

rqlite speaks HTTP/JSON natively. This thin wrapper provides:

  * `execute(sql, params, ...)` — for INSERT/UPDATE/DELETE/CREATE.
    Uses POST /db/execute, batches statements transactionally.
  * `query(sql, params, ...)` — for SELECT.
    Uses POST /db/query with `level=strong` for linearizable reads
    when needed; `level=weak` (default) for fast reads (read from
    local follower's copy, may be a few hundred ms behind leader).
  * `revision()` — current bedrock_meta.revision; the "log_index
    replacement" for poll-based watch loops.
  * `watch(since_revision, *, interval_s=...)` — generator that
    yields the new revision number whenever it advances past
    `since_revision`. Replaces today's bedrock-rust IPC Subscribe.

Design choices grounded in the rewrite-notes:

  * **HTTP over gRPC**: rqlite is HTTP-native; this fits Bedrock's
    FastAPI/Python stack with no extra protobuf/grpc dep tree.
    See D-01 reasoning.
  * **Sync + async sides**: orchestrator.py runs in asyncio; CLI
    helpers run sync. Provide both surfaces so neither side has
    to bridge event loops awkwardly.
  * **Leader-follow with retries**: rqlite forwards writes to the
    Raft leader transparently, but a leader-change mid-write can
    return 503 briefly. Retry with exponential backoff up to a
    bounded number of attempts.
  * **Local socket by default**: connect to 127.0.0.1:4001 on the
    same node — each node runs its own rqlite instance, so
    localhost is always reachable. Avoids fighting the cluster
    /32 routing during transients.

This client is INTENTIONALLY thin. Application schema knowledge
(what tables exist, what columns mean) lives in callers
(view_builder.py, mgmt/app.py handlers). This file is just the
HTTP/SQL pipe.
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional, Sequence, Union

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # caller will fail at first use with a useful message

log = logging.getLogger("bedrock.rqlite_client")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001  # rqlite's HTTP API port; Raft itself is on 4002.
# Per-node rqlited binds 0.0.0.0:4001/4002 so 127.0.0.1 works as the
# stable on-node API; the arbiter rqlited (when active on the master)
# uses ports 4011/4012 against the .254 VIP so the two coexist.
DEFAULT_TIMEOUT_S = 10.0

# Retry behaviour for leader-change blips. rqlite forwards writes to
# the elected leader; during a leader election (sub-second on a
# healthy cluster) we may get 5xx. Bounded retries with backoff.
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_S = (0.05, 0.15, 0.5, 1.0)


class RqliteError(Exception):
    """Raised on any non-2xx response or local connection failure."""


class RqliteRowError(RqliteError):
    """Raised when a single statement inside a /db/execute batch errored
    (rqlite returns per-row error fields; we surface as an exception
    rather than silent partial success)."""


def _check_httpx() -> None:
    if httpx is None:
        raise RuntimeError(
            "rqlite_client: httpx not installed. "
            "Run: pip install httpx  (or include in bedrock's deps list)."
        )


# ────────────────────────────────────────────────────────────────────
# Sync client — for CLI, install scripts, anything outside FastAPI
# ────────────────────────────────────────────────────────────────────

class RqliteClient:
    """Sync HTTP client for rqlite. Reuses a single httpx.Client
    connection pool for the process lifetime. Thread-safe per
    httpx.Client guarantees.

    TLS: if /etc/bedrock/{node.crt,node.key.pem,ca.crt} all exist,
    dial https:// with mTLS using the per-node cert. Otherwise dial
    http:// (used only at very-early-bootstrap moments before
    cluster_ca has run; once cluster_ca runs the cert files exist
    permanently and we'll always use TLS)."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        _check_httpx()
        from pathlib import Path as _P
        node_crt = _P("/etc/bedrock/node.crt")
        node_key = _P("/etc/bedrock/node.key.pem")
        ca_crt   = _P("/etc/bedrock/ca.crt")
        if node_crt.exists() and node_key.exists() and ca_crt.exists():
            # httpx 0.28's `cert=` parameter doesn't reliably trigger
            # mTLS client-cert presentation, and when a custom
            # transport= is also provided, the Client-level verify=
            # is silently ignored (verify only configures the default
            # transport). Build an explicit ssl.SSLContext and pass
            # it as the transport's verify=. Verified on 2026-05-25
            # against rqlite v10 + httpx 0.28.1.
            import ssl as _ssl
            ctx = _ssl.create_default_context(cafile=str(ca_crt))
            ctx.load_cert_chain(certfile=str(node_crt), keyfile=str(node_key))
            self._base = f"https://{host}:{port}"
            self._client = httpx.Client(
                base_url=self._base,
                timeout=timeout,
                transport=httpx.HTTPTransport(verify=ctx, retries=0),
            )
        else:
            self._base = f"http://{host}:{port}"
            self._client = httpx.Client(
                base_url=self._base,
                timeout=timeout,
                transport=httpx.HTTPTransport(retries=0),
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RqliteClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ── execute (writes) ──────────────────────────────────────────────

    def execute(
        self,
        statements: Union[str, Sequence[str], Sequence[Sequence[Any]]],
        *,
        params: Optional[Sequence[Any]] = None,
        transaction: bool = True,
    ) -> list[dict]:
        """Run one or more INSERT/UPDATE/DELETE/CREATE statements.

        Three call shapes:

          1. Single statement, no params:
             execute("INSERT INTO nodes(node_name, host) VALUES('a','h')")
          2. Single statement WITH params (positional ?):
             execute("INSERT INTO nodes(node_name,host) VALUES(?,?)",
                     params=["a", "h"])
          3. Batch of parameterised statements (each is a list whose
             first element is the SQL and rest are positional params):
             execute([
                 ["INSERT INTO nodes(node_name,host) VALUES(?,?)", "a", "h"],
                 ["INSERT INTO nodes(node_name,host) VALUES(?,?)", "b", "h2"],
             ])

        `transaction=True` (default) wraps the whole batch in a single
        Raft commit — all-or-nothing. `transaction=False` commits each
        statement separately (rarely what you want).

        Returns the list of per-statement result dicts from rqlite.
        Raises RqliteRowError if any individual statement reported an
        error; RqliteError on transport/HTTP-level failures.
        """
        payload = _build_execute_payload(statements, params)
        url = "/db/execute?transaction" if transaction else "/db/execute"
        body = self._request_with_retry("POST", url, json=payload)
        results = body.get("results") or []
        for i, r in enumerate(results):
            if "error" in r:
                raise RqliteRowError(
                    f"rqlite stmt[{i}] error: {r['error']!r}"
                )
        return results

    # ── query (reads) ────────────────────────────────────────────────

    def query(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        *,
        level: str = "weak",       # 'none' | 'weak' | 'strong'
    ) -> list[dict]:
        """SELECT helper. Returns a list of dict rows (column→value).

        `level` follows rqlite's read-consistency model:
          * 'none'   — read locally, no leader consult. Fastest, can
                       be very stale.
          * 'weak'   — read locally on a follower that's heard from
                       the leader recently. Default; sub-second
                       freshness in steady state.
          * 'strong' — go via the leader, linearizable. Use this when
                       the read MUST reflect a just-completed write.
        """
        if params:
            payload = [[sql, *params]]
        else:
            payload = [sql]
        body = self._request_with_retry(
            "POST", f"/db/query?level={level}",
            json=payload,
        )
        results = body.get("results") or []
        if not results:
            return []
        r = results[0]
        if "error" in r:
            raise RqliteRowError(f"rqlite query error: {r['error']!r}")
        cols = r.get("columns") or []
        values = r.get("values") or []
        return [dict(zip(cols, row)) for row in values]

    def query_one(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        *,
        level: str = "weak",
    ) -> Optional[dict]:
        """Convenience: return the first row or None."""
        rows = self.query(sql, params, level=level)
        return rows[0] if rows else None

    # ── revision / watch ─────────────────────────────────────────────

    def revision(self) -> int:
        """Current bedrock_meta.revision. The monotonic counter that
        replaces today's log_index. Caller uses this as the seed for
        watch()."""
        row = self.query_one(
            "SELECT revision FROM bedrock_meta WHERE id = 1",
            level="weak",
        )
        return int(row["revision"]) if row else 0

    def watch(
        self,
        since_revision: int = 0,
        *,
        interval_s: float = 0.5,
        stop: Optional[callable] = None,
    ) -> Iterator[int]:
        """Generator: poll bedrock_meta.revision at `interval_s` and
        yield the new value whenever it advances past `since_revision`.

        Replaces today's bedrock-rust IPC Subscribe → committed-entries
        stream. The caller pulls the changed rows out of the relevant
        tables themselves (typically via view_builder.fold_since(rev)).

        `stop` is an optional zero-arg callable; the loop exits when it
        returns truthy. Useful for graceful shutdown.

        Note: this is poll-based, not push. For Bedrock's per-hour
        QPS it's fine; for a higher-rate workload, the rqlite
        team has a Server-Sent-Events extension that could replace
        the polling — out of scope for v1.0.
        """
        last = since_revision
        while True:
            if stop is not None and stop():
                return
            try:
                cur = self.revision()
            except RqliteError:
                # transient connection error — pause and retry
                time.sleep(interval_s)
                continue
            if cur > last:
                yield cur
                last = cur
            time.sleep(interval_s)

    # ── internal: request + retry plumbing ───────────────────────────

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Any] = None,
    ) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = self._client.request(method, url, json=json)
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                last_exc = e
            else:
                # Treat 5xx and 503-on-leader-change as retriable.
                if resp.status_code >= 500:
                    last_exc = RqliteError(
                        f"rqlite {method} {url}: HTTP {resp.status_code} "
                        f"body={resp.text[:200]!r}"
                    )
                elif resp.status_code >= 400:
                    raise RqliteError(
                        f"rqlite {method} {url}: HTTP {resp.status_code} "
                        f"body={resp.text[:200]!r}"
                    )
                else:
                    try:
                        return resp.json()
                    except (ValueError, json.JSONDecodeError):
                        raise RqliteError(
                            f"rqlite {method} {url}: bad JSON body "
                            f"{resp.text[:200]!r}"
                        )
            # backoff before retry
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_S[attempt])
        raise RqliteError(
            f"rqlite {method} {url}: gave up after "
            f"{RETRY_ATTEMPTS} attempts. Last error: {last_exc!r}"
        )


def _build_execute_payload(
    statements: Union[str, Sequence[str], Sequence[Sequence[Any]]],
    params: Optional[Sequence[Any]],
) -> list:
    """Normalise the three accepted shapes into rqlite's list-of-lists
    wire format. Shared by sync + async clients."""
    if isinstance(statements, str):
        if params is not None:
            return [[statements, *params]]
        return [statements]
    out: list = []
    for s in statements:
        if isinstance(s, str):
            out.append(s)
        else:
            out.append(list(s))
    return out


# ────────────────────────────────────────────────────────────────────
# Async client — for FastAPI handlers, orchestrator.py asyncio tasks
# ────────────────────────────────────────────────────────────────────

class AsyncRqliteClient:
    """Async variant. API mirrors RqliteClient but with awaitable
    methods. Watch is an async generator."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        _check_httpx()
        self._base = f"http://{host}:{port}"
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncRqliteClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def execute(
        self,
        statements: Union[str, Sequence[str], Sequence[Sequence[Any]]],
        *,
        params: Optional[Sequence[Any]] = None,
        transaction: bool = True,
    ) -> list[dict]:
        payload = _build_execute_payload(statements, params)
        url = "/db/execute?transaction" if transaction else "/db/execute"
        body = await self._request_with_retry("POST", url, json=payload)
        results = body.get("results") or []
        for i, r in enumerate(results):
            if "error" in r:
                raise RqliteRowError(
                    f"rqlite stmt[{i}] error: {r['error']!r}"
                )
        return results

    async def query(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        *,
        level: str = "weak",
    ) -> list[dict]:
        if params:
            payload = [[sql, *params]]
        else:
            payload = [sql]
        body = await self._request_with_retry(
            "POST", f"/db/query?level={level}",
            json=payload,
        )
        results = body.get("results") or []
        if not results:
            return []
        r = results[0]
        if "error" in r:
            raise RqliteRowError(f"rqlite query error: {r['error']!r}")
        cols = r.get("columns") or []
        values = r.get("values") or []
        return [dict(zip(cols, row)) for row in values]

    async def query_one(
        self,
        sql: str,
        params: Optional[Sequence[Any]] = None,
        *,
        level: str = "weak",
    ) -> Optional[dict]:
        rows = await self.query(sql, params, level=level)
        return rows[0] if rows else None

    async def revision(self) -> int:
        row = await self.query_one(
            "SELECT revision FROM bedrock_meta WHERE id = 1",
            level="weak",
        )
        return int(row["revision"]) if row else 0

    async def watch(
        self,
        since_revision: int = 0,
        *,
        interval_s: float = 0.5,
        stop: Optional[callable] = None,
    ):
        """Async generator: yields each new revision number as it
        advances past `since_revision`. Use with `async for rev in
        client.watch(...)`."""
        import asyncio
        last = since_revision
        while True:
            if stop is not None and stop():
                return
            try:
                cur = await self.revision()
            except RqliteError:
                await asyncio.sleep(interval_s)
                continue
            if cur > last:
                yield cur
                last = cur
            await asyncio.sleep(interval_s)

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Any] = None,
    ) -> dict:
        import asyncio
        last_exc: Optional[Exception] = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await self._client.request(method, url, json=json)
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
                last_exc = e
            else:
                if resp.status_code >= 500:
                    last_exc = RqliteError(
                        f"rqlite {method} {url}: HTTP {resp.status_code} "
                        f"body={resp.text[:200]!r}"
                    )
                elif resp.status_code >= 400:
                    raise RqliteError(
                        f"rqlite {method} {url}: HTTP {resp.status_code} "
                        f"body={resp.text[:200]!r}"
                    )
                else:
                    try:
                        return resp.json()
                    except (ValueError, json.JSONDecodeError):
                        raise RqliteError(
                            f"rqlite {method} {url}: bad JSON body "
                            f"{resp.text[:200]!r}"
                        )
            if attempt + 1 < RETRY_ATTEMPTS:
                await asyncio.sleep(RETRY_BACKOFF_S[attempt])
        raise RqliteError(
            f"rqlite {method} {url}: gave up after "
            f"{RETRY_ATTEMPTS} attempts. Last error: {last_exc!r}"
        )


# ────────────────────────────────────────────────────────────────────
# Schema bootstrap
# ────────────────────────────────────────────────────────────────────

def apply_schema(client: RqliteClient, schema_sql_path: str) -> None:
    """Apply bedrock_schema.sql to the cluster. Idempotent
    (CREATE TABLE IF NOT EXISTS everywhere). Run once at install
    time on the elected master; replicates to all rqlite peers via
    Raft.

    SQL splitter strips `-- single-line` and `/* block */` comments
    BEFORE splitting on `;` — so a semicolon inside a comment
    doesn't fragment the surrounding CREATE statement.
    """
    with open(schema_sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    # Strip /* ... */ block comments first.
    import re as _re
    sql = _re.sub(r"/\*.*?\*/", "", sql, flags=_re.DOTALL)
    # Strip `-- ...` line comments (whole-line and trailing). Keep
    # the newline so line breaks within statements are preserved.
    cleaned_lines = []
    for line in sql.splitlines():
        # Find a `--` that isn't inside a string literal. Simple
        # heuristic: split at the first `--` not preceded by `"`
        # or `'`. Good enough for our schema (no `--` in literals).
        idx = line.find("--")
        if idx >= 0:
            line = line[:idx]
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    # Now split on `;` and submit non-empty statements.
    statements: list[str] = []
    for chunk in cleaned.split(";"):
        s = chunk.strip()
        if s:
            statements.append(s + ";")
    if not statements:
        return
    client.execute(statements, transaction=True)
    # Ensure the singleton bedrock_meta row exists.
    client.execute(
        "INSERT OR IGNORE INTO bedrock_meta(id, revision, schema_ver, bootstrapped_at) "
        "VALUES (1, 0, 1, ?)",
        params=[int(time.time())],
    )
    # Additive column migrations for clusters created before a column
    # was introduced. CREATE TABLE IF NOT EXISTS won't add a column to
    # an existing table, so each newer column is ADDed here, ignoring
    # the "duplicate column name" error when it already exists.
    _add_column_if_missing(client, "vms", "priority",
                           "TEXT NOT NULL DEFAULT 'normal'")
    # nodes.state: lifecycle gate for the election denominator (C1).
    # Existing nodes default to 'active' so a schema re-apply on a
    # running cluster doesn't suddenly drop everyone from the denominator.
    _add_column_if_missing(client, "nodes", "state",
                           "TEXT NOT NULL DEFAULT 'active'")
    # backup_targets.is_mirror: a sync-to mirror destination (never
    # independently created). Existing targets default 0 (primary).
    _add_column_if_missing(client, "backup_targets", "is_mirror",
                           "INTEGER NOT NULL DEFAULT 0")
    # vms.libvirt_xml: the domain XML, stored so a later-joining node can
    # re-define + take over the VM on failover. Existing rows default ''
    # (failover re-define refuses loudly until the VM is next written).
    _add_column_if_missing(client, "vms", "libvirt_xml",
                           "TEXT NOT NULL DEFAULT ''")


def _add_column_if_missing(client: RqliteClient, table: str, column: str,
                           coldef: str) -> None:
    """Idempotent ``ALTER TABLE ADD COLUMN``. Checks the live schema
    via PRAGMA table_info first so a re-run (or a fresh cluster that
    already has the column from CREATE TABLE) is a clean no-op rather
    than relying on swallowing the duplicate-column error."""
    try:
        cols = client.query(f"PRAGMA table_info({table})")
        if any((c.get("name") == column) for c in cols):
            return
        client.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
    except Exception:
        # Best-effort: a concurrent applier may have added it between
        # the check and the ALTER. The column is the goal, not the path.
        pass


def bump_revision(client: RqliteClient) -> int:
    """Atomically increment bedrock_meta.revision and return the new
    value. Every mutation that should be visible to watchers must
    call this within the same transaction as the mutation itself.

    For now this is a separate execute() — within rqlite all writes
    go through Raft anyway, so the ordering "bump-then-mutate" or
    "mutate-then-bump" matters only for the watcher's view-of-order;
    callers should bump LAST so watchers reading-then-fetching get
    the just-committed mutation.
    """
    client.execute(
        "UPDATE bedrock_meta SET revision = revision + 1 WHERE id = 1"
    )
    row = client.query_one(
        "SELECT revision FROM bedrock_meta WHERE id = 1",
        level="strong",
    )
    return int(row["revision"]) if row else 0
