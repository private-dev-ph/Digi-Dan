import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.core.config import Settings
from api.knowledge import cloud
from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.cloud_vectors import UpstashVectorStore
from api.knowledge.ports import VersioningError, initial_state
from api.knowledge.production_staging import ProductionStager, create_production_stager
from api.knowledge.versioning import KnowledgeService
from tests.test_stage3_cloud_registry import MockRedis, registry
from tests.test_stage3_versioning import candidate, PROBES


class SimulatedIndex:
    def __init__(self):
        self.rows = {}
        self.fail_upload = False
        self.move_source = None

    def info(self):
        return SimpleNamespace(dimension=768, namespaces={
            key: SimpleNamespace(vector_count=len(rows), pending_vector_count=0)
            for key, rows in self.rows.items()})

    def upsert(self, rows, namespace):
        if self.move_source:
            self.move_source()
        if self.fail_upload:
            raise RuntimeError('upload failed')
        self.rows.setdefault(namespace, {}).update({r['id']: copy.deepcopy(r) for r in rows})
        return 'Success'

    def range(self, *, namespace, cursor, **_):
        return SimpleNamespace(next_cursor='', vectors=[SimpleNamespace(**copy.deepcopy(r))
                             for r in self.rows.get(namespace, {}).values()])

    def query(self, *, namespace, top_k, **_):
        return [SimpleNamespace(**copy.deepcopy(r), score=1.0)
                for r in list(self.rows.get(namespace, {}).values())[:top_k]]


class MutableVerifier:
    def __init__(self):
        self.current = True
        self.calls = 0

    def verify(self, record):
        self.calls += 1
        if not self.current:
            raise VersioningError('source_stale')
        return {"verified": True}

    def __call__(self, record):
        self.verify(record)
        return True


def make_stager(verifier=None, *, active=None, fail_upload=False):
    redis = MockRedis()
    registry_store = registry(redis)
    if active:
        state = initial_state()
        state['generation'] = 1
        state['active'] = active
        state['versions'][active] = {"ticket": active, "version_id": "a" * 64,
            "namespace": "knowledge-old", "candidate_hash": "b" * 64,
            "embedding": {"provider": "google", "model": "gemini-embedding-001",
                "dimensions": 768, "policy_version": "knowledge-embedding-v1"},
            "expected_ids": ["old"], "expected_count": 1, "expected_hashes": {"old": "c" * 64},
            "status": "verified"}
        registry_store.compare_and_swap(0, state)
    index = SimulatedIndex()
    index.fail_upload = fail_upload
    vectors = UpstashVectorStore(index, environment='production', dimensions=768, writable=True)
    models = SimpleNamespace(embed_content=lambda **kwargs: SimpleNamespace(embeddings=[
        SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in kwargs['contents']]))
    service = KnowledgeService(registry_store, vectors,
        GeminiEmbedder(client=SimpleNamespace(models=models)), poll_attempts=1)
    verifier = verifier or MutableVerifier()
    profile = gemini_profile()
    stager = ProductionStager(service, profile, verifier)
    return stager, registry_store, index, verifier


class ProductionStagingTests(unittest.TestCase):
    def test_rejects_invalid_generation_and_stale_initial_source_without_writes(self):
        stager, store, index, verifier = make_stager()
        with self.assertRaises(VersioningError):
            stager.stage(candidate(), expected_generation=True, probes=PROBES)
        self.assertEqual(store.read()[1]['generation'], 0)
        self.assertEqual(index.rows, {})
        verifier.current = False
        with self.assertRaises(VersioningError):
            stager.stage(candidate(), expected_generation=0, probes=PROBES)
        self.assertEqual(store.read()[1]['generation'], 0)
        self.assertEqual(index.rows, {})

    def test_stages_record_and_probes_without_changing_active(self):
        stager, store, _, _ = make_stager(active='old-ticket')
        result = stager.stage(candidate(), expected_generation=1, probes=PROBES)
        state = store.read()[1]
        self.assertEqual(state['active'], 'old-ticket')
        self.assertEqual(result['status'], 'staged')
        self.assertEqual(result['chunk_count'], len(candidate()['chunks']))
        self.assertEqual(result['probe_count'], len(PROBES))
        self.assertEqual(result['candidate_hash'], candidate()['content_hash'])
        self.assertTrue(result['physical_namespace'].startswith('digi-dan-production-knowledge-'))
        self.assertFalse(result['activation_performed'])
        self.assertEqual(state['versions'][result['ticket']]['status'], 'verified')

    def test_source_moving_during_build_cannot_report_success_or_activate(self):
        verifier = MutableVerifier()
        stager, store, index, _ = make_stager(verifier=verifier, active='old-ticket')
        index.move_source = lambda: setattr(verifier, 'current', False)
        with self.assertRaises(VersioningError):
            stager.stage(candidate(), expected_generation=1, probes=PROBES)
        state = store.read()[1]
        self.assertEqual(state['active'], 'old-ticket')
        # Final source validation happens after the service has committed the
        # verified inactive record; it must never activate that record.
        self.assertEqual(next(r for r in state['versions'].values() if r['ticket'] != 'old-ticket')['status'], 'verified')

    def test_failed_build_preserves_old_active(self):
        stager, store, _, _ = make_stager(active='old-ticket', fail_upload=True)
        with self.assertRaises(VersioningError):
            stager.stage(candidate(), expected_generation=1, probes=PROBES)
        self.assertEqual(store.read()[1]['active'], 'old-ticket')

    def test_identical_candidate_is_reported_unchanged_without_replacing_active(self):
        stager, store, _, verifier = make_stager()
        first = stager.stage(candidate(), expected_generation=0, probes=PROBES)
        stager._service.activate(first['ticket'], verify_source=verifier)
        result = stager.stage(candidate(), expected_generation=1, probes=PROBES)
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['ticket'], first['ticket'])
        self.assertEqual(store.read()[1]['active'], first['ticket'])
        self.assertFalse(result['activation_performed'])

    def test_factory_only_enables_production_and_preserves_default_guard(self):
        settings = Settings(knowledge_environment='production',
            upstash_redis_rest_url='https://redis.upstash.io', upstash_redis_rest_token='r',
            upstash_vector_rest_url='https://vector.upstash.io', upstash_vector_rest_token='v',
            gemini_api_key='g')
        index = SimpleNamespace(_client=SimpleNamespace(timeout=None))
        with patch.object(cloud, 'Index', return_value=index), \
             patch.object(cloud, 'UpstashRedisRegistry') as registry_ctor, \
             patch.object(cloud, 'UpstashVectorStore') as vector_ctor:
            stager = cloud.create_production_stager(settings, site_url='https://zachcodes.vercel.app')
        self.assertIsInstance(stager, ProductionStager)
        self.assertTrue(registry_ctor.call_args.kwargs['writable'])
        self.assertTrue(vector_ctor.call_args.kwargs['writable'])
        self.assertEqual(stager._service.embedder._max_attempts, 3)
        self.assertEqual(stager._service.poll_attempts, 12)
        self.assertEqual(stager._service.poll_interval, 1.0)
        with self.assertRaises(VersioningError):
            create_production_stager(settings.model_copy(update={'knowledge_environment': 'test'}),
                                     site_url='https://zachcodes.vercel.app')
        with self.assertRaises(VersioningError) as caught:
            cloud.create_cloud_stores(settings, writable=True)
        self.assertEqual(caught.exception.code, 'production_writes_not_enabled')


if __name__ == '__main__':
    unittest.main()
