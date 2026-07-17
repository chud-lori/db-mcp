"""Elasticsearch engine (stdlib REST, no driver). Read-only query + schema.

ES has no session-level read-only mode, so enforcement is the endpoint
allowlist: this module only ever issues POST /{index}/_search and GETs to
/_cat/indices and /{index}/_mapping. The index name is validated so it can
never smuggle a different endpoint into the path (e.g. "logs/_delete_by_query").
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import EngineError
from ..guard import ReadOnlyViolation

# Legal ES index-name characters (incl. wildcards and comma lists). Anything
# else — notably "/" — could redirect the request to a write endpoint.
_INDEX_RE = re.compile(r"^[a-zA-Z0-9_.,*-]+$")

_TIMEOUT = 30


def _validate_index(index: str) -> str:
    if "/" in index or not _INDEX_RE.match(index):
        raise ReadOnlyViolation(
            f"invalid index name {index!r} — must match {_INDEX_RE.pattern} (no '/')"
        )
    return index


def _request(cfg: dict[str, Any], method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Issue one HTTP request to the cluster and return the parsed JSON."""
    url = cfg.get("url", "").rstrip("/") + path
    headers = {"Accept": "application/json"}
    username = cfg.get("username")
    if username is not None:
        token = base64.b64encode(f"{username}:{cfg.get('password', '')}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    context = None
    if not cfg.get("verify_tls", True):
        # Self-signed dev clusters only — opt-in via verify_tls = false.
        context = ssl._create_unverified_context()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT, context=context)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        reason = ""
        try:
            err = json.loads(raw.decode())
            reason = err.get("error", {}).get("reason") or json.dumps(err.get("error", err))
        except Exception:
            reason = raw.decode(errors="replace")
        raise EngineError(f"ES returned HTTP {exc.code}: {reason[:300]}") from None
    except urllib.error.URLError as exc:
        raise EngineError(
            f"cannot reach ES at {url}: {exc.reason} — check the url, network, and that the cluster is up"
        ) from None


def query(cfg: dict[str, Any], query: str, target: str | None, limit: int) -> dict[str, Any]:
    """POST a search body to /{index}/_search. `target` is the index name."""
    if not target:
        raise ReadOnlyViolation("es queries require target=<index name>")
    index = _validate_index(target)

    if not query or not query.strip():
        body: dict[str, Any] = {}  # match_all
    else:
        try:
            body = json.loads(query)
        except (ValueError, TypeError):
            raise ReadOnlyViolation("es query must be a JSON search body, e.g. {\"query\": {\"match_all\": {}}}") from None
    if not isinstance(body, dict):
        raise ReadOnlyViolation(f"es search body must be a JSON object, got {type(body).__name__}")

    # Respect a smaller size in the body, cap at limit.
    size = min(body.get("size", limit), limit)
    body["size"] = size

    start = time.monotonic()
    resp = _request(cfg, "POST", f"/{index}/_search", body)
    wall_ms = int((time.monotonic() - start) * 1000)

    hits = resp.get("hits", {}).get("hits", [])
    rows = [{"_id": h.get("_id"), "_score": h.get("_score"), **h.get("_source", {})} for h in hits]
    result: dict[str, Any] = {
        "rows": rows,
        "row_count": len(rows),
        "truncated": len(rows) == size,
        "elapsed_ms": resp.get("took", wall_ms),
    }
    if "aggs" in body or "aggregations" in body:
        result["aggregations"] = resp.get("aggregations")
    return result


def _flatten_properties(props: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a mapping's properties tree into {dot.path: type}."""
    out: dict[str, str] = {}
    for name, spec in props.items():
        path = f"{prefix}{name}"
        sub = spec.get("properties") if isinstance(spec, dict) else None
        if sub:
            # object/nested containers keep their own type when declared.
            if spec.get("type"):
                out[path] = spec["type"]
            out.update(_flatten_properties(sub, path + "."))
        else:
            out[path] = spec.get("type", "object") if isinstance(spec, dict) else "object"
    return out


def schema(cfg: dict[str, Any], target: str | None) -> dict[str, Any]:
    """No target: list indices via _cat. With target: flattened field mappings."""
    if target is None:
        rows = _request(cfg, "GET", "/_cat/indices?format=json&h=index,docs.count,store.size")
        return {
            "indices": [
                {
                    "index": row.get("index"),
                    "docs_count": row.get("docs.count"),
                    "store_size": row.get("store.size"),
                }
                for row in rows
            ]
        }

    index = _validate_index(target)
    resp = _request(cfg, "GET", f"/{index}/_mapping")
    mappings: dict[str, str] = {}
    for spec in resp.values():  # wildcard targets may match several indices
        props = spec.get("mappings", {}).get("properties", {})
        mappings.update(_flatten_properties(props))
    return {"mappings": mappings}
