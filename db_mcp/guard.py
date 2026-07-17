from __future__ import annotations

import re

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# First keyword of the (single) statement must be one of these. WITH is
# allowed because CTE pipelines are how analysts write selects; the write
# check below still rejects WITH ... INSERT/UPDATE/DELETE.
_READ_STARTERS = ("select", "show", "describe", "desc", "explain", "with")

# Rejected anywhere in the statement, word-bounded, after comment/string
# stripping. Belt over the braces: the DB user should be SELECT-only anyway.
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|replace|truncate|drop|alter|create|grant|revoke|"
    r"vacuum|analyze|reindex|cluster|refresh|copy|call|do|lock|rename|set|reset|"
    r"comment|listen|notify|load|handler|install|shutdown|kill|purge|flush|"
    r"outfile|dumpfile|into)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(Exception):
    pass


def _strip_sql_noise(sql: str) -> str:
    """Remove comments and string literals so keyword checks can't be fooled
    by e.g. WHERE name = 'drop table' or trailing -- comments."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            i = sql.find("\n", i)
            i = n if i == -1 else i
        elif two == "/*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in ("'", '"'):
            i += 1
            while i < n:
                if sql[i] == "\\":
                    i += 2
                elif sql[i] == ch:
                    if sql[i : i + 2] == ch * 2:  # escaped '' / ""
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
            out.append("''")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def ensure_readonly_sql(sql: str) -> str:
    """Validate that `sql` is one read-only statement. Returns the statement
    stripped of any trailing semicolon. Raises ReadOnlyViolation otherwise."""
    if not sql or not sql.strip():
        raise ReadOnlyViolation("empty query")
    cleaned = _strip_sql_noise(sql).strip()
    body = cleaned.rstrip("; \n\t")
    if ";" in body:
        raise ReadOnlyViolation("multiple statements are not allowed — send one SELECT at a time")
    first = (body.split(None, 1) or [""])[0].lower()
    if first not in _READ_STARTERS:
        raise ReadOnlyViolation(f"only read statements allowed ({', '.join(_READ_STARTERS)}); got {first!r}")
    hit = _WRITE_WORDS.search(body)
    if hit:
        raise ReadOnlyViolation(f"write/DDL keyword {hit.group(0)!r} is not allowed on a read-only connection")
    return sql.strip().rstrip(";").strip()


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not isinstance(limit, int) or limit < 1:
        raise ReadOnlyViolation(f"limit must be a positive integer, got {limit!r}")
    return min(limit, MAX_LIMIT)


def apply_sql_limit(sql: str, limit: int) -> str:
    """Append LIMIT if the statement doesn't already carry one; an existing
    LIMIT larger than the cap is rejected rather than silently rewritten."""
    stripped = _strip_sql_noise(sql)
    match = re.search(r"\blimit\s+(\d+)", stripped, re.IGNORECASE)
    if match:
        if int(match.group(1)) > MAX_LIMIT:
            raise ReadOnlyViolation(f"LIMIT {match.group(1)} exceeds the cap of {MAX_LIMIT}")
        return sql
    first = stripped.strip().split(None, 1)[0].lower()
    if first in ("show", "describe", "desc", "explain"):
        return sql
    return f"{sql} LIMIT {limit}"
