#!/usr/bin/env python3
"""Entry point for the db-mcp stdio server. Register with your harness as:

    claude mcp add --scope user db-mcp <venv-python> /path/to/db-mcp/run_mcp.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
