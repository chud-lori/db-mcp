"""Tests for the mysql engine — no network, no real database, no PyMySQL.

A fake pymysql module is patched into sys.modules; its connections record
every executed statement so guard behavior and session hardening can be
asserted without a server.
"""

from __future__ import annotations

import datetime
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp import guard
from db_mcp.engines import EngineError, mysql

CFG = {
    "type": "mysql",
    "host": "localhost",
    "port": 3306,
    "database": "legacy",
    "user": "db_mcp_ro",
    "password": "x",
}


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.conn.executed.append((sql, params))

    def fetchall(self):
        return self.conn.rows


class FakeConnection:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.executed: list[tuple[str, object]] = []
        self.closed = False
        self.kwargs: dict = {}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def make_fake_pymysql(rows=()):
    """Build a fake pymysql (+ .cursors) module pair recording connections."""
    mod = types.ModuleType("pymysql")
    cursors = types.ModuleType("pymysql.cursors")
    cursors.DictCursor = type("DictCursor", (), {})
    mod.cursors = cursors
    mod.connections = []

    def connect(**kwargs):
        conn = FakeConnection(list(rows))
        conn.kwargs = kwargs
        mod.connections.append(conn)
        return conn

    mod.connect = connect
    return mod, cursors


class MySQLEngineTest(unittest.TestCase):
    def run_query(self, sql, rows=(), limit=100):
        """Run mysql.query against a fake driver; return (result, fake_module)."""
        mod, cursors = make_fake_pymysql(rows)
        with mock.patch.dict(sys.modules, {"pymysql": mod, "pymysql.cursors": cursors}):
            result = mysql.query(CFG, sql, None, limit)
        return result, mod

    def test_write_statement_rejected_before_connect(self):
        mod, cursors = make_fake_pymysql()
        with mock.patch.dict(sys.modules, {"pymysql": mod, "pymysql.cursors": cursors}):
            with self.assertRaises(guard.ReadOnlyViolation):
                mysql.query(CFG, "DELETE FROM users", None, 100)
        self.assertEqual(mod.connections, [])  # guard fired before any connect

    def test_session_read_only_statement_executed(self):
        _, mod = self.run_query("SELECT 1")
        conn = mod.connections[0]
        executed = [sql for sql, _ in conn.executed]
        self.assertIn("SET SESSION TRANSACTION READ ONLY", executed)
        self.assertTrue(conn.closed)

    def test_limit_appended_when_absent(self):
        _, mod = self.run_query("SELECT * FROM users", limit=50)
        last_sql = mod.connections[0].executed[-1][0]
        self.assertTrue(last_sql.endswith("LIMIT 50"), last_sql)

    def test_existing_limit_above_cap_rejected(self):
        mod, cursors = make_fake_pymysql()
        with mock.patch.dict(sys.modules, {"pymysql": mod, "pymysql.cursors": cursors}):
            with self.assertRaises(guard.ReadOnlyViolation):
                mysql.query(CFG, "SELECT * FROM users LIMIT 5000", None, 100)
        self.assertEqual(mod.connections, [])

    def test_json_safe_coercion(self):
        rows = [
            {
                "price": Decimal("12.50"),
                "created": datetime.datetime(2026, 7, 16, 9, 30, 0),
                "blob": b"\x00\x01\x02",
            }
        ]
        result, _ = self.run_query("SELECT * FROM orders", rows=rows)
        row = result["rows"][0]
        self.assertEqual(row["price"], "12.50")
        self.assertEqual(row["created"], "2026-07-16T09:30:00")
        self.assertEqual(row["blob"], "<3 bytes>")
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertIsInstance(result["elapsed_ms"], int)

    def test_truncated_flag_when_row_count_hits_limit(self):
        rows = [{"n": 1}, {"n": 2}]
        result, _ = self.run_query("SELECT * FROM t", rows=rows, limit=2)
        self.assertTrue(result["truncated"])

    def test_missing_driver_raises_engine_error(self):
        with mock.patch.dict(sys.modules, {"pymysql": None, "pymysql.cursors": None}):
            with self.assertRaises(EngineError) as ctx:
                mysql.query(CFG, "SELECT 1", None, 100)
        self.assertIn("pip install PyMySQL", str(ctx.exception))

    def test_schema_no_target_lists_tables(self):
        rows = [{"name": "users", "type": "BASE TABLE", "rows_estimate": 42}]
        mod, cursors = make_fake_pymysql(rows)
        with mock.patch.dict(sys.modules, {"pymysql": mod, "pymysql.cursors": cursors}):
            result = mysql.schema(CFG, None)
        self.assertEqual(result["tables"], [{"name": "users", "type": "BASE TABLE", "rows_estimate": 42}])
        last_sql, params = mod.connections[0].executed[-1]
        self.assertIn("information_schema.tables", last_sql)
        self.assertIsNone(params)

    def test_schema_target_uses_parameterized_query(self):
        rows = [{"name": "id", "type": "int", "nullable": "NO", "default": None, "key": "PRI"}]
        mod, cursors = make_fake_pymysql(rows)
        with mock.patch.dict(sys.modules, {"pymysql": mod, "pymysql.cursors": cursors}):
            result = mysql.schema(CFG, "users")
        self.assertEqual(
            result["columns"],
            [{"name": "id", "type": "int", "nullable": False, "default": None, "key": "PRI"}],
        )
        last_sql, params = mod.connections[0].executed[-1]
        self.assertIn("information_schema.columns", last_sql)
        self.assertEqual(params, ("users",))
        self.assertNotIn("users", last_sql)  # target passed as a parameter, never inlined


if __name__ == "__main__":
    unittest.main()
