import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from fastapi import HTTPException

from api.core.config import Settings
from api.endpoint.application import create_app
from api.knowledge.ports import VersioningError
from api.query import rag


ORIGIN = {"origin": "https://zachcodes.vercel.app"}


class Stage3ProductionRuntimeTests(unittest.TestCase):
    def client(self, settings, service=None):
        return TestClient(create_app(settings=settings, knowledge_service=service))

    def test_production_constructs_read_only_service(self):
        service = object()
        settings = Settings(knowledge_mode="versioned", knowledge_environment="production")
        with patch("api.knowledge.cloud.create_cloud_service", return_value=service) as factory:
            app = create_app(settings=settings)
        factory.assert_called_once_with(settings, writable=False)
        self.assertIs(app.state.knowledge_service, service)

    def test_legacy_never_constructs_cloud_service(self):
        with patch("api.knowledge.cloud.create_cloud_service") as factory:
            app = create_app(settings=Settings(knowledge_mode="legacy", knowledge_environment="production"))
        factory.assert_not_called()
        self.assertIsNone(app.state.knowledge_service)

    def test_test_environment_fails_closed(self):
        settings = Settings(knowledge_mode="versioned", knowledge_environment="test")
        with patch("api.knowledge.cloud.create_cloud_service") as factory:
            client = self.client(settings)
        factory.assert_not_called()
        response = client.post("/api/chat", json={"message": "hello"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        client.close()

    def test_production_configuration_failure_is_generic_503(self):
        settings = Settings(knowledge_mode="versioned", knowledge_environment="production")
        with patch("api.knowledge.cloud.create_cloud_service", side_effect=VersioningError("missing_cloud_credentials", "secret detail")):
            client = self.client(settings)
        response = client.post("/api/chat", json={"message": "hello"}, headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret detail", response.text)
        client.close()

    def test_injected_service_is_used_and_status_is_minimal(self):
        class Service:
            def retrieve(self, question, top_k):
                return ({"version_id": "v123", "namespace": "secret"}, [{"id": "fact", "text": "answer fact", "metadata": {"ticket": "secret"}}])

            def get_active(self):
                return {"version_id": "v123", "namespace": "secret", "ticket": "secret", "source_facts": ["secret"]}

        settings = Settings(knowledge_mode="versioned", knowledge_environment="test")
        service = Service()
        with patch("api.knowledge.cloud.create_cloud_service") as factory, patch.object(rag, "generate_answer", return_value="ok"):
            client = self.client(settings, service)
            chat = client.post("/api/chat", json={"message": "question"}, headers=ORIGIN)
            status = client.get("/api/knowledge/status", headers=ORIGIN)
        factory.assert_not_called()
        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["knowledge_version"], "v123")
        self.assertEqual(status.json(), {"mode": "versioned", "ready": True, "knowledge_version": "v123"})
        client.close()

    def test_auto_created_production_service_handles_chat(self):
        class Service:
            def retrieve(self, question, top_k):
                return ({"version_id": "cloud-v1"}, [{"id": "cloud-fact", "text": "from cloud", "metadata": {}}])

        service = Service()
        settings = Settings(knowledge_mode="versioned", knowledge_environment="production")
        with patch("api.knowledge.cloud.create_cloud_service", return_value=service) as factory, patch.object(rag, "generate_answer", return_value="ok"):
            client = self.client(settings)
            response = client.post("/api/chat", json={"message": "question"}, headers=ORIGIN)
        factory.assert_called_once_with(settings, writable=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["knowledge_version"], "cloud-v1")
        client.close()

    def test_status_without_service_or_active_version_returns_503(self):
        settings = Settings(knowledge_mode="versioned", knowledge_environment="test")
        client = self.client(settings)
        response = client.get("/api/knowledge/status", headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        client.close()

        class EmptyService:
            def get_active(self):
                return None

        client = self.client(settings, EmptyService())
        response = client.get("/api/knowledge/status", headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        client.close()

        class BrokenService:
            def get_active(self):
                raise VersioningError("registry_corrupt", "private details")

        client = self.client(settings, BrokenService())
        response = client.get("/api/knowledge/status", headers=ORIGIN)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private details", response.text)
        client.close()

    def test_legacy_status_is_ready(self):
        client = self.client(Settings(knowledge_mode="legacy"))
        response = client.get("/api/knowledge/status", headers=ORIGIN)
        self.assertEqual(response.json(), {"mode": "legacy", "ready": True})
        client.close()


if __name__ == "__main__":
    unittest.main()
