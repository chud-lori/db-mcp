import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import datetime
import types
import unittest
from decimal import Decimal
from unittest import mock

from db_mcp.engines import EngineError, postgres
from db_mcp.guard import ReadOnlyViolation

CFG = {
    "type": "postgres",
    "host": "localhost",
    "port": 5432,
    "database": "app",
    "user": "testuser",
    "password": "secret",
}


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, args=None):
        self._conn.executed.append((sql, args))
        if sql.lstrip().upper().startswith("SET "):
            self.description = None
            self._rows = []
        else:
            self.description = self._conn.description
            self._rows = list(self._conn.rows)

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows=(), description=None):
        self.rows = list(rows)
        self.description = description
        self.executed = []  # [(sql, args), ...]
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def fake_pg8000(conn):
    mod = types.ModuleType("pg8000")
    mod.connect = mock.Mock(return_value=conn)
    return mod


class PostgresEngineTests(unittest.TestCase):
    def run_query(self, sql, rows=(), description=None, limit=100):
        conn = FakeConnection(rows=rows, description=description)
        mod = fake_pg8000(conn)
        with mock.patch.dict(sys.modules, {"pg8000": mod}):
            result = postgres.query(CFG, sql, None, limit)
        return result, conn, mod

    def test_write_statement_rejected_before_connect(self):
        conn = FakeConnection()
        mod = fake_pg8000(conn)
        with mock.patch.dict(sys.modules, {"pg8000": mod}):
            for sql in ("INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t", "DROP TABLE t"):
                with self.assertRaises(ReadOnlyViolation):
                    postgres.query(CFG, sql, None, 100)
        self.assertFalse(mod.connect.called)

    def test_session_set_read_only(self):
        _, conn, _ = self.run_query("SELECT 1", rows=[(1,)], description=[("?column?",)])
        executed_sql = [sql for sql, _ in conn.executed]
        self.assertIn("SET default_transaction_read_only = on", executed_sql)
        self.assertIn("SET statement_timeout = '30s'", executed_sql)
        self.assertTrue(conn.closed)

    def test_limit_appended_when_absent(self):
        _, conn, _ = self.run_query("SELECT * FROM users", limit=50)
        final_sql = conn.executed[-1][0]
        self.assertTrue(final_sql.endswith("LIMIT 50"), final_sql)

    def test_existing_limit_preserved(self):
        _, conn, _ = self.run_query("SELECT * FROM users LIMIT 5", limit=50)
        final_sql = conn.executed[-1][0]
        self.assertEqual(final_sql.count("LIMIT"), 1)
        self.assertIn("LIMIT 5", final_sql)

    def test_rows_are_json_safe_dicts(self):
        description = [("id",), ("amount",), ("created",), ("blob",)]
        rows = [
            (1, Decimal("12.50"), datetime.datetime(2026, 7, 16, 9, 30, 0), b"\x00\x01\x02"),
        ]
        result, _, _ = self.run_query("SELECT * FROM payments", rows=rows, description=description, limit=10)
        self.assertEqual(
            result["rows"],
            [{"id": 1, "amount": "12.50", "created": "2026-07-16T09:30:00", "blob": "<3 bytes>"}],
        )
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertIsInstance(result["elapsed_ms"], int)

    def test_truncated_when_row_count_hits_limit(self):
        description = [("id",)]
        rows = [(1,), (2,)]
        result, _, _ = self.run_query("SELECT id FROM t", rows=rows, description=description, limit=2)
        self.assertTrue(result["truncated"])

    def test_missing_driver_raises_engine_error(self):
        with mock.patch.dict(sys.modules, {"pg8000": None}):
            with self.assertRaises(EngineError) as ctx:
                postgres.query(CFG, "SELECT 1", None, 100)
        self.assertIn("pip install pg8000", str(ctx.exception))

    def test_schema_lists_tables(self):
        conn = FakeConnection(
            rows=[("public", "users", "BASE TABLE"), ("app", "orders", "VIEW")],
            description=[("table_schema",), ("table_name",), ("table_type",)],
        )
        with mock.patch.dict(sys.modules, {"pg8000": fake_pg8000(conn)}):
            result = postgres.schema(CFG, None)
        self.assertEqual(
            result["tables"],
            [
                {"schema": "public", "name": "users", "type": "BASE TABLE"},
                {"schema": "app", "name": "orders", "type": "VIEW"},
            ],
        )
        self.assertTrue(conn.closed)

    def test_schema_target_is_parameterized(self):
        conn = FakeConnection(
            rows=[("id", "integer", "NO", "nextval('users_id_seq')"), ("email", "text", "YES", None)],
            description=[("column_name",), ("data_type",), ("is_nullable",), ("column_default",)],
        )
        with mock.patch.dict(sys.modules, {"pg8000": fake_pg8000(conn)}):
            result = postgres.schema(CFG, "users'; DROP TABLE users; --")
        final_sql, args = conn.executed[-1]
        self.assertNotIn("DROP TABLE", final_sql)
        self.assertEqual(args, ("public", "users'; DROP TABLE users; --"))
        self.assertEqual(
            result["columns"][0],
            {"name": "id", "type": "integer", "nullable": False, "default": "nextval('users_id_seq')"},
        )
        self.assertEqual(
            result["columns"][1],
            {"name": "email", "type": "text", "nullable": True, "default": None},
        )

    def test_schema_qualified_target(self):
        conn = FakeConnection(rows=[], description=[("column_name",), ("data_type",), ("is_nullable",), ("column_default",)])
        with mock.patch.dict(sys.modules, {"pg8000": fake_pg8000(conn)}):
            postgres.schema(CFG, "billing.invoices")
        _, args = conn.executed[-1]
        self.assertEqual(args, ("billing", "invoices"))


if __name__ == "__main__":
    unittest.main()
