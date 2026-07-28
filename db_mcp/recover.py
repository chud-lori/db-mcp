"""Turn a failed query into a self-correcting one.

A driver reports a guessed column as a bare string — "Unknown column
'DATE_CREATE' in 'field list'" — which tells the caller it was wrong but not
what the right names are, so the obvious next move is another guess. These
helpers spot that class of failure and append the real names to the error, so
the retry is informed instead of another round of trial and error.

Best effort by construction: the table extraction is a regex, not a parser,
and every lookup is optional. A hint that cannot be built is simply omitted —
the original driver error is never replaced or hidden.
"""

from __future__ import annotations

import re

# Identifier, optionally schema-qualified, optionally quoted per dialect.
_IDENT = r"""(?:[`"\[]?[\w$]+[`"\]]?\.)?[`"\[]?[\w$]+[`"\]]?"""

# Tables named by the statement. A subquery ("FROM (SELECT ...") does not
# match because "(" is outside the identifier class, which is what we want.
_TABLE_RE = re.compile(rf"\b(?:from|join)\s+({_IDENT})", re.IGNORECASE)

_UNKNOWN_COLUMN_RE = re.compile(
    r"""
      unknown\s+column\s+'([^']+)'            # mysql 1054
    | column\s+"([^"]+)"\s+does\s+not\s+exist  # postgres 42703
    """,
    re.IGNORECASE | re.VERBOSE,
)

_UNKNOWN_TABLE_RE = re.compile(
    r"""
      table\s+'([^']+)'\s+doesn't\s+exist       # mysql 1146
    | relation\s+"([^"]+)"\s+does\s+not\s+exist  # postgres 42P01
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bounds so a hint can never become the expensive part of a failed call.
_MAX_TABLES_INSPECTED = 5
_MAX_NAMES_LISTED = 200


_QUOTE_CHARS = re.compile(r"[`\"\[\]]")


def _unquote(name: str) -> str:
    """Strip dialect quoting, including around a schema qualifier."""
    return _QUOTE_CHARS.sub("", name)


def tables_in(sql: str) -> list[str]:
    """Best-effort list of tables a statement reads, in order, deduped."""
    seen: list[str] = []
    for match in _TABLE_RE.finditer(sql):
        name = _unquote(match.group(1))
        if name and name.lower() not in {t.lower() for t in seen}:
            seen.append(name)
    return seen


def _render(label: str, names: list[str]) -> str:
    shown = names[:_MAX_NAMES_LISTED]
    suffix = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    return f"{label}: {', '.join(shown)}{suffix}"


def sql_error_hint(message: str, sql: str, columns_of, table_names) -> str:
    """Build the hint to append to a failed SQL call; '' when there is none.

    `columns_of(table)` and `table_names()` are engine callbacks returning
    plain name lists. Both may raise or return nothing — the hint is dropped
    rather than allowed to mask the real error.
    """
    try:
        if _UNKNOWN_COLUMN_RE.search(message):
            lines = []
            for table in tables_in(sql)[:_MAX_TABLES_INSPECTED]:
                names = list(columns_of(table) or ())
                if names:
                    lines.append(_render(f"columns in {table}", names))
            if lines:
                return "\n" + "\n".join(lines)
            return ""

        if _UNKNOWN_TABLE_RE.search(message):
            names = list(table_names() or ())
            if names:
                return "\n" + _render("tables in this database", sorted(names))
    except Exception:
        # A broken hint must never replace a real error message.
        return ""
    return ""


def enrich_sql_error(exc: BaseException, sql: str, columns_of, table_names) -> str:
    """The driver's message, plus the names it should have been given."""
    message = str(exc)
    return message + sql_error_hint(message, sql, columns_of, table_names)
