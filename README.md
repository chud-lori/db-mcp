# db-mcp

Read-only database access for AI agents, as a local stdio MCP server. One
uniform surface over **Postgres, MySQL, MongoDB, and Elasticsearch**, across
**multiple environments** (dev, staging, prod, …).

Companion to [agent-workbench](https://github.com/chud-lori/agent-workbench),
which deliberately holds no credentials and makes no network calls — anything
that needs either lives in a sidecar like this one.

## Query-only, enforced in the server

Use whatever credentials you already have — the server itself guarantees
nothing but reads go through. It never creates users, never touches grants,
never runs DDL/DCL:

1. **The statement guard**: one statement per call; must start with
   SELECT/SHOW/DESCRIBE/EXPLAIN/WITH; every write/DDL/DCL keyword rejected
   after comment/string stripping; row limits enforced (default 100, cap
   1000). Mongo allows only `find`/`aggregate`/`count`/`distinct` and walks
   aggregate pipelines recursively to reject `$out`/`$merge` (even nested in
   `$lookup`/`$facet`); Elasticsearch only ever issues `_search`/`_mapping`/
   `_cat` requests with index names validated against path smuggling.
2. **The session**: connections are additionally set read-only where the
   store supports it (`default_transaction_read_only` on Postgres, `SET
   SESSION TRANSACTION READ ONLY` on MySQL) plus 30s server-side timeouts.

## Failures come back usable

A read-only server still wastes your time if a bad call hangs or a wrong
guess tells you nothing:

- **Every failure comes back as an answer.** Whatever a driver raises is
  returned as a tool error against the id of the request that caused it. A
  reply the caller cannot match to its request is, from its side, the same as
  no reply at all — the call just never returns.
- **Nothing runs unbounded.** Server-side caps (`statement_timeout`,
  `max_execution_time`, `maxTimeMS`) bound *execution*, not a stalled socket
  or a cursor dripping one batch at a time — each `getMore` restarts the
  server's clock. Every engine also carries socket timeouts and a 35s
  client-side deadline, so a runaway call returns an error instead of hanging
  the session.
- **A wrong column name comes back with the right ones.** `Unknown column
  'DATE_CREATE'` tells you the guess was wrong but not what to use, so the
  next attempt is another guess; SQL engines append the table's real columns
  (or the database's tables, for an unknown table) to the error.
- **A mongo timeout on an `{"$oid": …}` filter says so.** Extended JSON is not
  converted to an `ObjectId` here, so the match scans and finds nothing; the
  error names the `$toString`/`$expr` rewrite that works.

**Prod is a separate tool.** `db_query` covers non-prod envs (refuses
`env="prod"`); `db_query_prod` is its own tool name so your harness can
allowlist dev queries while prod keeps prompting for manual approval.

## Setup

```bash
git clone <this-repo> db-mcp && cd db-mcp
# Python >=3.11 plus the three drivers — any venv/interpreter you like.
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

mkdir -p ~/.config/db-mcp
cp config.example.toml ~/.config/db-mcp/config.toml
$EDITOR ~/.config/db-mcp/config.toml       # fill in read-only creds
chmod 600 ~/.config/db-mcp/config.toml     # the server refuses looser modes

# Claude Code (point it at whichever interpreter has the drivers)
claude mcp add --scope user db-mcp "$PWD/.venv/bin/python3" "$PWD/run_mcp.py"
```

Credentials live only in `~/.config/db-mcp/config.toml` — never in this repo,
never in the harness config, never in brain notes.

## Tools

| Tool | Use |
|---|---|
| `db_list` | configured databases (name, env, type, host — no secrets) |
| `db_query` | one read-only query on a non-prod env (default `dev`) |
| `db_query_prod` | same, prod only — separate tool so it can be permission-gated separately |
| `db_schema` | tables/collections/indices, or columns/mappings of one target |

Query shapes: SQL string for postgres/mysql; JSON `{"op": "find", "filter":
…}` for mongo (`target` = collection); JSON search body for es (`target` =
index).

## Tests

```bash
python3 -m unittest discover -s tests -v   # no network, no drivers required
```
