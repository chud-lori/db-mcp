from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from time import perf_counter
from typing import Any

from . import EngineError
from .. import guard

# Session-level defense in depth; the SELECT-only DB user in config remains
# the real enforcement.
_SESSION_GUARDS = (
    "SET default_transaction_read_only = on",
    "SET statement_timeout = '30s'",
)


def _connect(cfg: dict[str, Any]):
    try:
        import pg8000
    except ImportError:
        raise EngineError("postgres driver missing — pip install pg8000") from None
    return pg8000.connect(
        host=cfg.get("host"),
        port=cfg.get("port", 5432),
        database=cfg.get("database"),
        user=cfg.get("user"),
        password=cfg.get("password"),
        timeout=10,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def query(cfg: dict[str, Any], query: str, target: str | None, limit: int) -> dict[str, Any]:
    # Validate before touching the network — a rejected statement must never
    # reach the server.
    sql = guard.ensure_readonly_sql(query)
    limit = guard.clamp_limit(limit)
    sql = guard.apply_sql_limit(sql, limit)
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        for stmt in _SESSION_GUARDS:
            cur.execute(stmt)
        start = perf_counter()
        cur.execute(sql)
        raw = cur.fetchall() if cur.description else []
        elapsed_ms = int((perf_counter() - start) * 1000)
        cols = [d[0] for d in cur.description or ()]
        rows = [{c: _json_safe(v) for c, v in zip(cols, r)} for r in raw]
        return {
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == limit,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        conn.close()


def schema(cfg: dict[str, Any], target: str | None) -> dict[str, Any]:
    conn = _connect(cfg)
    try:
        cur = conn.cursor()
        for stmt in _SESSION_GUARDS:
            cur.execute(stmt)
        if target is None:
            cur.execute(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
                "ORDER BY table_schema, table_name"
            )
            return {
                "tables": [
                    {"schema": s, "name": n, "type": t} for s, n, t in cur.fetchall()
                ]
            }
        # "schema.table" or bare table (default schema 'public'); parameterized
        # so a hostile target can't inject.
        schema_name, _, table_name = target.rpartition(".")
        schema_name = schema_name or "public"
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (schema_name, table_name),
        )
        return {
            "columns": [
                {
                    "name": name,
                    "type": dtype,
                    "nullable": nullable == "YES",
                    "default": _json_safe(default),
                }
                for name, dtype, nullable, default in cur.fetchall()
            ]
        }
    finally:
        conn.close()
