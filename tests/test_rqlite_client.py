"""Unit tests for installer/lib/rqlite_client.py.

Doesn't require a real rqlite instance — mocks the httpx Client's
`request()` method to return canned rqlite response shapes. This
exercises:

  * payload normalisation across the 3 call shapes (string, string+
    params, batch of [sql,params] lists)
  * /db/execute success + per-row error handling
  * /db/query success including dict-of-columns assembly
  * level=strong/weak passthrough to the URL
  * retry-on-5xx with bounded attempts
  * the schema bootstrap helper (apply_schema)
  * the watch() generator yields revision deltas

Run with: python3 tests/test_rqlite_client.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "installer"))

# Import under aliased name so we can also access the module-level
# helpers and constants.
from lib import rqlite_client as rc  # noqa: E402


def _fake_response(status: int, body):
    """Build a mock httpx.Response-like object with the rqlite shape."""
    resp = mock.MagicMock()
    resp.status_code = status
    if isinstance(body, dict):
        resp.json.return_value = body
        resp.text = json.dumps(body)
    else:
        resp.json.side_effect = ValueError("not JSON")
        resp.text = str(body)
    return resp


class TestPayloadNormalisation(unittest.TestCase):
    """The three accepted shapes for execute() inputs."""

    def test_single_string_no_params(self):
        out = rc._build_execute_payload("INSERT INTO t VALUES(1)", None)
        self.assertEqual(out, ["INSERT INTO t VALUES(1)"])

    def test_single_string_with_params(self):
        out = rc._build_execute_payload(
            "INSERT INTO t VALUES(?, ?)", ["a", 42])
        self.assertEqual(out, [["INSERT INTO t VALUES(?, ?)", "a", 42]])

    def test_batch_of_parameterised_lists(self):
        out = rc._build_execute_payload([
            ["INSERT INTO t VALUES(?, ?)", "a", 1],
            ["INSERT INTO t VALUES(?, ?)", "b", 2],
            "DELETE FROM stale_t",
        ], None)
        self.assertEqual(out, [
            ["INSERT INTO t VALUES(?, ?)", "a", 1],
            ["INSERT INTO t VALUES(?, ?)", "b", 2],
            "DELETE FROM stale_t",
        ])


class TestExecute(unittest.TestCase):
    def setUp(self):
        # Build a client and replace its internal httpx.Client with
        # a mock. The mock returns a per-test list of canned responses.
        self.client = rc.RqliteClient.__new__(rc.RqliteClient)
        self.client._base = "http://127.0.0.1:4001"
        self.client._client = mock.MagicMock()

    def test_execute_success(self):
        self.client._client.request.return_value = _fake_response(
            200, {"results": [{"rows_affected": 1}]})
        out = self.client.execute(
            "INSERT INTO nodes(node_name, host) VALUES(?, ?)",
            params=["sim-1", "192.168.2.201"],
        )
        self.assertEqual(out, [{"rows_affected": 1}])
        call = self.client._client.request.call_args
        self.assertEqual(call.args, ("POST", "/db/execute?transaction"))
        self.assertEqual(
            call.kwargs["json"],
            [["INSERT INTO nodes(node_name, host) VALUES(?, ?)",
              "sim-1", "192.168.2.201"]],
        )

    def test_execute_per_row_error_raises(self):
        self.client._client.request.return_value = _fake_response(
            200, {"results": [{"error": "constraint violation"}]})
        with self.assertRaises(rc.RqliteRowError) as cm:
            self.client.execute("INSERT INTO nodes(node_name) VALUES('x')")
        self.assertIn("constraint violation", str(cm.exception))

    def test_execute_4xx_raises_no_retry(self):
        self.client._client.request.return_value = _fake_response(
            400, {"error": "bad sql"})
        with self.assertRaises(rc.RqliteError):
            self.client.execute("INVALID SQL")
        # Should NOT have retried — 4xx is terminal
        self.assertEqual(self.client._client.request.call_count, 1)

    def test_execute_5xx_retries_then_raises(self):
        self.client._client.request.return_value = _fake_response(
            503, {"error": "leader changing"})
        with mock.patch.object(rc.time, "sleep"):  # don't actually sleep
            with self.assertRaises(rc.RqliteError):
                self.client.execute("INSERT INTO t VALUES(1)")
        # Should have retried up to RETRY_ATTEMPTS times
        self.assertEqual(
            self.client._client.request.call_count, rc.RETRY_ATTEMPTS
        )

    def test_execute_5xx_then_success_recovers(self):
        self.client._client.request.side_effect = [
            _fake_response(503, {"error": "leader changing"}),
            _fake_response(200, {"results": [{"rows_affected": 1}]}),
        ]
        with mock.patch.object(rc.time, "sleep"):
            out = self.client.execute("INSERT INTO t VALUES(1)")
        self.assertEqual(out, [{"rows_affected": 1}])
        self.assertEqual(self.client._client.request.call_count, 2)

    def test_execute_no_transaction_url(self):
        self.client._client.request.return_value = _fake_response(
            200, {"results": [{}]})
        self.client.execute("DELETE FROM t", transaction=False)
        call = self.client._client.request.call_args
        self.assertEqual(call.args, ("POST", "/db/execute"))


class TestQuery(unittest.TestCase):
    def setUp(self):
        self.client = rc.RqliteClient.__new__(rc.RqliteClient)
        self.client._base = "http://127.0.0.1:4001"
        self.client._client = mock.MagicMock()

    def test_query_assembles_dicts(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{
                "columns": ["node_name", "host", "loopback_ip"],
                "values": [
                    ["sim-1", "192.168.2.201", "100.42.42.1"],
                    ["sim-2", "192.168.2.202", "100.42.42.2"],
                ],
            }]
        })
        rows = self.client.query("SELECT node_name, host, loopback_ip FROM nodes")
        self.assertEqual(rows, [
            {"node_name": "sim-1", "host": "192.168.2.201", "loopback_ip": "100.42.42.1"},
            {"node_name": "sim-2", "host": "192.168.2.202", "loopback_ip": "100.42.42.2"},
        ])

    def test_query_empty_result(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{"columns": ["node_name"], "values": []}]
        })
        rows = self.client.query("SELECT node_name FROM nodes")
        self.assertEqual(rows, [])

    def test_query_one_returns_first_or_none(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{
                "columns": ["revision"],
                "values": [[42]],
            }]
        })
        row = self.client.query_one("SELECT revision FROM bedrock_meta WHERE id=1")
        self.assertEqual(row, {"revision": 42})

        self.client._client.request.return_value = _fake_response(200, {
            "results": [{"columns": ["x"], "values": []}]
        })
        self.assertIsNone(self.client.query_one("SELECT x FROM empty"))

    def test_level_strong_passed_to_url(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{"columns": [], "values": []}]
        })
        self.client.query("SELECT 1", level="strong")
        call = self.client._client.request.call_args
        self.assertEqual(call.args, ("POST", "/db/query?level=strong"))

    def test_query_with_params(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{
                "columns": ["node_name"],
                "values": [["sim-1"]],
            }]
        })
        self.client.query(
            "SELECT node_name FROM nodes WHERE host = ?",
            params=["192.168.2.201"],
        )
        call = self.client._client.request.call_args
        self.assertEqual(
            call.kwargs["json"],
            [["SELECT node_name FROM nodes WHERE host = ?", "192.168.2.201"]],
        )

    def test_query_row_error_raises(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{"error": "no such table: ghosts"}]
        })
        with self.assertRaises(rc.RqliteRowError):
            self.client.query("SELECT * FROM ghosts")


class TestRevisionAndWatch(unittest.TestCase):
    def setUp(self):
        self.client = rc.RqliteClient.__new__(rc.RqliteClient)
        self.client._base = "http://127.0.0.1:4001"
        self.client._client = mock.MagicMock()

    def _set_revision(self, n):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{
                "columns": ["revision"],
                "values": [[n]],
            }]
        })

    def test_revision_returns_int(self):
        self._set_revision(7)
        self.assertEqual(self.client.revision(), 7)

    def test_revision_zero_on_missing_row(self):
        self.client._client.request.return_value = _fake_response(200, {
            "results": [{"columns": ["revision"], "values": []}]
        })
        self.assertEqual(self.client.revision(), 0)

    def test_watch_yields_on_advance(self):
        # Revisions reported in order: 5, 5, 8, 8, 9
        responses = [
            _fake_response(200, {"results": [{"columns": ["revision"], "values": [[r]]}]} )
            for r in [5, 5, 8, 8, 9, 9]
        ]
        self.client._client.request.side_effect = responses

        stop_after = [3]   # mutable, stops after 3 advance checks

        def stop():
            stop_after[0] -= 1
            return stop_after[0] < 0

        with mock.patch.object(rc.time, "sleep"):
            yielded = list(self.client.watch(
                since_revision=5,
                interval_s=0.0,
                stop=stop,
            ))
        # First poll: 5 == since, no yield. Second: 5, no yield.
        # Third: 8 > 5, yield 8. Fourth: 8, no yield. Etc.
        # We've stopped after 3 calls; first yield is 8.
        self.assertIn(8, yielded)


class TestApplySchema(unittest.TestCase):
    """The schema-bootstrap helper splits the SQL file into statements
    and runs them as a single transactional execute."""

    def test_splits_on_semicolon_and_strips_comments(self):
        client = mock.MagicMock(spec=rc.RqliteClient)
        # Write a tiny schema file to a tmp path
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write("""
            -- a comment line
            CREATE TABLE a (x INTEGER);
            CREATE TABLE b (
                -- inline comment
                y INTEGER
            );

            -- trailing comment, no semicolon after
            """)
            tmp_path = f.name
        try:
            rc.apply_schema(client, tmp_path)
        finally:
            os.unlink(tmp_path)

        # Contract: apply_schema runs (1) the statement batch as ONE
        # transactional execute, (2) the INSERT OR IGNORE meta row, then
        # (3+) one additive ALTER TABLE per column that CREATE TABLE IF
        # NOT EXISTS can't add to a pre-existing table. Today: vms.priority
        # (self-heal) and nodes.state (C1 election-denominator lifecycle).
        # With a mock client the PRAGMA check reports each column absent,
        # so every ALTER is issued — so call_count = 2 + (#migrations).
        # Today: vms.priority, nodes.state, backup_targets.is_mirror,
        # vms.libvirt_xml, backup_targets.endpoint_id, witnesses.endpoint_id,
        # backup_targets.repo_password_enc, backup_targets.s3_access_key,
        # backup_targets.s3_secret_key_enc
        # — 9 migrations, so call_count = 2 + 9 = 11.
        self.assertEqual(client.execute.call_count, 11)
        first_call_args = client.execute.call_args_list[0]
        statements = first_call_args.args[0]
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE TABLE a", statements[0])
        self.assertIn("CREATE TABLE b", statements[1])
        # The trailing executes are the additive column migrations.
        alter_sqls = [client.execute.call_args_list[i].args[0]
                      for i in range(2, client.execute.call_count)]
        self.assertTrue(any("ALTER TABLE vms ADD COLUMN priority" in s
                            for s in alter_sqls))
        self.assertTrue(any("ALTER TABLE nodes ADD COLUMN state" in s
                            for s in alter_sqls))
        self.assertTrue(any("ALTER TABLE backup_targets ADD COLUMN is_mirror" in s
                            for s in alter_sqls))
        self.assertTrue(any("ALTER TABLE vms ADD COLUMN libvirt_xml" in s
                            for s in alter_sqls))
        self.assertTrue(any("ALTER TABLE backup_targets ADD COLUMN endpoint_id" in s
                            for s in alter_sqls))
        self.assertTrue(any("ALTER TABLE witnesses ADD COLUMN endpoint_id" in s
                            for s in alter_sqls))
        self.assertTrue(any(
            "ALTER TABLE backup_targets ADD COLUMN repo_password_enc" in s
            for s in alter_sqls))
        self.assertTrue(any(
            "ALTER TABLE backup_targets ADD COLUMN s3_access_key" in s
            for s in alter_sqls))
        self.assertTrue(any(
            "ALTER TABLE backup_targets ADD COLUMN s3_secret_key_enc" in s
            for s in alter_sqls))


class TestAsyncClient(unittest.IsolatedAsyncioTestCase):
    """Async variant — payload normalisation matches sync; we just
    smoke-test that the async path uses the same helper."""

    async def test_async_execute_uses_same_payload_shape(self):
        client = rc.AsyncRqliteClient.__new__(rc.AsyncRqliteClient)
        client._base = "http://127.0.0.1:4001"
        # AsyncClient is a context manager mock returning a mock
        # response in awaitable form.
        async def fake_request(method, url, json=None):
            return _fake_response(200, {"results": [{"rows_affected": 1}]})
        client._client = mock.MagicMock()
        client._client.request = fake_request

        out = await client.execute(
            "INSERT INTO nodes VALUES(?)", params=["x"]
        )
        self.assertEqual(out, [{"rows_affected": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
