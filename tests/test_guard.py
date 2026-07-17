from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp.guard import (
    MAX_LIMIT,
    ReadOnlyViolation,
    apply_sql_limit,
    clamp_limit,
    ensure_readonly_sql,
)


class EnsureReadonlySqlTests(unittest.TestCase):
    def test_plain_select_passes(self) -> None:
        self.assertEqual(ensure_readonly_sql("SELECT * FROM users;"), "SELECT * FROM users")

    def test_cte_select_passes(self) -> None:
        ensure_readonly_sql("WITH recent AS (SELECT id FROM leads) SELECT count(*) FROM recent")

    def test_writes_are_rejected(self) -> None:
        for sql in (
            "INSERT INTO users VALUES (1)",
            "UPDATE users SET name='x'",
            "DELETE FROM users",
            "DROP TABLE users",
            "TRUNCATE users",
            "CREATE TABLE t (id int)",
            "GRANT ALL ON users TO evil",
            "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
            "SELECT * FROM users INTO OUTFILE '/tmp/x'",
        ):
            with self.assertRaises(ReadOnlyViolation, msg=sql):
                ensure_readonly_sql(sql)

    def test_multiple_statements_rejected(self) -> None:
        with self.assertRaises(ReadOnlyViolation):
            ensure_readonly_sql("SELECT 1; DROP TABLE users")

    def test_write_keyword_inside_string_literal_is_fine(self) -> None:
        # The stripper must not be fooled by data that *mentions* writes.
        ensure_readonly_sql("SELECT * FROM logs WHERE msg = 'user ran DROP TABLE'")

    def test_write_hidden_behind_comment_is_still_caught(self) -> None:
        with self.assertRaises(ReadOnlyViolation):
            ensure_readonly_sql("SELECT 1 /* harmless */; DELETE FROM users -- oops")

    def test_set_and_reset_rejected(self) -> None:
        for sql in ("SET search_path TO evil", "RESET ALL"):
            with self.assertRaises(ReadOnlyViolation):
                ensure_readonly_sql(sql)


class LimitTests(unittest.TestCase):
    def test_clamp_defaults_and_caps(self) -> None:
        self.assertEqual(clamp_limit(None), 100)
        self.assertEqual(clamp_limit(50), 50)
        self.assertEqual(clamp_limit(999999), MAX_LIMIT)
        with self.assertRaises(ReadOnlyViolation):
            clamp_limit(0)

    def test_limit_appended_when_absent(self) -> None:
        self.assertEqual(apply_sql_limit("SELECT * FROM t", 100), "SELECT * FROM t LIMIT 100")

    def test_existing_limit_kept(self) -> None:
        self.assertEqual(apply_sql_limit("SELECT * FROM t LIMIT 5", 100), "SELECT * FROM t LIMIT 5")

    def test_oversized_existing_limit_rejected(self) -> None:
        with self.assertRaises(ReadOnlyViolation):
            apply_sql_limit(f"SELECT * FROM t LIMIT {MAX_LIMIT + 1}", 100)

    def test_show_and_explain_not_limited(self) -> None:
        self.assertEqual(apply_sql_limit("SHOW TABLES", 100), "SHOW TABLES")
        self.assertEqual(apply_sql_limit("EXPLAIN SELECT 1", 100), "EXPLAIN SELECT 1")

    def test_limit_inside_string_literal_is_ignored(self) -> None:
        # 'LIMIT 9999' as data must not satisfy (or trip) the limit check.
        out = apply_sql_limit("SELECT * FROM t WHERE note = 'LIMIT 9999'", 100)
        self.assertTrue(out.endswith("LIMIT 100"))


if __name__ == "__main__":
    unittest.main()
