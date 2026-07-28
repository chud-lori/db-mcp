"""Tests for the shared client-side deadline.

The regression it exists for: server-side caps bound execution only, so a
stalled call could hang a whole agent session (observed at 1800s) instead of
returning an error. Something must always fire.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp.engines import CONNECT_TIMEOUT_S, QUERY_TIMEOUT_S, Deadline, EngineError, QueryTimeout


class DeadlineTest(unittest.TestCase):
    def test_budget_left_does_not_raise(self):
        Deadline(30).check()  # no exception

    def test_expired_budget_raises_query_timeout(self):
        with self.assertRaises(QueryTimeout) as ctx:
            Deadline(0).check("mongo find")
        message = str(ctx.exception)
        self.assertIn("mongo find", message)
        self.assertIn("0s client-side deadline", message)

    def test_query_timeout_is_an_engine_error(self):
        # The server maps EngineError to a tool error; QueryTimeout must not
        # escape as an unhandled exception.
        self.assertTrue(issubclass(QueryTimeout, EngineError))

    def test_query_budget_exceeds_server_side_caps(self):
        # 30s statement_timeout / max_execution_time / maxTimeMS must win the
        # race, so their precise error reaches the caller instead of ours.
        self.assertGreater(QUERY_TIMEOUT_S, 30)

    def test_connect_budget_is_shorter_than_query_budget(self):
        self.assertLess(CONNECT_TIMEOUT_S, QUERY_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
