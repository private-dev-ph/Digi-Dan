import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.knowledge.ports import VersioningError
from api.knowledge.production_rollout import ProductionRollout, create_production_rollout
from api.knowledge.versioning import KnowledgeService
from scripts.tests.test_knowledge_versioning import Embedder, PROBES, PROFILE, Registry, Vectors, candidate


class Verifier:
    def __init__(self):
        self.valid = True
        self.before_return = None

    def __call__(self, _):
        if self.before_return:
            self.before_return()
        if not self.valid:
            raise VersioningError("source_stale")
        return True


def setup_rollout():
    registry, vectors, verifier = Registry(), Vectors(), Verifier()
    service = KnowledgeService(registry, vectors, Embedder(), sleep=lambda _: None)
    rollout = ProductionRollout(service, PROFILE, verifier)
    return rollout, service, registry, vectors, verifier


class ProductionRolloutTests(unittest.TestCase):
    def stage(self, service, registry, text="Alpha project facts."):
        return service.sync(candidate(text), PROFILE,
                           expected_generation=registry.state["generation"],
                           verify_source=lambda _: True, probes=PROBES,
                           activate=False)

    def test_activates_exact_ticket_and_current_candidate(self):
        rollout, service, registry, _, _ = setup_rollout()
        staged = self.stage(service, registry)
        result = rollout.activate(staged["ticket"], candidate(),
                                  expected_generation=registry.state["generation"])
        self.assertEqual(result["ticket"], staged["ticket"])
        self.assertTrue(result["activation_performed"])
        self.assertEqual(result["physical_namespace"], "digi-dan-production-" + staged["namespace"])
        self.assertEqual(service.get_active()["ticket"], staged["ticket"])

    def test_candidate_provenance_and_profile_mismatch_fail_without_mutation(self):
        rollout, service, registry, _, _ = setup_rollout()
        staged = self.stage(service, registry)
        generation = registry.state["generation"]
        wrong_source = candidate(provenance={**candidate()["provenance"],
                                               "portfolio_content_hash": "f" * 64})
        for bad in (candidate("different reviewed content"), wrong_source):
            with self.assertRaises(VersioningError):
                rollout.activate(staged["ticket"], bad, expected_generation=generation)
        wrong_profile = ProductionRollout(service, {**PROFILE, "model": "other"}, lambda _: True)
        with self.assertRaises(VersioningError):
            wrong_profile.activate(staged["ticket"], candidate(), expected_generation=generation)
        self.assertEqual(registry.state["generation"], generation)
        self.assertIsNone(registry.state["active"])

    def test_stale_generation_and_cas_race_leave_active_unchanged(self):
        rollout, service, registry, vectors, verifier = setup_rollout()
        first = self.stage(service, registry)
        rollout.activate(first["ticket"], candidate(), expected_generation=1)
        second_candidate = candidate("Second current fact.")
        second = service.sync(second_candidate, PROFILE,
                              expected_generation=registry.state["generation"],
                              verify_source=lambda _: True, probes=PROBES, activate=False)
        active = registry.state["active"]
        queries = vectors.queries
        with self.assertRaises(VersioningError):
            rollout.activate(second["ticket"], second_candidate, expected_generation=1)
        self.assertEqual(vectors.queries, queries)
        def concurrent_update():
            state = registry.read()[1]
            state["generation"] += 1
            registry.compare_and_swap(registry.revision, state)
        verifier.before_return = concurrent_update
        expected = registry.state["generation"]
        with self.assertRaises(VersioningError) as caught:
            rollout.activate(second["ticket"], second_candidate, expected_generation=expected)
        self.assertEqual(caught.exception.code, "generation_conflict")
        self.assertEqual(registry.state["active"], active)

    def test_failed_probe_or_stale_source_does_not_replace_active(self):
        rollout, service, registry, vectors, verifier = setup_rollout()
        first = self.stage(service, registry)
        rollout.activate(first["ticket"], candidate(), expected_generation=1)
        second_candidate = candidate("Second current fact.")
        second = service.sync(second_candidate, PROFILE, expected_generation=registry.state["generation"],
                              verify_source=lambda _: True, probes=PROBES, activate=False)
        vectors.query = lambda *_: []
        with self.assertRaises(VersioningError):
            rollout.activate(second["ticket"], second_candidate, expected_generation=registry.state["generation"])
        vectors.query = lambda namespace, vector, top_k: [{**r, "score": 1.0} for r in vectors.scan(namespace)[:top_k]]
        verifier.valid = False
        with self.assertRaises(VersioningError):
            rollout.activate(second["ticket"], second_candidate, expected_generation=registry.state["generation"])
        self.assertEqual(registry.state["active"], first["ticket"])

    def test_rollback_requires_current_candidate_and_fresh_source(self):
        rollout, service, registry, _, verifier = setup_rollout()
        prior_candidate = candidate()
        prior = service.sync(prior_candidate, PROFILE, expected_generation=0,
                             verify_source=lambda _: True, probes=PROBES)
        newer_candidate = candidate("New reviewed fact.")
        newer = service.sync(newer_candidate, PROFILE, expected_generation=registry.state["generation"],
                             verify_source=lambda _: True, probes=PROBES)
        generation = registry.state["generation"]
        with self.assertRaises(VersioningError):
            rollout.rollback(prior["ticket"], candidate("Outdated content."), expected_generation=generation)
        verifier.valid = False
        with self.assertRaises(VersioningError):
            rollout.rollback(prior["ticket"], prior_candidate, expected_generation=generation)
        self.assertEqual(registry.state["active"], newer["ticket"])
        verifier.valid = True
        result = rollout.rollback(prior["ticket"], prior_candidate, expected_generation=generation)
        self.assertEqual(result["ticket"], prior["ticket"])
        self.assertEqual(service.get_active()["ticket"], prior["ticket"])

    def test_factory_is_production_only_and_vectors_are_read_only(self):
        settings = SimpleNamespace(knowledge_environment="production",
            upstash_redis_rest_url="https://redis.upstash.io", upstash_redis_rest_token="r",
            upstash_vector_rest_url="https://vector.upstash.io", upstash_vector_rest_token="v",
            gemini_api_key="g", embedding_dimensions=768,
            gemini_embedding_model="gemini-embedding-001")
        registry, vectors = SimpleNamespace(), SimpleNamespace(writable=True)
        cloud_module = SimpleNamespace(
            cloud_configuration=lambda value: {"environment": value.knowledge_environment,
                                               "embedding_profile": PROFILE},
            _create_cloud_stores=Mock(return_value=(registry, vectors)),
        )
        embeddings_module = SimpleNamespace(GeminiEmbedder=lambda *_args, **_kwargs: Embedder())
        with patch.dict(sys.modules, {
            "api.knowledge.cloud": cloud_module,
            "api.knowledge.cloud_embeddings": embeddings_module,
        }):
            rollout = create_production_rollout(settings, "https://zachcodes.vercel.app")
            with self.assertRaises(VersioningError):
                create_production_rollout(SimpleNamespace(**{**vars(settings),
                    "knowledge_environment": "test"}), "https://zachcodes.vercel.app")
        self.assertIsInstance(rollout, ProductionRollout)
        self.assertTrue(cloud_module._create_cloud_stores.call_args.kwargs["writable"])
        self.assertTrue(cloud_module._create_cloud_stores.call_args.kwargs["production_staging"])
        self.assertFalse(vectors.writable)
        self.assertEqual(rollout._service.registry, registry)


if __name__ == "__main__":
    unittest.main()
