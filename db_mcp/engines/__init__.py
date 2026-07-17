"""Engine registry.

Every engine module implements the same two functions:

    query(cfg: dict, query: str, target: str | None, limit: int) -> dict
        Execute one read-only query. `target` is the collection (mongo) or
        index (es); SQL engines ignore it. Returns:
        {"rows": [...], "row_count": int, "truncated": bool, "elapsed_ms": int}
        Rows are JSON-safe dicts (engines coerce driver types: Decimal->str,
        datetime->isoformat, ObjectId->str, bytes->repr).

    schema(cfg: dict, target: str | None) -> dict
        With no target: list tables/collections/indices. With a target:
        columns/mappings/sampled fields for it.

Engines import their driver lazily inside functions and raise EngineError
with the pip package name when it is missing, so db_list and the other
engines work regardless. Engines must also enforce read-only at the session
level where the store supports it (postgres/mysql: read-only transaction
characteristics; mongo/es: operation whitelists) — the SELECT-only DB user
in config remains the real enforcement, this is defense in depth.
"""

from __future__ import annotations


class EngineError(Exception):
    pass


def get_engine(db_type: str):
    if db_type == "postgres":
        from . import postgres

        return postgres
    if db_type == "mysql":
        from . import mysql

        return mysql
    if db_type == "mongo":
        from . import mongo

        return mongo
    if db_type == "es":
        from . import es

        return es
    raise EngineError(f"unknown engine type {db_type!r}")
