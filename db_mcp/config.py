from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "db-mcp" / "config.toml"

KNOWN_TYPES = ("postgres", "mysql", "mongo", "es")


class ConfigError(Exception):
    pass


def config_path() -> Path:
    override = os.environ.get("DB_MCP_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load_config() -> dict[str, dict[str, dict[str, Any]]]:
    """Load {db_name: {env: connection_cfg}} from the TOML config.

    Credentials live only in this file, outside any repo. A group-/world-
    readable config is refused outright rather than warned about — the file
    holds prod passwords.
    """
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"no config at {path} — copy config.example.toml there, add your "
            "read-only credentials, then: chmod 600 " + str(path)
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ConfigError(f"{path} is readable by group/others (mode {oct(mode)}); run: chmod 600 {path}")
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    config: dict[str, dict[str, dict[str, Any]]] = {}
    for db_name, envs in raw.items():
        if not isinstance(envs, dict):
            raise ConfigError(f"[{db_name}] must contain [{db_name}.<env>] tables")
        for env, cfg in envs.items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"[{db_name}.{env}] must be a table of connection settings")
            db_type = cfg.get("type")
            if db_type not in KNOWN_TYPES:
                raise ConfigError(f"[{db_name}.{env}] type must be one of {KNOWN_TYPES}, got {db_type!r}")
            config.setdefault(db_name, {})[env] = cfg
    return config


def resolve(config: dict[str, dict[str, dict[str, Any]]], db: str, env: str) -> dict[str, Any]:
    if db not in config:
        raise ConfigError(f"unknown db {db!r} — configured: {sorted(config)}")
    if env not in config[db]:
        raise ConfigError(f"db {db!r} has no env {env!r} — configured envs: {sorted(config[db])}")
    return config[db][env]


def describe(config: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Connection inventory with NO secrets — safe to show the model/user."""
    out = []
    for db_name in sorted(config):
        for env in sorted(config[db_name]):
            cfg = config[db_name][env]
            out.append(
                {
                    "db": db_name,
                    "env": env,
                    "type": cfg.get("type"),
                    "host": cfg.get("host") or cfg.get("url", "").split("@")[-1] or None,
                    "database": cfg.get("database"),
                }
            )
    return out
