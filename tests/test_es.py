"""Tests for the Elasticsearch engine — no network, urlopen is mocked."""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp.engines import EngineError, es
from db_mcp.guard import ReadOnlyViolation

CFG = {"type": "es", "url": "https://localhost:9200", "username": "u", "password": "p"}


def _resp(payload, status=200):
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode()
    mock.status = status
    return mock


def _search_payload(hits=(), took=7, aggregations=None):
    payload = {"took": took, "hits": {"hits": list(hits)}}
    if aggregations is not None:
        payload["aggregations"] = aggregations
    return payload


class TestQueryGuards(unittest.TestCase):
    def test_missing_target_raises(self):
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(ReadOnlyViolation):
                es.query(CFG, "{}", None, 100)
            with self.assertRaises(ReadOnlyViolation):
                es.query(CFG, "{}", "", 100)
            urlopen.assert_not_called()

    def test_path_smuggling_index_rejected_without_network(self):
        for bad in ("logs/_delete_by_query", "../_cluster", "a/b", "logs/_search"):
            with patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(ReadOnlyViolation, msg=bad):
                    es.query(CFG, "{}", bad, 100)
                urlopen.assert_not_called()

    def test_garbage_json_body_rejected(self):
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(ReadOnlyViolation):
                es.query(CFG, "not json {", "logs", 100)
            with self.assertRaises(ReadOnlyViolation):
                es.query(CFG, "[1, 2]", "logs", 100)  # object required
            urlopen.assert_not_called()


class TestQueryRequest(unittest.TestCase):
    def test_only_posts_to_search_endpoint(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload())) as urlopen:
            es.query(CFG, '{"query": {"match_all": {}}}', "logs", 100)
        req = urlopen.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/logs/_search"), req.full_url)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(urlopen.call_count, 1)

    def test_size_capped_at_limit(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload())) as urlopen:
            es.query(CFG, '{"size": 5000}', "logs", 100)
        body = json.loads(urlopen.call_args[0][0].data.decode())
        self.assertEqual(body["size"], 100)

    def test_smaller_existing_size_respected(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload())) as urlopen:
            es.query(CFG, '{"size": 5}', "logs", 100)
        body = json.loads(urlopen.call_args[0][0].data.decode())
        self.assertEqual(body["size"], 5)

    def test_size_defaults_to_limit(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload())) as urlopen:
            es.query(CFG, "{}", "logs", 50)
        body = json.loads(urlopen.call_args[0][0].data.decode())
        self.assertEqual(body["size"], 50)


class TestQueryResponse(unittest.TestCase):
    HITS = [
        {"_id": "1", "_score": 1.5, "_source": {"msg": "hello", "level": "info"}},
        {"_id": "2", "_score": 0.9, "_source": {"msg": "boom", "level": "error"}},
    ]

    def test_hits_map_to_flattened_rows(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload(self.HITS, took=12))):
            result = es.query(CFG, "{}", "logs", 100)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(
            result["rows"][0],
            {"_id": "1", "_score": 1.5, "msg": "hello", "level": "info"},
        )
        self.assertEqual(result["rows"][1]["msg"], "boom")
        self.assertEqual(result["elapsed_ms"], 12)
        self.assertFalse(result["truncated"])
        self.assertNotIn("aggregations", result)

    def test_truncated_when_rows_equal_size(self):
        with patch("urllib.request.urlopen", return_value=_resp(_search_payload(self.HITS))):
            result = es.query(CFG, '{"size": 2}', "logs", 100)
        self.assertTrue(result["truncated"])

    def test_aggregations_passed_through(self):
        aggs = {"levels": {"buckets": [{"key": "error", "doc_count": 3}]}}
        with patch(
            "urllib.request.urlopen",
            return_value=_resp(_search_payload(aggregations=aggs)),
        ):
            result = es.query(CFG, '{"aggs": {"levels": {"terms": {"field": "level"}}}}', "logs", 100)
        self.assertEqual(result["aggregations"], aggs)


class TestErrors(unittest.TestCase):
    def test_http_error_surfaces_status_and_reason(self):
        err_body = json.dumps({"error": {"reason": "failed to parse query"}}).encode()
        http_err = urllib.error.HTTPError(
            "https://localhost:9200/logs/_search", 400, "Bad Request", None, io.BytesIO(err_body)
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaises(EngineError) as ctx:
                es.query(CFG, "{}", "logs", 100)
        self.assertIn("400", str(ctx.exception))
        self.assertIn("failed to parse query", str(ctx.exception))

    def test_url_error_surfaces_url_and_hint(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            with self.assertRaises(EngineError) as ctx:
                es.query(CFG, "{}", "logs", 100)
        self.assertIn("https://localhost:9200", str(ctx.exception))
        self.assertIn("check the url", str(ctx.exception))


class TestSchema(unittest.TestCase):
    def test_list_indices(self):
        cat = [{"index": "logs", "docs.count": "42", "store.size": "1mb"}]
        with patch("urllib.request.urlopen", return_value=_resp(cat)) as urlopen:
            result = es.schema(CFG, None)
        req = urlopen.call_args[0][0]
        self.assertIn("/_cat/indices?format=json", req.full_url)
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(
            result["indices"],
            [{"index": "logs", "docs_count": "42", "store_size": "1mb"}],
        )

    def test_mapping_flattens_nested_properties_to_dot_paths(self):
        mapping = {
            "logs": {
                "mappings": {
                    "properties": {
                        "ts": {"type": "date"},
                        "user": {
                            "properties": {
                                "name": {"type": "keyword"},
                                "address": {"properties": {"city": {"type": "text"}}},
                            }
                        },
                        "tags": {"type": "nested", "properties": {"label": {"type": "keyword"}}},
                    }
                }
            }
        }
        with patch("urllib.request.urlopen", return_value=_resp(mapping)) as urlopen:
            result = es.schema(CFG, "logs")
        req = urlopen.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/logs/_mapping"))
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(
            result["mappings"],
            {
                "ts": "date",
                "user.name": "keyword",
                "user.address.city": "text",
                "tags": "nested",
                "tags.label": "keyword",
            },
        )

    def test_schema_target_validated(self):
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(ReadOnlyViolation):
                es.schema(CFG, "logs/_delete_by_query")
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
