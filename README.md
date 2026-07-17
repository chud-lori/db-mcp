# db-mcp

Read-only database access for AI agents, as a local stdio MCP server. One
uniform surface over **Postgres, MySQL, MongoDB, and Elasticsearch**, across
**multiple environments** (dev, staging, prod, …).

Companion to [agent-workbench](https://github.com/chud-lori/agent-workbench),
which deliberately holds no credentials and makes no network calls — anything
that needs either lives in a sidecar like this one.

## Read-only, three layers deep

1. **The DB user** (the real control): connect with dedicated `SELECT`-only
   users — grants below. Everything else is defense in depth.
2. **The session**: connections are set read-only where the store supports it
   (`default_transaction_read_only` on Postgres, `SET SESSION TRANSACTION READ
   ONLY` on MySQL) plus statement timeouts.
3. **The statement guard**: one statement per call; must start with
   SELECT/SHOW/DESCRIBE/EXPLAIN/WITH; write/DDL keywords rejected after
   comment/string stripping; row limits enforced (default 100, cap 1000).
   Mongo allows only `find`/`aggregate`/`count`/`distinct` and walks aggregate
   pipelines recursively to reject `$out`/`$merge` (even nested in
   `$lookup`/`$facet`); Elasticsearch only ever issues `_search`/`_mapping`/
   `_cat` requests with index names validated against path smuggling.

**Prod is a separate tool.** `db_query` covers non-prod envs (refuses
`env="prod"`); `db_query_prod` is its own tool name so your harness can
allowlist dev queries while prod keeps prompting for manual approval.

## Setup

```bash
cd ~/Projects/db-mcp
# Python >=3.11 with the drivers installed. This machine's convention: one
# shared pyenv virtualenv named "mcp" for all local MCP servers.
MCP_PY="$(pyenv root)/versions/mcp/bin/python3"
"$MCP_PY" -m pip install -r requirements.txt

mkdir -p ~/.config/db-mcp
cp config.example.toml ~/.config/db-mcp/config.toml
$EDITOR ~/.config/db-mcp/config.toml       # fill in read-only creds
chmod 600 ~/.config/db-mcp/config.toml     # the server refuses looser modes

# Claude Code
claude mcp add --scope user db-mcp "$MCP_PY" "$PWD/run_mcp.py"
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

## Read-only users (run as admin, once per DB)

```sql
-- Postgres
CREATE ROLE db_mcp_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE app TO db_mcp_ro;
GRANT USAGE ON SCHEMA public TO db_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO db_mcp_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO db_mcp_ro;

-- MySQL
CREATE USER 'db_mcp_ro'@'%' IDENTIFIED BY '...';
GRANT SELECT ON app.* TO 'db_mcp_ro'@'%';
```

```javascript
// MongoDB
use admin
db.createUser({user: "db_mcp_ro", pwd: "...", roles: [{role: "read", db: "events"}]})
```

```
# Elasticsearch: create a role with `read` + `view_index_metadata` on your
# index patterns and a user holding only that role (Kibana → Security, or
# POST /_security/role + /_security/user).
```

## Tests

```bash
python3 -m unittest discover -s tests -v   # no network, no drivers required
```
