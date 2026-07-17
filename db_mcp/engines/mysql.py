"""MySQL engine (PyMySQL). Read-only query + schema inspection.

The guard validates/limits the SQL before we connect; the session is also
flipped to READ ONLY with a 30s SELECT cap — defense in depth on top of the
SELECT-only DB user in config.
"""

from __future__ import annotations

import datetime
import time
from decimal import Decimal
from typing import Any

from . import EngineError
from .. import guard


def _connect(cfg: dict[str, Any]):
    """Lazy driver import + connect; session hardened before returning."""
    try:
        import pymysql
        import pymysql.cursors
    except ImportError:
        raise EngineError("mysql driver missing — pip install PyMySQL") from None

    conn = pymysql.connect(
        host=cfg.get("host"),
        port=int(cfg.get("port", 3306)),
        database=cfg.get("database"),
        user=cfg.get("user"),
        password=cfg.get("password"),
        connect_timeout=10,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            try:
                # Only affects SELECT; absent on old MySQL/MariaDB — best effort.
                cur.execute("SET SESSION max_execution_time = 30000")
            except Exception:
                pass
    except Exception:
        conn.close()
        raise
    return conn


def _json_safe(value: Any) -> Any:
    """Coerce driver types to JSON-safe values."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    return value


def _coerce_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(val) for key, val in row.items()}


def query(cfg: dict[str, Any], query: str, target: str | None, limit: int) -> dict[str, Any]:
    """Execute one read-only statement. `target` is unused for SQL engines."""
    sql = guard.ensure_readonly_sql(query)
    sql = guard.apply_sql_limit(sql, limit)

    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            start = time.monotonic()
            cur.execute(sql)
            rows = cur.fetchall()
            elapsed_ms = int((time.monotonic() - start) * 1000)
    finally:
        conn.close()

    safe_rows = [_coerce_row(row) for row in rows]
    return {
        "rows": safe_rows,
        "row_count": len(safe_rows),
        "truncated": len(safe_rows) == limit,
        "elapsed_ms": elapsed_ms,
    }


def schema(cfg: dict[str, Any], target: str | None) -> dict[str, Any]:
    """No target: list tables in the connected database. With target: columns."""
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            if target is None:
                cur.execute(
                    "SELECT table_name AS name, table_type AS type, "
                    "table_rows AS rows_estimate "
                    "FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() "
                    "ORDER BY table_name"
                )
                return {
                    "tables": [
                        {
                            "name": row["name"],
                            "type": row["type"],
                            "rows_estimate": row["rows_estimate"],
                        }
                        for row in cur.fetchall()
                    ]
                }
            cur.execute(
                "SELECT column_name AS name, column_type AS type, "
                "is_nullable AS nullable, column_default AS `default`, "
                "column_key AS `key` "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s "
                "ORDER BY ordinal_position",
                (target,),
            )
            return {
                "columns": [
                    {
                        "name": row["name"],
                        "type": _json_safe(row["type"]),
                        "nullable": row["nullable"] == "YES",
                        "default": _json_safe(row["default"]),
                        "key": row["key"],
                    }
                    for row in cur.fetchall()
                ]
            }
    finally:
        conn.close()
