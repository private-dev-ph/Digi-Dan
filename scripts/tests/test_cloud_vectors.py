import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from api.knowledge.cloud_vectors import UpstashVectorStore
from api.knowledge.ports import VersioningError


def item(ident="a", vector=None, data="hello", metadata=None, score=0.5):
    return SimpleNamespace(id=ident, vector=vector or [1.0, 0.0], data=data,
                           metadata={} if metadata is None else metadata, score=score)


class UpstashVectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.index = Mock()
        self.index.upsert.return_value = "Success"
        self.index.info.return_value = SimpleNamespace(
            dimension=2, namespaces={"digi-dan-test-knowledge-demo": SimpleNamespace(vector_count=1, pending_vector_count=0)})
        self.store = UpstashVectorStore(self.index, environment="test", dimensions=2,
                                        writable=True, page_size=2, max_records=4)

    def test_environment_namespace_mapping_and_rejection(self):
        self.store.inspect("knowledge-demo")
        self.index.info.assert_called_once()
        with self.assertRaises(VersioningError):
            self.store.inspect("knowledge-../secret")
        with self.assertRaises(VersioningError):
            UpstashVectorStore(self.index, environment="staging", dimensions=2)

    def test_inspect_maps_ready_and_pending(self):
        self.index.info.return_value.namespaces["digi-dan-test-knowledge-demo"].pending_vector_count = 3
        self.assertEqual(self.store.inspect("knowledge-demo"), {"ready": 1, "pending": 3})

    def test_dimension_mismatch_fails_before_write(self):
        self.index.info.return_value.dimension = 3
        with self.assertRaises(VersioningError) as error:
            self.store.put("knowledge-demo", [{"id": "a", "text": "x", "metadata": {}, "vector": [1, 0]}])
        self.assertEqual(error.exception.code, "dimension_mismatch")
        self.index.upsert.assert_not_called()

    def test_put_validates_vectors_before_sdk(self):
        for vector in ([1, float("nan")], [0, 0], [1], [True, 0], [1.7e308, 1.7e308]):
            with self.subTest(vector=vector), self.assertRaises(VersioningError):
                self.store.put("knowledge-demo", [{"id": "a", "text": "x", "metadata": {}, "vector": vector}])
        self.index.upsert.assert_not_called()

    def test_put_rejects_empty_text_and_oversized_batch(self):
        with self.assertRaises(VersioningError):
            self.store.put("knowledge-demo", [{"id": "a", "text": "", "metadata": {}, "vector": [1, 0]}])
        with self.assertRaises(VersioningError):
            self.store.put("knowledge-demo", [{"id": str(i), "text": "x", "metadata": {}, "vector": [1, 0]} for i in range(5)])
        self.index.upsert.assert_not_called()

    def test_upsert_requires_success_result(self):
        self.index.upsert.return_value = "not success"
        with self.assertRaises(VersioningError):
            self.store.put("knowledge-demo", [{"id": "a", "text": "hello", "metadata": {}, "vector": [1, 0]}])
        self.assertEqual(self.index.upsert.call_count, 1)

    def test_upsert_sdk_error_is_sanitized(self):
        self.index.upsert.side_effect = RuntimeError("token secret response body")
        with self.assertRaises(VersioningError) as error:
            self.store.put("knowledge-demo", [{"id": "a", "text": "hello", "metadata": {}, "vector": [1, 0]}])
        self.assertNotIn("secret", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_put_maps_text_to_data_and_namespace(self):
        self.store.put("knowledge-demo", [{"id": "a", "text": "hello", "metadata": {"x": 1}, "vector": [1, 0]}])
        self.index.upsert.assert_called_once_with(
            [{"id": "a", "vector": [1, 0], "metadata": {"x": 1}, "data": "hello"}],
            namespace="digi-dan-test-knowledge-demo")

    def test_production_namespace_is_isolated(self):
        production = UpstashVectorStore(self.index, environment="production", dimensions=2, writable=True)
        production.put("knowledge-demo", [{"id": "a", "text": "hello", "metadata": {}, "vector": [1, 0]}])
        self.assertEqual(self.index.upsert.call_args.kwargs["namespace"], "digi-dan-production-knowledge-demo")

    def test_readonly_denies_mutations(self):
        store = UpstashVectorStore(self.index, environment="test", dimensions=2)
        for call in (lambda: store.put("knowledge-demo", []), lambda: store.delete("knowledge-demo")):
            with self.assertRaises(VersioningError):
                call()
        self.index.info.assert_not_called()

    def test_delete_uses_delete_namespace_only(self):
        self.store.delete("knowledge-demo")
        self.index.delete_namespace.assert_called_once_with("digi-dan-test-knowledge-demo")
        self.index.reset.assert_not_called()

    def test_query_requests_full_records_and_maps_response(self):
        self.index.query.return_value = [item()]
        self.assertEqual(self.store.query("knowledge-demo", [1, 0], 2),
                         [{"id": "a", "text": "hello", "metadata": {}, "vector": [1.0, 0.0], "score": 0.5}])
        kwargs = self.index.query.call_args.kwargs
        self.assertTrue(kwargs["include_vectors"] and kwargs["include_metadata"] and kwargs["include_data"])
        self.assertEqual(kwargs["namespace"], "digi-dan-test-knowledge-demo")

    def test_query_rejects_malformed_results(self):
        for result in ([item(vector=[1], data="x")], [item(data=None)], [item(ident="")],
                       [item(score=float("inf"))], [item(), item()]):
            self.index.query.return_value = result
            with self.subTest(result=result), self.assertRaises(VersioningError):
                self.store.query("knowledge-demo", [1, 0], 2)

    def test_scan_paginates_and_requests_full_records(self):
        self.index.range.side_effect = [
            SimpleNamespace(next_cursor="c1", vectors=[item("a")]),
            SimpleNamespace(next_cursor="", vectors=[item("b")]),
        ]
        records = self.store.scan("knowledge-demo")
        self.assertEqual([r["id"] for r in records], ["a", "b"])
        self.assertEqual([c.kwargs["cursor"] for c in self.index.range.call_args_list], ["", "c1"])
        for call in self.index.range.call_args_list:
            self.assertTrue(call.kwargs["include_vectors"] and call.kwargs["include_metadata"] and call.kwargs["include_data"])

    def test_scan_rejects_repeated_cursor_and_duplicate_ids(self):
        self.index.range.side_effect = [SimpleNamespace(next_cursor="c1", vectors=[item("a")]),
                                        SimpleNamespace(next_cursor="c1", vectors=[])]
        with self.assertRaises(VersioningError):
            self.store.scan("knowledge-demo")
        self.index.range.side_effect = [SimpleNamespace(next_cursor="c1", vectors=[item("a")]),
                                        SimpleNamespace(next_cursor="", vectors=[item("a")])]
        with self.assertRaises(VersioningError):
            self.store.scan("knowledge-demo")

    def test_scan_enforces_record_limit(self):
        self.index.range.return_value = SimpleNamespace(next_cursor="", vectors=[item(str(i)) for i in range(5)])
        with self.assertRaises(VersioningError):
            self.store.scan("knowledge-demo")

    def test_scan_bounds_empty_pages_with_ever_changing_cursors(self):
        self.index.range.side_effect = lambda **kwargs: SimpleNamespace(
            next_cursor="cursor-{}".format(self.index.range.call_count), vectors=[])
        with self.assertRaises(VersioningError):
            self.store.scan("knowledge-demo")
        self.assertEqual(self.index.range.call_count, self.store.max_records + 1)

    def test_sdk_errors_are_sanitized(self):
        self.index.info.side_effect = RuntimeError("token secret response body")
        with self.assertRaises(VersioningError) as error:
            self.store.inspect("knowledge-demo")
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("body", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
