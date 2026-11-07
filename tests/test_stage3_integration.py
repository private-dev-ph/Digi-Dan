import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.core.config import Settings
from api.endpoint.application import create_app
from api.knowledge.local_store import LocalEmbedder, SQLiteRegistry, SQLiteVectorStore
from api.knowledge.manifest import canonical_hash
from api.knowledge.versioning import KnowledgeService
from api.query import rag


PROFILE = {"provider": "local-test", "model": "token-hash-v1", "dimensions": 128, "policy_version": "1"}
VERIFY_SOURCE = lambda _record: True
ORIGIN = {"origin": "https://zachcodes.vercel.app"}


def candidate(fact_id, text):
    metadata = {
        "section": "synthetic",
        "entity_id": fact_id,
        "source_url": "https://example.test/synthetic",
        "owner": "curated",
        "title": fact_id,
        "parent_id": fact_id,
        "chunk_index": 1,
        "chunk_count": 1,
    }
    full_text = f"{fact_id}\n{text}"
    chunk = {"id": fact_id, "text": full_text, "metadata": metadata}
    chunk["content_hash"] = canonical_hash(chunk)
    provenance = {
        "portfolio_source": {"site_url": "https://example.test"},
        "portfolio_content_hash": "a" * 64,
        "resume_source": {"url": "https://example.test/resume.pdf", "pdf_sha256": "b" * 64},
        "resume_extractor": {"name": "synthetic-test"},
        "resume_content_hash": "c" * 64,
        "legacy_content_hash": "d" * 64,
        "policy_hash": "e" * 64,
    }
    return {
        "schema_version": 1,
        "kind": "knowledge-candidate",
        "complete": True,
        "review_status": "policy-validated",
        "policy_version": "test-policy-v1",
        "content_hash": canonical_hash([{"id": chunk["id"], "text": chunk["text"], "metadata": chunk["metadata"]}]),
        "chunks": [chunk],
        "provenance": provenance,
    }


class Stage3IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.registry = SQLiteRegistry(root / "registry.sqlite")
        self.vectors = SQLiteVectorStore(root / "vectors.sqlite")
        self.embedder = LocalEmbedder()
        self.service = KnowledgeService(self.registry, self.vectors, self.embedder, sleep=lambda _: None)
        self.settings = Settings(knowledge_mode="versioned", chat_rate_limit=0)
        self.client = TestClient(create_app(settings=self.settings, knowledge_service=self.service))
        self.answer_patch = patch.object(rag, "generate_answer", return_value="mocked answer")
        self.answer_patch.start()

    def tearDown(self):
        self.answer_patch.stop()
        self.client.close()
        self.temp.cleanup()

    def stage(self, fact_id, text, expected_generation, activate=True):
        fact = candidate(fact_id, text)
        return self.service.sync(
            fact,
            PROFILE,
            expected_generation=expected_generation,
            verify_source=VERIFY_SOURCE,
            probes=[{"question": text, "expected_ids": [fact_id], "top_k": 1}],
            activate=activate,
        )

    def chat(self, question):
        return self.client.post("/api/chat", json={"message": question}, headers=ORIGIN)

    def test_chat_before_activation_returns_503(self):
        response = self.chat("orchid fact")
        self.assertEqual(response.status_code, 503)

    def test_activation_chat_returns_stored_fact_and_version(self):
        activation = self.stage("fact-alpha", "orchid builds reliable widgets", 0)
        response = self.chat("orchid widgets")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["knowledge_version"], activation["version_id"])
        self.assertEqual(body["sources"][0]["id"], "fact-alpha")
        self.assertIn("orchid builds reliable widgets", body["sources"][0]["text"])

    def test_chat_cannot_select_namespace(self):
        response = self.client.post("/api/chat", json={"message": "facts", "namespace": "knowledge-other"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 400)

    def test_rollback_restores_previous_version_facts(self):
        alpha = self.stage("fact-alpha", "orchid builds reliable widgets", 0)
        self.stage("fact-beta", "maple grows fragrant blossoms", 1)
        beta_chat = self.chat("maple blossoms")
        self.assertEqual(beta_chat.json()["knowledge_version"], self.service.get_active()["version_id"])
        result = self.service.rollback(alpha["ticket"], expected_generation=2, verify_source=VERIFY_SOURCE)
        response = self.chat("orchid widgets")
        self.assertEqual(result["version_id"], alpha["version_id"])
        self.assertEqual(response.json()["knowledge_version"], alpha["version_id"])
        self.assertEqual(response.json()["sources"][0]["id"], "fact-alpha")

    def test_chat_captures_active_namespace_before_activation_switch(self):
        alpha = self.stage("fact-alpha", "orchid builds reliable widgets", 0)
        beta = self.stage("fact-beta", "maple grows fragrant blossoms", 1, activate=False)

        class SwitchingEmbedder:
            switched = False

            def embed(inner, texts, profile):
                if texts == ["orchid widgets"] and not inner.switched:
                    inner.switched = True
                    self.service.activate(beta["ticket"], verify_source=VERIFY_SOURCE)
                return self.embedder.embed(texts, profile)

        self.service.embedder = SwitchingEmbedder()
        response = self.chat("orchid widgets")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.service.embedder.switched)
        self.assertEqual(self.service.get_active()["version_id"], beta["version_id"])
        self.assertEqual(response.json()["knowledge_version"], alpha["version_id"])
        self.assertEqual(response.json()["sources"][0]["id"], "fact-alpha")


if __name__ == "__main__":
    unittest.main()
