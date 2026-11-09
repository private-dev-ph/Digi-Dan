import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.core.config import Settings, get_settings
from api.endpoint.application import create_app
from api.query import rag


class RateLimitConfigurationTests(unittest.TestCase):
    def test_shared_mode_requires_trust_and_positive_allowance(self):
        for values in (
            {"chat_rate_limit_backend": "redis"},
            {"chat_rate_limit_backend": "redis", "chat_client_identity_mode": "trusted_proxy", "chat_rate_limit": 0},
            {"chat_client_identity_mode": "local", "knowledge_environment": "production"},
            {"chat_local_client_id": "a" * 64 + "\n"},
            {"chat_rate_limit_window_seconds": 0},
            {"rate_limit_redis_timeout_seconds": float("nan")},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Settings(**values)

    def test_missing_limiter_credentials_fail_closed_without_network_or_generation(self):
        settings = Settings(chat_rate_limit_backend="redis", chat_client_identity_mode="trusted_proxy", api_bypass_secret="test-secret")
        with patch.object(rag, "generate_answer") as generate:
            with TestClient(create_app(settings=settings)) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers={"x-api-secret":"test-secret", "x-digidan-client-id":"a"*64})
        self.assertEqual(response.status_code, 503)
        generate.assert_not_called()

    def test_environment_reads_separate_limiter_credentials(self):
        get_settings.cache_clear()
        try:
            with patch("api.core.config.load_dotenv"), patch.dict("os.environ", {
                "CHAT_RATE_LIMIT_BACKEND":"redis", "CHAT_CLIENT_IDENTITY_MODE":"trusted_proxy",
                "RATE_LIMIT_REDIS_REST_URL":"https://limiter.upstash.io",
                "RATE_LIMIT_REDIS_REST_TOKEN":"synthetic-limiter-token",
                "UPSTASH_REDIS_REST_TOKEN":"synthetic-knowledge-read-token",
                "RATE_LIMIT_REDIS_TIMEOUT_SECONDS":"1.5",
            }, clear=True):
                settings = get_settings()
            self.assertEqual(settings.rate_limit_redis_rest_token, "synthetic-limiter-token")
            self.assertEqual(settings.upstash_redis_rest_token, "synthetic-knowledge-read-token")
            self.assertEqual(settings.rate_limit_redis_timeout_seconds, 1.5)
        finally:
            get_settings.cache_clear()
