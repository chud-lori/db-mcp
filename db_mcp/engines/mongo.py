"""MongoDB engine: JSON query specs against a whitelist of read operations.

`query` is a JSON string:
    {"op": "find"|"aggregate"|"count"|"distinct",
     "filter": {...}, "pipeline": [...], "field": "...",
     "projection": {...}, "sort": [["field", 1], ...]}
`target` is the collection name (required for query).

Read-only enforcement is an op whitelist plus a recursive pipeline walk that
rejects $out/$merge anywhere (stages nest via $lookup/$unionWith/$facet).
The read-only DB user in config remains the real control — defense in depth.
"""

from __future__ import annotations

import datetime
import json
import time
from typing import Any

from . import EngineError
from ..guard import ReadOnlyViolation, clamp_limit

_READ_OPS = ("find", "aggregate", "count", "distinct")

# Server-side execution cap. The client timeout alone doesn't stop the server
# from grinding on a runaway aggregation after we hang up.
_MAX_TIME_MS = 30000

# Aggregation stages that write to a collection. Rejected anywhere in the
# pipeline, at any nesting depth.
_WRITE_STAGES = ("$out", "$merge")


def _connect(cfg: dict[str, Any]):
    """Lazy driver import + client. Raises EngineError when pymongo is absent."""
    try:
        from pymongo import MongoClient
    except ImportError:
        raise EngineError("mongo driver missing — pip install pymongo") from None
    kwargs: dict[str, Any] = {
        "serverSelectionTimeoutMS": 10000,
        "readPreference": "secondaryPreferred",
    }
    if cfg.get("uri"):
        return MongoClient(cfg["uri"], **kwargs)
    return MongoClient(
        host=cfg.get("host", "localhost"),
        port=int(cfg.get("port", 27017)),
        username=cfg.get("user"),
        password=cfg.get("password"),
        **kwargs,
    )


def _ensure_readonly_pipeline(node: Any) -> None:
    """Recursively reject $out/$merge — stages can hide inside $lookup.pipeline,
    $unionWith.pipeline, and $facet branches, so a top-level scan is not enough."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _WRITE_STAGES:
                raise ReadOnlyViolation(
                    f"aggregation stage {key!r} writes to a collection and is not "
                    "allowed on a read-only connection"
                )
            _ensure_readonly_pipeline(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _ensure_readonly_pipeline(item)


def _json_safe(value: Any) -> Any:
    """Coerce driver types for JSON: ObjectId/Decimal128 -> str,
    datetime -> isoformat, bytes -> placeholder; walk nested containers."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)  # ObjectId, Decimal128, and any other bson scalar


def _parse_spec(query: str, target: str | None) -> dict[str, Any]:
    """Validate the JSON spec and read-only rules BEFORE touching the driver."""
    if not target:
        raise ReadOnlyViolation("mongo queries need a collection — pass target=<collection>")
    try:
        spec = json.loads(query)
    except (TypeError, ValueError) as exc:
        raise ReadOnlyViolation(
            f'mongo query must be a JSON object like {{"op": "find", "filter": {{...}}}}: {exc}'
        ) from None
    if not isinstance(spec, dict):
        raise ReadOnlyViolation(f"mongo query must be a JSON object, got {type(spec).__name__}")
    op = spec.get("op")
    if op not in _READ_OPS:
        raise ReadOnlyViolation(f"only read ops allowed ({', '.join(_READ_OPS)}); got {op!r}")
    if op == "aggregate":
        pipeline = spec.get("pipeline")
        if not isinstance(pipeline, list):
            raise ReadOnlyViolation("aggregate requires a 'pipeline' list")
        _ensure_readonly_pipeline(pipeline)
    return spec


def query(cfg: dict[str, Any], query: str, target: str | None, limit: int) -> dict[str, Any]:
    spec = _parse_spec(query, target)
    limit = clamp_limit(limit)
    op = spec["op"]
    filt = spec.get("filter") or {}

    start = time.perf_counter()
    client = _connect(cfg)
    try:
        coll = client[cfg["database"]][target]
        if op == "find":
            cursor = coll.find(filt, spec.get("projection") or None, max_time_ms=_MAX_TIME_MS)
            sort = spec.get("sort")
            if sort:
                cursor = cursor.sort([(field, int(direction)) for field, direction in sort])
            rows = [_json_safe(doc) for doc in cursor.limit(limit)]
        elif op == "aggregate":
            # Cap output with a final $limit; the pipeline was walked above.
            pipeline = list(spec["pipeline"]) + [{"$limit": limit}]
            rows = [_json_safe(doc) for doc in coll.aggregate(pipeline, maxTimeMS=_MAX_TIME_MS)]
        elif op == "count":
            rows = [{"count": coll.count_documents(filt, maxTimeMS=_MAX_TIME_MS)}]
        else:  # distinct
            field = spec.get("field")
            if not field:
                raise ReadOnlyViolation("distinct requires a 'field'")
            values = list(coll.distinct(field, filt, maxTimeMS=_MAX_TIME_MS))
            rows = [{"values": _json_safe(values[:limit])}]
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return {
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == limit,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        client.close()


def schema(cfg: dict[str, Any], target: str | None) -> dict[str, Any]:
    client = _connect(cfg)
    try:
        db = client[cfg["database"]]
        if not target:
            return {"collections": sorted(db.list_collection_names())}
        # Sample docs and report the union of fields with observed types.
        fields: dict[str, dict[str, Any]] = {}
        for doc in db[target].find().limit(50):
            for name, value in doc.items():
                info = fields.setdefault(name, {"types": set(), "seen_in": 0})
                info["types"].add(type(value).__name__)
                info["seen_in"] += 1
        return {
            "fields": [
                {"name": name, "types": sorted(info["types"]), "seen_in": info["seen_in"]}
                for name, info in sorted(fields.items())
            ]
        }
    finally:
        client.close()
