import copy
import tempfile
import unittest
from pathlib import Path

from api.knowledge.local_store import SQLiteRegistry
from api.knowledge.manifest import canonical_hash
from api.knowledge.ports import VersioningError
from api.knowledge.probe_cache import PROBE_CACHE_POLICY, make_probe_cache, read_probe_cache


class ProbeCacheReceiptTests(unittest.TestCase):
    def setUp(self):
        self.record = {"ticket": "ticket-1", "namespace": "knowledge-v1", "version_id": "a" * 64,
                       "candidate_hash": "b" * 64,
                       "embedding": {"provider": "test", "model": "m", "dimensions": 3, "policy_version": "p"}}
        self.probes = [{"question": "alpha?", "expected_ids": ["a", "b"], "top_k": 4}]
        self.vectors = [[1.0, 2, 3]]

    def receipt(self):
        return make_probe_cache(self.record, self.probes, self.vectors, 123.5)

    def test_roundtrip_and_defensive_copies(self):
        receipt = self.receipt()
        self.assertEqual(receipt["policy_version"], PROBE_CACHE_POLICY)
        self.record["probe_cache"] = receipt
        loaded = read_probe_cache(self.record, self.probes)
        self.assertEqual(loaded, receipt)
        loaded["vectors"][0][0] = 99
        self.assertEqual(self.record["probe_cache"]["vectors"][0][0], 1.0)
        self.vectors[0][0] = 88
        self.assertEqual(receipt["vectors"][0][0], 1.0)

    def test_sqlite_registry_json_roundtrip_preserves_receipt(self):
        receipt = self.receipt()
        record = {**self.record, "status": "verified", "probe_cache": receipt}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.sqlite"
            registry = SQLiteRegistry(path)
            revision, state = registry.read()
            state["versions"][record["ticket"]] = record
            self.assertTrue(registry.compare_and_swap(revision, state))
            restored = SQLiteRegistry(path).read()[1]["versions"][record["ticket"]]
        self.assertEqual(read_probe_cache(restored, self.probes), receipt)

    def test_missing_and_binding_changes_are_cache_misses(self):
        self.assertIsNone(read_probe_cache(self.record, self.probes))
        original = self.receipt()
        changes = [
            ("ticket", "ticket-2"), ("namespace", "knowledge-v2"), ("version_id", "c" * 64),
            ("candidate_hash", "d" * 64),
        ]
        for key, value in changes:
            record = copy.deepcopy(self.record)
            record["probe_cache"] = original
            record[key] = value
            self.assertIsNone(read_probe_cache(record, self.probes), key)
        record = copy.deepcopy(self.record)
        record["probe_cache"] = original
        record["embedding"]["model"] = "another"
        self.assertIsNone(read_probe_cache(record, self.probes))
        record = copy.deepcopy(self.record)
        record["probe_cache"] = original
        record["embedding"]["dimensions"] = 4
        self.assertIsNone(read_probe_cache(record, self.probes))
        changed = [{"question": "beta?", "expected_ids": ["a", "b"], "top_k": 4}]
        self.assertIsNone(read_probe_cache({**self.record, "probe_cache": original}, changed))
        for altered in (
            [{**self.probes[0], "expected_ids": ["a"]}],
            [{**self.probes[0], "top_k": 5}],
        ):
            self.assertIsNone(read_probe_cache({**self.record, "probe_cache": original}, altered))

    def test_policy_change_is_cache_miss(self):
        receipt = self.receipt()
        payload = {key: value for key, value in receipt.items() if key != "content_hash"}
        payload["policy_version"] = "future-policy"
        from api.knowledge.manifest import canonical_hash
        receipt = {**payload, "content_hash": canonical_hash(payload)}
        self.assertIsNone(read_probe_cache({**self.record, "probe_cache": receipt}, self.probes))

    def test_corruption_and_bad_inputs_rejected(self):
        receipt = self.receipt()
        tampered = copy.deepcopy(receipt)
        tampered["vectors"][0][0] = 9
        with self.assertRaises(VersioningError) as caught:
            read_probe_cache({**self.record, "probe_cache": tampered}, self.probes)
        self.assertEqual(caught.exception.code, "invalid_probe_cache")
        bad_cases = [
            (self.vectors[:-1], 1), ([[0, 0, 0]], 1), ([[1, float("nan"), 2]], 1),
            ([[1, 2]], 1), ([[1, 2, 3]], -1), ([[1, 2, 3]], float("inf")),
            ([[True, 2, 3]], 1), ([[1, 2, 3]], True),
        ]
        for vectors, timestamp in bad_cases:
            with self.subTest(vectors=vectors, timestamp=timestamp), self.assertRaises(VersioningError) as caught:
                make_probe_cache(self.record, self.probes, vectors, timestamp)
            self.assertEqual(caught.exception.code, "invalid_probe_cache")
        with self.assertRaises(VersioningError):
            make_probe_cache(self.record, self.probes * 33, self.vectors * 33, 1)
        oversized = copy.deepcopy(receipt)
        oversized["vectors"] = [[1.0] * 65536] * 4
        oversized["embedding_dimensions"] = 65536
        oversized["probe_count"] = 4
        with self.assertRaises(VersioningError):
            read_probe_cache({**self.record, "probe_cache": oversized}, self.probes)

    def test_malformed_receipt_hash_and_policy_rejected(self):
        receipt = self.receipt()
        for key, value in (("binding", "z" * 64), ("content_hash", "g" * 64),
                           ("policy_version", " "), ("policy_version", "p" * 129)):
            malformed = copy.deepcopy(receipt)
            malformed[key] = value
            if key != "content_hash":
                payload = {name: item for name, item in malformed.items() if name != "content_hash"}
                malformed["content_hash"] = canonical_hash(payload)
            with self.subTest(key=key, value=value), self.assertRaises(VersioningError) as caught:
                read_probe_cache({**self.record, "probe_cache": malformed}, self.probes)
            self.assertEqual(caught.exception.code, "invalid_probe_cache")

    def test_vector_norm_accepts_large_finite_values_and_invalid_record_is_sanitized(self):
        record = {**self.record, "embedding": {**self.record["embedding"], "dimensions": 1}}
        receipt = make_probe_cache(record, self.probes, [[1e200]], 0)
        self.assertEqual(read_probe_cache({**record, "probe_cache": receipt}, self.probes), receipt)
        with self.assertRaises(VersioningError) as caught:
            read_probe_cache({"probe_cache": receipt, "embedding": None}, self.probes)
        self.assertEqual(caught.exception.code, "invalid_probe_cache")


if __name__ == "__main__":
    unittest.main()
