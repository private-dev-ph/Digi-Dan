import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import ChatRequest, Document, IngestRequest
from api.knowledge.ports import VersioningError
from api.query import rag
from api.tools import upload_vectorstore, vector_store


class Stage3ChatTests(unittest.TestCase):
    def test_legacy_default_works(self):
        with patch.object(rag, "query_sources", return_value=[]), patch.object(rag, "generate_answer", return_value="hello"):
            response = rag.answer_question(ChatRequest(message="hi"), Settings())
        self.assertEqual(response.answer, "hello")
        self.assertIsNone(response.knowledge_version)

    def test_namespace_override_rejected_in_both_modes(self):
        for mode in ("legacy", "versioned"):
            with self.subTest(mode=mode), self.assertRaises(HTTPException) as raised:
                rag.answer_question(ChatRequest(message="hi", namespace="anything"), Settings(knowledge_mode=mode))
            self.assertEqual(raised.exception.status_code, 400)

    def test_versioned_uses_injected_service_only_and_returns_version(self):
        calls = []

        class Service:
            def retrieve(self, question, top_k):
                calls.append((question, top_k))
                return ({"version_id": "v-7"}, [{"id": "a", "text": "context", "metadata": {"x": 1}, "score": 0.8}])

        with patch.object(rag, "query_sources", side_effect=AssertionError("legacy query used")), patch.object(rag, "generate_answer", return_value="answer"):
            result = rag.answer_question(ChatRequest(message="hi"), Settings(knowledge_mode="versioned"), knowledge_service=Service())
        self.assertEqual(calls, [("hi", 4)])
        self.assertEqual(result.knowledge_version, "v-7")
        self.assertEqual(result.sources[0].id, "a")

    def test_versioned_without_service_fails_closed(self):
        with patch.object(rag, "query_sources", side_effect=AssertionError("legacy fallback used")), self.assertRaises(HTTPException) as raised:
            rag.answer_question(ChatRequest(message="hi"), Settings(knowledge_mode="versioned"))
        self.assertEqual(raised.exception.status_code, 503)

    def test_versioning_error_becomes_safe_503(self):
        class Service:
            def retrieve(self, question, top_k):
                raise VersioningError("no_active_version", "internal details")

        with self.assertRaises(HTTPException) as raised:
            rag.answer_question(ChatRequest(message="hi"), Settings(knowledge_mode="versioned"), knowledge_service=Service())
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("internal details", raised.exception.detail)

    def test_version_is_resolved_once_during_retrieval(self):
        active = ["old"]

        class Service:
            def retrieve(self, question, top_k):
                active[0] = "new"
                return ({"version_id": "captured"}, [])

        with patch.object(rag, "generate_answer", return_value="answer"):
            result = rag.answer_question(ChatRequest(message="hi"), Settings(knowledge_mode="versioned"), knowledge_service=Service())
        self.assertEqual(active, ["new"])
        self.assertEqual(result.knowledge_version, "captured")

    def test_ingest_and_low_level_guards_precede_embedding(self):
        with patch.object(vector_store, "embed_texts", side_effect=AssertionError("embedding called")):
            with self.assertRaises(HTTPException) as raised:
                rag.ingest_documents(IngestRequest(documents=[Document(text="x")]), Settings(knowledge_mode="versioned"))
            self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException) as raised:
                vector_store.upsert_documents([Document(text="x")], "default", Settings(knowledge_mode="versioned"))
            self.assertEqual(raised.exception.status_code, 409)
            with self.assertRaises(HTTPException) as raised:
                vector_store.upsert_documents([Document(text="x")], "knowledge-managed", Settings())
            self.assertEqual(raised.exception.status_code, 409)

    def test_legacy_upsert_still_works_for_unmanaged_namespace(self):
        with patch.object(vector_store, "embed_texts", return_value=[[0.1]]), patch.object(vector_store, "_index") as make_index:
            make_index.return_value.upsert.return_value = None
            count = vector_store.upsert_documents([Document(text="x")], "default", Settings())
        self.assertEqual(count, 1)
        make_index.return_value.upsert.assert_called_once()

    def test_legacy_loader_rejects_candidate_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            candidate = Path(temp_dir) / "candidate.json"
            candidate.write_text(json.dumps({"kind": "knowledge-candidate", "documents": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Versioned knowledge artifacts"):
                upload_vectorstore._load_documents(candidate)

    def test_app_accepts_optional_settings_and_service(self):
        from fastapi.testclient import TestClient
        from api.endpoint.application import create_app

        class Service:
            def retrieve(self, question, top_k):
                return ({"version_id": "web-v"}, [])

        with patch.object(rag, "generate_answer", return_value="answer"):
            client = TestClient(create_app(settings=Settings(knowledge_mode="versioned"), knowledge_service=Service()))
            response = client.post("/api/chat", json={"message": "hello"}, headers={"origin": "https://zachcodes.vercel.app"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["knowledge_version"], "web-v")


if __name__ == "__main__":
    unittest.main()
