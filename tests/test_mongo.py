"""Tests for the mongo engine. No network, no pymongo — the driver is faked
via sys.modules so the read-only enforcement and coercion logic run for real."""

import datetime
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db_mcp.engines import (
    CONNECT_TIMEOUT_S,
    QUERY_TIMEOUT_S,
    Deadline,
    EngineError,
    QueryTimeout,
    mongo,
)
from db_mcp.guard import ReadOnlyViolation

CFG = {"type": "mongo", "uri": "mongodb://ro@localhost:27017/events", "database": "events"}


class FakeObjectId:
    def __init__(self, hex_str):
        self._hex = hex_str

    def __str__(self):
        return self._hex


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self.limit_arg = None
        self.sort_arg = None

    def sort(self, spec):
        self.sort_arg = spec
        return self

    def limit(self, n):
        self.limit_arg = n
        return self

    def __iter__(self):
        docs = self.docs if self.limit_arg is None else self.docs[: self.limit_arg]
        return iter(docs)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []
        self.calls = []

    def find(self, filter=None, projection=None, max_time_ms=None):
        self.max_time_ms = max_time_ms
        self.calls.append(("find", filter, projection))
        return FakeCursor(self.docs)

    def aggregate(self, pipeline, maxTimeMS=None):
        self.max_time_ms = maxTimeMS
        self.calls.append(("aggregate", pipeline))
        return iter(self.docs)

    def count_documents(self, filter, maxTimeMS=None):
        self.calls.append(("count_documents", filter))
        return 42

    def distinct(self, field, filter=None, maxTimeMS=None):
        self.calls.append(("distinct", field, filter))
        return ["a", "b", "c"]


class FakeDB:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        return self.collection

    def list_collection_names(self):
        return ["users", "events"]


class FakeClient:
    def __init__(self, collection):
        self.collection = collection
        self.closed = False

    def __getitem__(self, name):
        return FakeDB(self.collection)

    def close(self):
        self.closed = True


def fake_pymongo(client):
    module = types.ModuleType("pymongo")
    module.MongoClient = lambda *a, **k: client
    return module


class MongoQueryGuardTest(unittest.TestCase):
    """Read-only validation happens before the driver import — no mocking needed."""

    def test_write_ops_rejected(self):
        for op in ("insertOne", "delete", "mapReduce", "update", "drop"):
            with self.assertRaises(ReadOnlyViolation):
                mongo.query(CFG, '{"op": "%s"}' % op, "users", 10)

    def test_garbage_json_rejected(self):
        with self.assertRaises(ReadOnlyViolation) as ctx:
            mongo.query(CFG, "not json {", "users", 10)
        self.assertIn("JSON", str(ctx.exception))

    def test_non_object_json_rejected(self):
        with self.assertRaises(ReadOnlyViolation):
            mongo.query(CFG, '["find"]', "users", 10)

    def test_missing_target_rejected(self):
        with self.assertRaises(ReadOnlyViolation) as ctx:
            mongo.query(CFG, '{"op": "find"}', None, 10)
        self.assertIn("collection", str(ctx.exception))

    def test_out_at_top_level_rejected(self):
        q = '{"op": "aggregate", "pipeline": [{"$match": {}}, {"$out": "evil"}]}'
        with self.assertRaises(ReadOnlyViolation) as ctx:
            mongo.query(CFG, q, "users", 10)
        self.assertIn("$out", str(ctx.exception))

    def test_merge_nested_in_lookup_pipeline_rejected(self):
        # The key case: a naive top-level scan misses this.
        q = (
            '{"op": "aggregate", "pipeline": [{"$lookup": {"from": "other",'
            ' "pipeline": [{"$merge": {"into": "evil"}}], "as": "joined"}}]}'
        )
        with self.assertRaises(ReadOnlyViolation) as ctx:
            mongo.query(CFG, q, "users", 10)
        self.assertIn("$merge", str(ctx.exception))

    def test_merge_nested_in_facet_rejected(self):
        q = (
            '{"op": "aggregate", "pipeline": [{"$facet": {"branch":'
            ' [{"$sortByCount": "$x"}, {"$merge": "evil"}]}}]}'
        )
        with self.assertRaises(ReadOnlyViolation):
            mongo.query(CFG, q, "users", 10)

    def test_out_nested_in_unionwith_rejected(self):
        q = (
            '{"op": "aggregate", "pipeline": [{"$unionWith": {"coll": "other",'
            ' "pipeline": [{"$out": "evil"}]}}]}'
        )
        with self.assertRaises(ReadOnlyViolation):
            mongo.query(CFG, q, "users", 10)

    def test_pipeline_must_be_list(self):
        with self.assertRaises(ReadOnlyViolation):
            mongo.query(CFG, '{"op": "aggregate", "pipeline": {"$match": {}}}', "users", 10)


class MongoQueryExecutionTest(unittest.TestCase):
    def run_query(self, docs, query, limit=10):
        collection = FakeCollection(docs)
        client = FakeClient(collection)
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            result = mongo.query(CFG, query, "users", limit)
        return result, collection, client

    def test_server_side_time_cap_is_set(self):
        # gotcha: the client timeout alone doesn't stop the server grinding on
        # a runaway aggregation — every op must carry maxTimeMS.
        _, collection, _ = self.run_query([], '{"op": "find", "filter": {}}')
        self.assertEqual(collection.max_time_ms, 30000)
        _, collection, _ = self.run_query([], '{"op": "aggregate", "pipeline": [{"$match": {}}]}')
        self.assertEqual(collection.max_time_ms, 30000)

    def test_find_rows_coerced_json_safe(self):
        docs = [
            {
                "_id": FakeObjectId("64b0c0ffee"),
                "when": datetime.datetime(2026, 7, 17, 12, 30, 0),
                "blob": b"\x00\x01\x02",
                "nested": {"oid": FakeObjectId("deadbeef"), "vals": [b"xy"]},
            }
        ]
        result, collection, client = self.run_query(docs, '{"op": "find", "filter": {}}')
        row = result["rows"][0]
        self.assertEqual(row["_id"], "64b0c0ffee")
        self.assertEqual(row["when"], "2026-07-17T12:30:00")
        self.assertEqual(row["blob"], "<3 bytes>")
        self.assertEqual(row["nested"]["oid"], "deadbeef")
        self.assertEqual(row["nested"]["vals"], ["<2 bytes>"])
        self.assertEqual(result["row_count"], 1)
        self.assertFalse(result["truncated"])
        self.assertIn("elapsed_ms", result)
        self.assertTrue(client.closed)

    def test_find_applies_limit_and_truncated(self):
        docs = [{"n": i} for i in range(5)]
        result, _, _ = self.run_query(docs, '{"op": "find"}', limit=3)
        self.assertEqual(result["row_count"], 3)
        self.assertTrue(result["truncated"])

    def test_find_passes_filter_projection_sort(self):
        _, collection, _ = self.run_query(
            [], '{"op": "find", "filter": {"a": 1}, "projection": {"a": 1}, "sort": [["a", -1]]}'
        )
        self.assertEqual(collection.calls[0], ("find", {"a": 1}, {"a": 1}))

    def test_aggregate_appends_limit_as_final_stage(self):
        q = '{"op": "aggregate", "pipeline": [{"$match": {"a": 1}}, {"$group": {"_id": "$a"}}]}'
        _, collection, _ = self.run_query([{"_id": 1}], q, limit=7)
        name, pipeline = collection.calls[0]
        self.assertEqual(name, "aggregate")
        self.assertEqual(pipeline[-1], {"$limit": 7})
        self.assertEqual(pipeline[:-1], [{"$match": {"a": 1}}, {"$group": {"_id": "$a"}}])

    def test_count_shape(self):
        result, collection, _ = self.run_query([], '{"op": "count", "filter": {"x": 1}}')
        self.assertEqual(result["rows"], [{"count": 42}])
        self.assertEqual(collection.calls[0], ("count_documents", {"x": 1}))

    def test_distinct_shape_capped_at_limit(self):
        result, _, _ = self.run_query([], '{"op": "distinct", "field": "kind"}', limit=2)
        self.assertEqual(result["rows"], [{"values": ["a", "b"]}])

    def test_distinct_requires_field(self):
        with self.assertRaises(ReadOnlyViolation):
            self.run_query([], '{"op": "distinct"}')


class MongoSchemaTest(unittest.TestCase):
    def test_list_collections(self):
        client = FakeClient(FakeCollection())
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            result = mongo.schema(CFG, None)
        self.assertEqual(result["collections"], ["events", "users"])
        self.assertTrue(client.closed)

    def test_sampled_fields_union_with_types(self):
        docs = [{"a": 1, "b": "x"}, {"a": "two"}]
        client = FakeClient(FakeCollection(docs))
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            result = mongo.schema(CFG, "users")
        by_name = {f["name"]: f for f in result["fields"]}
        self.assertEqual(by_name["a"]["types"], ["int", "str"])
        self.assertEqual(by_name["a"]["seen_in"], 2)
        self.assertEqual(by_name["b"]["seen_in"], 1)


class MongoDriverMissingTest(unittest.TestCase):
    def test_missing_driver_raises_engine_error(self):
        # None in sys.modules forces ImportError even if pymongo were installed.
        with mock.patch.dict(sys.modules, {"pymongo": None}):
            with self.assertRaises(EngineError) as ctx:
                mongo.query(CFG, '{"op": "find"}', "users", 10)
        self.assertIn("pip install pymongo", str(ctx.exception))


class NetworkTimeout(Exception):
    """Stands in for pymongo's timeout family, which we do not import here."""


class TimingOutCollection(FakeCollection):
    def find(self, filter=None, projection=None, max_time_ms=None):
        raise NetworkTimeout("timed out")


def capturing_pymongo(client):
    """Fake pymongo that records the MongoClient keyword arguments."""
    module = types.ModuleType("pymongo")
    module.client_kwargs = {}

    def MongoClient(*args, **kwargs):
        module.client_kwargs = kwargs
        return client

    module.MongoClient = MongoClient
    return module


class MongoTimeoutTest(unittest.TestCase):
    """maxTimeMS bounds one server-side operation; nothing bounded the call."""

    def test_socket_and_connect_timeouts_are_set(self):
        module = capturing_pymongo(FakeClient(FakeCollection()))
        with mock.patch.dict(sys.modules, {"pymongo": module}):
            mongo.query(CFG, '{"op": "find"}', "users", 10)
        self.assertEqual(module.client_kwargs["socketTimeoutMS"], QUERY_TIMEOUT_S * 1000)
        self.assertEqual(module.client_kwargs["connectTimeoutMS"], CONNECT_TIMEOUT_S * 1000)

    def test_expired_budget_stops_a_dripping_cursor(self):
        # Each getMore restarts the server's clock, so a cursor returning one
        # small batch at a time outlives every server-side cap.
        client = FakeClient(FakeCollection([{"n": 1}, {"n": 2}]))
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            with mock.patch.object(mongo, "Deadline", lambda *a, **k: Deadline(0)):
                with self.assertRaises(QueryTimeout):
                    mongo.query(CFG, '{"op": "find"}', "users", 10)
        self.assertTrue(client.closed)

    def test_driver_timeout_reported_as_query_timeout(self):
        client = FakeClient(TimingOutCollection())
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            with self.assertRaises(QueryTimeout) as ctx:
                mongo.query(CFG, '{"op": "find", "filter": {"name": "x"}}', "users", 10)
        self.assertIn("client-side deadline", str(ctx.exception))
        self.assertNotIn("$toString", str(ctx.exception))  # no $oid in this filter

    def test_oid_filter_timeout_explains_the_workaround(self):
        # The repeat offender: {"$oid": ...} is never converted to an
        # ObjectId, so the match scans the collection and finds nothing.
        client = FakeClient(TimingOutCollection())
        query = '{"op": "find", "filter": {"package_id": {"$oid": "6a62c70db886846400e21b1e"}}}'
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            with self.assertRaises(QueryTimeout) as ctx:
                mongo.query(CFG, query, "users", 10)
        message = str(ctx.exception)
        self.assertIn("$oid", message)
        self.assertIn("$toString", message)

    def test_oid_rejected_by_the_server_also_explains_the_workaround(self):
        # The other shape of the same mistake: instead of scanning, the server
        # rejects it outright with "unknown operator: $oid".
        class Rejecting(FakeCollection):
            def find(self, filter=None, projection=None, max_time_ms=None):
                raise RuntimeError("unknown operator: $oid, full error: {'ok': 0.0}")

        client = FakeClient(Rejecting())
        query = '{"op": "find", "filter": {"package_id": {"$oid": "6a62c70db886846400e21b1e"}}}'
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            with self.assertRaises(EngineError) as ctx:
                mongo.query(CFG, query, "users", 10)
        message = str(ctx.exception)
        self.assertIn("unknown operator: $oid", message)  # original preserved
        self.assertIn("$toString", message)

    def test_non_timeout_driver_errors_are_not_relabelled(self):
        class Exploding(FakeCollection):
            def find(self, filter=None, projection=None, max_time_ms=None):
                raise RuntimeError("connection reset")

        client = FakeClient(Exploding())
        with mock.patch.dict(sys.modules, {"pymongo": fake_pymongo(client)}):
            with self.assertRaises(RuntimeError):
                mongo.query(CFG, '{"op": "find"}', "users", 10)


class OidOperandTest(unittest.TestCase):
    def test_detected_at_any_depth(self):
        self.assertTrue(mongo._has_oid_operand({"_id": {"$oid": "abc"}}))
        self.assertTrue(mongo._has_oid_operand({"pipeline": [{"$match": {"a": {"$oid": "x"}}}]}))
        self.assertTrue(mongo._has_oid_operand({"_id": {"$in": [{"$oid": "x"}]}}))

    def test_absent_when_no_oid(self):
        self.assertFalse(mongo._has_oid_operand({"phone": "+65", "n": [1, 2]}))


if __name__ == "__main__":
    unittest.main()
