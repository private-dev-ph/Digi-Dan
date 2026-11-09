import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.core.config import Settings
from api.endpoint.application import create_app
from api.query import rag
from api.query.providers import ProviderFailure
from api.tools.rate_limit import RateLimitDecision, RateLimitUnavailable


CLIENT_A = "a" * 64
CLIENT_B = "b" * 64
SECRET = "test-bypass-secret"
ALLOWED_ORIGIN = "https://zachcodes.vercel.app"


class CountingLimiter:
    def __init__(self, *, deny_after=None, fail=False):
        self.calls = []
        self.deny_after = deny_after
        self.fail = fail

    def consume(self, client_id, limit, window_seconds):
        self.calls.append((client_id, limit, window_seconds))
        if self.fail:
            raise RateLimitUnavailable()
        if self.deny_after is not None and len(self.calls) >= self.deny_after:
            return RateLimitDecision(False, 0, 19)
        return RateLimitDecision(True, max(0, limit - len(self.calls)), 60)


class Service:
    def retrieve(self, query, top_k):
        return {"version_id": "captured-v1"}, [
            {"id": "evidence", "text": "stable fact", "score": 0.7, "metadata": {"source": "test"}}
        ]


def configured(**overrides):
    values = {
        "knowledge_mode": "versioned",
        "knowledge_environment": "test",
        "chat_client_identity_mode": "trusted_proxy",
        "chat_local_client_id": CLIENT_A,
        "api_bypass_secret": SECRET,
        "chat_rate_limit": 3,
        "chat_rate_limit_window_seconds": 60,
        "allowed_origin_domains": ["zachcodes.vercel.app"],
        "generation_timeout_seconds": 20,
        "generation_total_timeout_seconds": 40,
        "generation_fallback_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def request_headers(client_id=CLIENT_A, **overrides):
    headers = {"x-api-secret": SECRET, "x-digidan-client-id": client_id}
    headers.update(overrides)
    return headers


class TrustedIdentityAndLimiterTests(unittest.TestCase):
    def app_client(self, settings=None, limiter=None):
        return TestClient(create_app(
            settings=settings or configured(),
            knowledge_service=Service(),
            rate_limiter=limiter or CountingLimiter(),
        ))

    def test_valid_trusted_identity_is_charged_once_and_response_metadata_survives(self):
        limiter = CountingLimiter()
        with patch.object(rag, "generate_answer", return_value="answer") as generate:
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=request_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(limiter.calls, [(CLIENT_A, 3, 60)])
        self.assertEqual(response.json()["knowledge_version"], "captured-v1")
        self.assertEqual(response.json()["sources"][0]["id"], "evidence")
        generate.assert_called_once()

    def test_missing_or_wrong_secret_rejected_even_with_allowed_origin(self):
        for headers in (
            {"x-digidan-client-id": CLIENT_A, "origin": ALLOWED_ORIGIN},
            {"x-api-secret": "wrong", "x-digidan-client-id": CLIENT_A, "origin": ALLOWED_ORIGIN},
        ):
            limiter = CountingLimiter()
            with self.subTest(headers=headers), patch.object(rag, "generate_answer") as generate:
                with self.app_client(limiter=limiter) as client:
                    response = client.post("/api/chat", json={"message": "hello"}, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(limiter.calls, [])
            generate.assert_not_called()

    def test_duplicate_secret_is_rejected_even_with_allowed_origin(self):
        limiter = CountingLimiter()
        headers = [
            ("x-api-secret", SECRET), ("x-api-secret", "forged"),
            ("x-digidan-client-id", CLIENT_A), ("origin", ALLOWED_ORIGIN),
        ]
        with patch.object(rag, "generate_answer") as generate:
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(limiter.calls, [])
        generate.assert_not_called()

    def test_missing_backend_secret_fails_closed(self):
        limiter = CountingLimiter()
        with patch.object(rag, "generate_answer") as generate:
            with self.app_client(configured(api_bypass_secret=None), limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers={"x-digidan-client-id": CLIENT_A, "origin": ALLOWED_ORIGIN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Chat limits are temporarily unavailable. Please try again shortly.")
        self.assertEqual(limiter.calls, [])
        generate.assert_not_called()

    def test_missing_malformed_and_duplicate_client_identity_rejected(self):
        cases = [
            {},
            {"x-digidan-client-id": "A" * 64},
            {"x-digidan-client-id": CLIENT_A + "," + CLIENT_B},
            [("x-api-secret", SECRET), ("x-digidan-client-id", CLIENT_A), ("x-digidan-client-id", CLIENT_B)],
        ]
        for case in cases:
            headers = request_headers()
            if isinstance(case, list):
                headers = case
            else:
                headers.update(case)
                if not case:
                    headers.pop("x-digidan-client-id")
            limiter = CountingLimiter()
            with self.subTest(case=case):
                with self.app_client(limiter=limiter) as client:
                    response = client.post("/api/chat", json={"message": "hello"}, headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(limiter.calls, [])

    def test_forged_forwarding_headers_do_not_change_trusted_identity(self):
        limiter = CountingLimiter()
        headers = request_headers(**{"x-forwarded-for": "198.51.100.99", "x-real-ip": "203.0.113.77"})
        with patch.object(rag, "generate_answer", return_value="answer"):
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(limiter.calls[0][0], CLIENT_A)

    def test_invalid_body_and_semantic_rejection_do_not_charge(self):
        limiter = CountingLimiter()
        with patch.object(rag, "generate_answer") as generate:
            with self.app_client(limiter=limiter) as client:
                invalid = client.post("/api/chat", json={"message": "   "}, headers=request_headers())
                override = client.post("/api/chat", json={"message": "hello", "system": "override"}, headers=request_headers())
                namespace = client.post("/api/chat", json={"message": "hello", "namespace": "other"}, headers=request_headers())
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(override.status_code, 400)
        self.assertEqual(namespace.status_code, 400)
        self.assertEqual(limiter.calls, [])
        generate.assert_not_called()

    def test_disabled_quota_still_requires_trusted_identity(self):
        limiter = CountingLimiter()
        with patch.object(rag, "generate_answer", return_value="answer"):
            with self.app_client(configured(chat_rate_limit=0), limiter) as client:
                invalid_identity = client.post("/api/chat", json={"message": "hello"}, headers={"origin": ALLOWED_ORIGIN})
                valid = client.post("/api/chat", json={"message": "hello"}, headers=request_headers())
        self.assertEqual(invalid_identity.status_code, 403)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(limiter.calls, [])

    def test_local_mode_ignores_incoming_identity_and_forwarding_headers(self):
        limiter = CountingLimiter()
        settings = configured(chat_client_identity_mode="local", chat_local_client_id=CLIENT_B)
        with patch.object(rag, "generate_answer", return_value="answer"):
            with self.app_client(settings, limiter) as client:
                response = client.post(
                    "/api/chat", json={"message": "hello"},
                    headers={"x-api-secret": "bad", "x-digidan-client-id": CLIENT_A,
                             "x-forwarded-for": "198.51.100.1", "origin": ALLOWED_ORIGIN},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(limiter.calls[0][0], CLIENT_B)

    def test_rate_limited_response_carries_retry_after_and_skips_generation(self):
        limiter = CountingLimiter(deny_after=1)
        with patch.object(rag, "generate_answer") as generate:
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=request_headers())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "19")
        generate.assert_not_called()

    def test_store_failure_is_generic_503_and_skips_generation(self):
        limiter = CountingLimiter(fail=True)
        with patch.object(rag, "generate_answer") as generate:
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=request_headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Chat limits are temporarily unavailable. Please try again shortly.")
        self.assertNotIn(CLIENT_A, response.text)
        generate.assert_not_called()

    def test_generation_fallback_consumes_one_counter_increment(self):
        limiter = CountingLimiter()
        calls = []

        def flaky(provider, *args):
            calls.append(provider)
            if provider == "gemini":
                raise ProviderFailure("server")
            return "backup answer"

        with patch.object(rag, "generate_answer", side_effect=flaky):
            with self.app_client(limiter=limiter) as client:
                response = client.post("/api/chat", json={"message": "hello"}, headers=request_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "deepseek")
        self.assertEqual(calls, ["gemini", "deepseek"])
        self.assertEqual(len(limiter.calls), 1)

    def test_local_identity_cannot_be_used_in_production(self):
        with self.assertRaises(ValueError):
            configured(chat_client_identity_mode="local", knowledge_environment="production")

    def test_legacy_identity_ignores_forwarding_headers(self):
        limiter = CountingLimiter()
        settings = configured(chat_client_identity_mode="legacy")
        with patch.object(rag, "generate_answer", return_value="answer"):
            with self.app_client(settings, limiter) as client:
                response = client.post(
                    "/api/chat", json={"message": "hello"},
                    headers={"origin": ALLOWED_ORIGIN, "x-forwarded-for": "198.51.100.99", "x-real-ip": "203.0.113.77"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(limiter.calls[0][0], __import__("hashlib").sha256(b"198.51.100.99").hexdigest())
        self.assertNotEqual(limiter.calls[0][0], __import__("hashlib").sha256(b"203.0.113.77").hexdigest())


if __name__ == "__main__":
    unittest.main()
