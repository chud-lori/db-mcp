"""Tests for the JSON-RPC layer.

The regression these exist for: a driver exception that escaped the tool
handler was answered as a protocol error with a null id. A client cannot
match that to the request it sent, so from its side the call simply never
returns — which is what an idle-timeout abort looks like from the outside.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp import server


def call_tool(name="db_list", args=None, request_id=7):
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }
    return server._handle_request(request)


class ToolErrorTest(unittest.TestCase):
    def test_unexpected_driver_exception_becomes_a_tool_error(self):
        boom = RuntimeError("unknown operator: $oid")
        with mock.patch.object(server, "_handle_tool", side_effect=boom):
            response = call_tool()
        self.assertEqual(response["id"], 7)
        self.assertNotIn("error", response)  # a result, not a protocol error
        self.assertTrue(response["result"]["isError"])
        self.assertIn("unknown operator: $oid", response["result"]["structuredContent"]["error"])

    def test_error_type_is_named_for_unexpected_exceptions(self):
        with mock.patch.object(server, "_handle_tool", side_effect=KeyError("database")):
            response = call_tool()
        self.assertIn("KeyError", response["result"]["structuredContent"]["error"])

    def test_known_errors_keep_their_bare_message(self):
        from db_mcp.guard import ReadOnlyViolation

        with mock.patch.object(server, "_handle_tool", side_effect=ReadOnlyViolation("no writes")):
            response = call_tool()
        self.assertEqual(response["result"]["structuredContent"]["error"], "no writes")

    def test_content_and_structured_content_agree(self):
        with mock.patch.object(server, "_handle_tool", side_effect=RuntimeError("boom")):
            response = call_tool()
        text = response["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), response["result"]["structuredContent"])


class RequestIdTest(unittest.TestCase):
    """Every answer must carry the id of the request it answers."""

    def run_line(self, line):
        out = []
        with mock.patch.object(sys, "stdin", iter([line])):
            with mock.patch.object(sys.stdout, "write", side_effect=out.append):
                with mock.patch.object(sys.stdout, "flush"):
                    server.main()
        return [json.loads(chunk) for chunk in out if chunk.strip()]

    def test_dispatch_failure_still_answers_the_request_id(self):
        line = json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {}}) + "\n"
        with mock.patch.object(server, "_handle_request", side_effect=RuntimeError("boom")):
            responses = self.run_line(line)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 11)
        self.assertEqual(responses[0]["error"]["code"], -32603)

    def test_unparseable_line_is_answered_without_an_id(self):
        responses = self.run_line("{not json\n")
        self.assertEqual(len(responses), 1)
        self.assertIsNone(responses[0]["id"])

    def test_notification_gets_no_response(self):
        line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        self.assertEqual(self.run_line(line), [])

    def test_unknown_method_answers_the_request_id(self):
        line = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "nope"}) + "\n"
        responses = self.run_line(line)
        self.assertEqual(responses[0]["id"], 3)
        self.assertEqual(responses[0]["error"]["code"], -32601)


class ProdSeparationTest(unittest.TestCase):
    def test_db_query_refuses_prod(self):
        result = server._handle_tool("db_query", {"db": "x", "query": "SELECT 1", "env": "prod"})
        self.assertIn("db_query_prod", result["error"])


if __name__ == "__main__":
    unittest.main()
