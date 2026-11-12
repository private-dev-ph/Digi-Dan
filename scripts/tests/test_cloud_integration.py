"""Run the real coordinator through cloud adapters with simulated services."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import Document
from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.cloud_vectors import UpstashVectorStore
from api.knowledge.ports import VersioningError
from api.knowledge.versioning import KnowledgeService
from api.tools import vector_store
from scripts.tests.test_cloud_registry import MockRedis, registry
from scripts.tests.test_knowledge_versioning import candidate, PROBES


class SimulatedIndex:
    def __init__(self):
        self.rows = {}
        self.fail_upload = False

    def info(self):
        return SimpleNamespace(dimension=768, namespaces={
            name: SimpleNamespace(vector_count=len(rows), pending_vector_count=0)
            for name, rows in self.rows.items()})

    def upsert(self, rows, namespace):
        if self.fail_upload:
            raise RuntimeError('upstream response with secret-token')
        self.rows.setdefault(namespace, {}).update({row['id']: copy.deepcopy(row) for row in rows})
        return 'Success'

    def range(self, *, namespace, cursor, **_):
        assert cursor == ''
        return SimpleNamespace(next_cursor='', vectors=[SimpleNamespace(**row)
                               for row in self.rows.get(namespace, {}).values()])

    def query(self, *, namespace, top_k, **_):
        return [SimpleNamespace(**row, score=1.0)
                for row in list(self.rows.get(namespace, {}).values())[:top_k]]

    def delete_namespace(self, namespace):
        self.rows.pop(namespace, None)


class CloudContractTests(unittest.TestCase):
    def test_stage_activate_retrieve_and_failed_successor(self):
        redis = MockRedis()
        state = registry(redis)
        index = SimulatedIndex()
        vectors = UpstashVectorStore(index, environment='test', dimensions=768, writable=True)
        models = SimpleNamespace(embed_content=lambda **kwargs: SimpleNamespace(
            embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767) for _ in kwargs['contents']]))
        embedder = GeminiEmbedder(client=SimpleNamespace(models=models))
        service = KnowledgeService(state, vectors, embedder, poll_attempts=1)
        try:
            result = service.sync(candidate(), gemini_profile(), expected_generation=0,
                                  verify_source=lambda _: True, probes=PROBES, activate=False)
            self.assertEqual(result['status'], 'staged')
            self.assertIsNone(state.read()[1]['active'])
            service.activate(result['ticket'], verify_source=lambda _: True)
            version, rows = service.retrieve('alpha project', 1)
            self.assertEqual(version['ticket'], result['ticket'])
            self.assertEqual(rows[0]['id'], 'alpha')
            self.assertTrue(all(name.startswith('digi-dan-test-knowledge-') for name in index.rows))
            index.fail_upload = True
            with self.assertRaises(VersioningError):
                service.sync(candidate('Updated alpha facts.'), gemini_profile(), expected_generation=1,
                             verify_source=lambda _: True, probes=PROBES)
            self.assertEqual(state.read()[1]['active'], result['ticket'])
        finally:
            state._client.close()

    def test_legacy_ingestion_rejects_physical_cloud_namespaces(self):
        for name in ('digi-dan-test-knowledge-abc', 'digi-dan-production-knowledge-abc'):
            with self.subTest(name=name), patch.object(vector_store, 'embed_texts') as embed:
                with self.assertRaises(HTTPException) as caught:
                    vector_store.upsert_documents([Document(text='overwrite')], name, Settings())
                self.assertEqual(caught.exception.status_code, 409)
                embed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
