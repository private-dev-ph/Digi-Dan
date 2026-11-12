"""Verification cache integrity and reuse."""
from __future__ import annotations
import copy
import unittest
from api.knowledge.ports import VersioningError
from api.knowledge.versioning import KnowledgeService
from scripts.tests.test_knowledge_versioning import Embedder, PROFILE, PROBES, Registry, Vectors, candidate, candidate_with_chunks, make_chunk
import tempfile
from pathlib import Path
from api.knowledge.local_store import SQLiteRegistry
from api.knowledge.manifest import canonical_hash
from api.knowledge.probe_cache import PROBE_CACHE_POLICY, make_probe_cache, read_probe_cache


class CountingEmbedder:
    def __init__(self):
        self.inputs = []

    def embed(self, texts, profile):
        self.inputs.extend(texts)
        return [[1.0, 2.0, 3.0] for _ in texts]


class ForbiddenEmbedder:
    def embed(self, *args):
        raise AssertionError("verified cached probes should avoid embedding")


class CachedProbeTests(unittest.TestCase):
    def setUp(self):
        self.registry, self.vectors = Registry(), Vectors()
        self.clock = lambda: 1000
        self.service = KnowledgeService(self.registry, self.vectors, Embedder(), clock=self.clock, sleep=lambda _: None)

    def sync(self, service=None, cand=None, probes=PROBES, **kw):
        service = service or self.service
        return service.sync(cand or candidate(), PROFILE,
                            expected_generation=self.registry.state["generation"],
                            verify_source=lambda _: True, probes=probes, **kw)

    def new_service(self, embedder):
        return KnowledgeService(self.registry, self.vectors, embedder, clock=self.clock, sleep=lambda _: None)

    def test_unchanged_sync_reuses_verified_probes_across_service_instances(self):
        first = self.sync()
        queries = self.vectors.queries
        counter = CountingEmbedder()
        service = self.new_service(counter)

        result = self.sync(service)

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(counter.inputs, [])
        self.assertGreater(self.vectors.queries, queries)
        self.assertEqual(result["probe_verification"]["mode"], "cached")
        self.assertEqual(result["probe_verification"]["probe_count"], 1)
        self.assertEqual(result["probe_verification"]["embedding_inputs"], 0)
        self.assertEqual(self.registry.state["active"], first["ticket"])

    def test_missing_cache_builds_fresh_and_seeds_cache(self):
        first = self.sync()
        self.registry.state["versions"][first["ticket"]].pop("probe_cache", None)
        counter = CountingEmbedder()
        service = self.new_service(counter)

        result = self.sync(service)

        self.assertEqual(result["status"], "unchanged")
        self.assertTrue(counter.inputs)
        self.assertEqual(result["probe_verification"]["mode"], "fresh")
        self.assertGreater(result["probe_verification"]["embedding_inputs"], 0)
        self.assertIn("probe_cache", self.registry.state["versions"][first["ticket"]])

    def test_question_expected_ids_and_top_k_changes_require_fresh_verification(self):
        self.sync()
        variants = [
            ([{"question": "alpha facts", "expected_ids": ["alpha"], "top_k": 1}], False),
            ([{"question": "alpha project", "expected_ids": ["alpha"], "top_k": 2}], False),
        ]
        for probes, should_fail in variants:
            with self.subTest(probes=probes):
                counter = CountingEmbedder()
                if should_fail:
                    with self.assertRaises(VersioningError):
                        self.sync(self.new_service(counter), probes=probes)
                else:
                    result = self.sync(self.new_service(counter), probes=probes)
                    self.assertEqual(result["probe_verification"]["mode"], "fresh")
                self.assertTrue(counter.inputs)

        two_chunks = candidate_with_chunks(make_chunk("alpha", "Alpha project facts."),
                                           make_chunk("beta", "Beta project facts."))
        self.sync(cand=two_chunks, probes=[{"question": "project facts", "expected_ids": ["alpha"], "top_k": 2}])
        counter = CountingEmbedder()
        result = self.sync(self.new_service(counter), cand=two_chunks,
                           probes=[{"question": "project facts", "expected_ids": ["beta"], "top_k": 2}])
        self.assertEqual(result["probe_verification"]["mode"], "fresh")
        self.assertTrue(counter.inputs)

    def test_corrupted_cache_fails_closed(self):
        first = self.sync()
        record = self.registry.state["versions"][first["ticket"]]
        record["probe_cache"] = {"corrupt": True}
        counter = CountingEmbedder()

        with self.assertRaises(VersioningError):
            self.sync(self.new_service(counter))

        self.assertEqual(counter.inputs, [])
        self.assertEqual(self.registry.state["active"], first["ticket"])

    def test_query_failure_does_not_fall_back_to_embedding_when_cache_is_valid(self):
        first = self.sync()
        counter = CountingEmbedder()
        self.vectors.query = lambda *args: (_ for _ in ()).throw(RuntimeError("storage unavailable"))

        with self.assertRaises(VersioningError):
            self.sync(self.new_service(counter))

        self.assertEqual(counter.inputs, [])
        self.assertEqual(self.registry.state["active"], first["ticket"])

    def test_source_failure_does_not_publish_a_new_cache(self):
        first = self.sync()
        record = self.registry.state["versions"][first["ticket"]]
        original_cache = copy.deepcopy(record.get("probe_cache"))
        with self.assertRaises(VersioningError):
            self.service.sync(candidate(), PROFILE, expected_generation=self.registry.state["generation"],
                              verify_source=lambda _: False, probes=[{"question": "new query", "expected_ids": ["alpha"], "top_k": 1}])
        self.assertEqual(self.registry.state["versions"][first["ticket"]].get("probe_cache"), original_cache)

    def test_generation_race_during_cached_probe_preserves_receipt(self):
        first = self.sync()
        original_cache = copy.deepcopy(self.registry.state["versions"][first["ticket"]]["probe_cache"])
        original_query = self.vectors.query
        raced = []

        def race_query(namespace, vector, top_k):
            results = original_query(namespace, vector, top_k)
            if not raced:
                revision, state = self.registry.read()
                state["generation"] += 1
                self.assertTrue(self.registry.compare_and_swap(revision, state))
                raced.append(True)
            return results

        self.vectors.query = race_query
        with self.assertRaises(VersioningError) as caught:
            self.sync(self.new_service(ForbiddenEmbedder()))

        self.assertEqual(caught.exception.code, "generation_conflict")
        self.assertEqual(self.registry.state["versions"][first["ticket"]]["probe_cache"], original_cache)
        self.assertEqual(self.registry.state["active"], first["ticket"])

    def test_content_change_and_activation_verify_probes_fresh(self):
        self.sync()
        counter = CountingEmbedder()
        changed = self.sync(self.new_service(counter), candidate("Updated Alpha facts."))
        self.assertEqual(changed["status"], "active")
        self.assertTrue(counter.inputs)

        staged_counter = CountingEmbedder()
        staged = self.sync(self.new_service(staged_counter), candidate("Staged Alpha facts."), activate=False)
        self.assertEqual(staged["status"], "staged")
        self.assertTrue(staged_counter.inputs)
        activation_counter = CountingEmbedder()
        activation_service = self.new_service(activation_counter)
        activation = activation_service.activate(staged["ticket"], verify_source=lambda _: True)
        self.assertEqual(activation["status"], "active")
        self.assertTrue(activation_counter.inputs)


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
