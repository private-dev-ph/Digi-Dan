from __future__ import annotations

import copy
import unittest

from api.knowledge.ports import VersioningError
from api.knowledge.versioning import KnowledgeService
from tests.test_stage3_versioning import (
    Embedder, PROFILE, PROBES, Registry, Vectors, candidate, candidate_with_chunks, make_chunk,
)


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


if __name__ == "__main__":
    unittest.main()
