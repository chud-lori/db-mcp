"""Tests for recover.py — the hints appended to a failed SQL call.

Pure logic: the engine callbacks are stubs, so no driver and no database.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp import recover

MYSQL_UNKNOWN_COLUMN = "(1054, \"Unknown column 'DATE_CREATE' in 'field list'\")"
PG_UNKNOWN_COLUMN = 'column "date_create" does not exist'
MYSQL_UNKNOWN_TABLE = "(1146, \"Table 'app.jobs' doesn't exist\")"
PG_UNKNOWN_TABLE = 'relation "jobs" does not exist'


def columns(mapping):
    return lambda table: mapping.get(table, [])


def tables(names):
    return lambda: list(names)


class TablesInTest(unittest.TestCase):
    def test_plain_from(self):
        self.assertEqual(recover.tables_in("SELECT * FROM job_tracking"), ["job_tracking"])

    def test_alias_and_joins(self):
        sql = "SELECT * FROM orders o JOIN users u ON u.id = o.user_id LEFT JOIN carts ON 1=1"
        self.assertEqual(recover.tables_in(sql), ["orders", "users", "carts"])

    def test_quoted_and_qualified_names_unquoted(self):
        self.assertEqual(recover.tables_in("SELECT * FROM `app`.`jobs`"), ["app.jobs"])
        self.assertEqual(recover.tables_in('SELECT * FROM "public"."jobs"'), ["public.jobs"])

    def test_deduped_case_insensitively_preserving_order(self):
        sql = "SELECT * FROM jobs JOIN other ON 1=1 JOIN JOBS ON 1=1"
        self.assertEqual(recover.tables_in(sql), ["jobs", "other"])

    def test_subquery_is_not_a_table(self):
        sql = "SELECT * FROM (SELECT id FROM jobs) t"
        self.assertEqual(recover.tables_in(sql), ["jobs"])


class ColumnHintTest(unittest.TestCase):
    def test_mysql_unknown_column_lists_real_columns(self):
        hint = recover.sql_error_hint(
            MYSQL_UNKNOWN_COLUMN,
            "SELECT DATE_CREATE FROM job_tracking LIMIT 100",
            columns({"job_tracking": ["id", "date_created", "status"]}),
            tables([]),
        )
        self.assertIn("columns in job_tracking: id, date_created, status", hint)

    def test_postgres_unknown_column_lists_real_columns(self):
        hint = recover.sql_error_hint(
            PG_UNKNOWN_COLUMN,
            "SELECT date_create FROM jobs LIMIT 100",
            columns({"jobs": ["id", "date_created"]}),
            tables([]),
        )
        self.assertIn("columns in jobs: id, date_created", hint)

    def test_every_joined_table_is_listed(self):
        hint = recover.sql_error_hint(
            MYSQL_UNKNOWN_COLUMN,
            "SELECT x FROM jobs j JOIN runs r ON r.job_id = j.id LIMIT 100",
            columns({"jobs": ["id"], "runs": ["job_id"]}),
            tables([]),
        )
        self.assertIn("columns in jobs: id", hint)
        self.assertIn("columns in runs: job_id", hint)

    def test_no_hint_when_lookup_finds_nothing(self):
        hint = recover.sql_error_hint(
            MYSQL_UNKNOWN_COLUMN, "SELECT x FROM jobs", columns({}), tables([])
        )
        self.assertEqual(hint, "")

    def test_unrelated_error_gets_no_hint(self):
        hint = recover.sql_error_hint(
            "syntax error at or near \")\"",
            "SELECT * FROM jobs",
            columns({"jobs": ["id"]}),
            tables(["jobs"]),
        )
        self.assertEqual(hint, "")

    def test_lookup_failure_never_replaces_the_error(self):
        def explode(_table):
            raise RuntimeError("information_schema unavailable")

        message = recover.enrich_sql_error(
            RuntimeError(MYSQL_UNKNOWN_COLUMN), "SELECT x FROM jobs", explode, tables([])
        )
        self.assertEqual(message, MYSQL_UNKNOWN_COLUMN)


class TableHintTest(unittest.TestCase):
    def test_mysql_unknown_table_lists_tables(self):
        hint = recover.sql_error_hint(
            MYSQL_UNKNOWN_TABLE, "SELECT * FROM jobs", columns({}), tables(["job_tracking", "runs"])
        )
        self.assertIn("tables in this database: job_tracking, runs", hint)

    def test_postgres_unknown_table_lists_tables(self):
        hint = recover.sql_error_hint(
            PG_UNKNOWN_TABLE, "SELECT * FROM jobs", columns({}), tables(["runs", "job_tracking"])
        )
        self.assertIn("tables in this database: job_tracking, runs", hint)  # sorted

    def test_long_lists_are_capped_and_counted(self):
        many = [f"t{i:03d}" for i in range(recover._MAX_NAMES_LISTED + 25)]
        hint = recover.sql_error_hint(
            MYSQL_UNKNOWN_TABLE, "SELECT * FROM jobs", columns({}), tables(many)
        )
        self.assertIn("(+25 more)", hint)


class EnrichTest(unittest.TestCase):
    def test_original_message_is_preserved_verbatim(self):
        message = recover.enrich_sql_error(
            RuntimeError(MYSQL_UNKNOWN_COLUMN),
            "SELECT DATE_CREATE FROM job_tracking",
            columns({"job_tracking": ["date_created"]}),
            tables([]),
        )
        self.assertTrue(message.startswith(MYSQL_UNKNOWN_COLUMN), message)
        self.assertIn("date_created", message)


if __name__ == "__main__":
    unittest.main()
