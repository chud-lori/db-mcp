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

Every engine must also bound wall-clock time client-side (see QUERY_TIMEOUT_S):
server-side caps bound execution, not a stalled socket or a slow-dripping
cursor, and an unbounded call hangs the whole agent session rather than
returning an error the caller can act on.
"""

from __future__ import annotations

import time

# Time budget for one call. Deliberately a few seconds above the 30s
# server-side caps (statement_timeout / max_execution_time / maxTimeMS) so a
# store that can name its own timeout wins the race and reports a precise
# error; this is only the backstop for when nothing server-side fires.
QUERY_TIMEOUT_S = 35

# Establishing a connection is not the same budget as running a query: a host
# that is down should fail fast rather than eat the whole query budget.
CONNECT_TIMEOUT_S = 10


class EngineError(Exception):
    pass


class QueryTimeout(EngineError):
    """Raised when a call outlives QUERY_TIMEOUT_S client-side.

    Server-side caps only bound *execution*. A stalled socket, a driver
    spinning on a value it cannot translate, or a cursor that returns one
    small batch at a time (each batch resetting the server's own clock) all
    slip past them, and the call then hangs until something upstream gives
    up — historically 1800s of dead session. This always fires.
    """


class Deadline:
    """Wall-clock budget for one engine call, checked at points we control."""

    def __init__(self, seconds: float = QUERY_TIMEOUT_S) -> None:
        self.seconds = seconds
        self._expires_at = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    def check(self, doing: str = "query") -> None:
        """Raise QueryTimeout if the budget is gone. Safe to call in a loop."""
        if self.expired():
            raise QueryTimeout(
                f"{doing} exceeded the {self.seconds:g}s client-side deadline and was abandoned — "
                "narrow the filter, add a limit, or query an indexed field"
            )


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
