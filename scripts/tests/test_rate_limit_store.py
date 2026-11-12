"""Rate limit storage and enforcement configuration."""
import concurrent.futures
import unittest
import httpx
from api.tools.rate_limit import LocalMemoryRateLimiter, RATE_LIMIT_SCRIPT, RateLimitDecision, RateLimitUnavailable, RedisRateLimiter
from unittest.mock import patch
from fastapi.testclient import TestClient
from pydantic import ValidationError
from api.core.config import Settings, get_settings
from api.endpoint.application import create_app
from api.query import rag


CLIENT_A = "a" * 64


CLIENT_B = "b" * 64


class FakeResponse:
    def __init__(self, result=None, status=200, error=None):
        self.result, self.status, self.error = result, status, error
        self.headers = {}

    @property
    def status_code(self):
        return self.status

    def json(self):
        if self.error:
            raise self.error
        return self.result


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls = []
        self.closed = False

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


class LocalRateLimiterTests(unittest.TestCase):
    def test_limit_expiry_and_independent_clients(self):
        now = [10.0]
        limiter = LocalMemoryRateLimiter(clock=lambda: now[0])
        self.assertEqual(limiter.consume(CLIENT_A, 2, 4), RateLimitDecision(True, 1, 4))
        self.assertEqual(limiter.consume(CLIENT_A, 2, 4), RateLimitDecision(True, 0, 4))
        self.assertEqual(limiter.consume(CLIENT_A, 2, 4), RateLimitDecision(False, 0, 4))
        self.assertEqual(limiter.consume(CLIENT_B, 2, 4).allowed, True)
        now[0] = 14.0
        self.assertEqual(limiter.consume(CLIENT_A, 2, 4), RateLimitDecision(True, 1, 4))
        self.assertEqual(set(limiter._entries), {CLIENT_A})

    def test_expiry_is_ceiled_and_exact_limit_is_thread_safe(self):
        now = [0.0]
        limiter = LocalMemoryRateLimiter(clock=lambda: now[0])
        limiter.consume(CLIENT_A, 1, 5)
        now[0] = 1.2
        self.assertEqual(limiter.consume(CLIENT_A, 1, 5).retry_after, 4)
        now[0] = 10.0
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            decisions = list(pool.map(lambda _: limiter.consume(CLIENT_B, 7, 60), range(25)))
        self.assertEqual(sum(d.allowed for d in decisions), 7)
        self.assertEqual(sum(not d.allowed for d in decisions), 18)

    def test_policy_rejects_invalid_values_and_booleans(self):
        limiter = LocalMemoryRateLimiter()
        for client, limit, window in [("A" * 64, 1, 10), (CLIENT_A, True, 10),
                                      (CLIENT_A, 1, False), (CLIENT_A, 0, 10),
                                      (CLIENT_A, 1, 604801)]:
            with self.subTest(limit=limit, window=window), self.assertRaises(ValueError):
                limiter.consume(client, limit, window)


class RedisRateLimiterTests(unittest.TestCase):
    def make_store(self, response=None, error=None, environment="test"):
        client = FakeClient(response, error)
        return RedisRateLimiter("https://example.upstash.io", "secret", environment,
                                client=client, timeout=1.25), client

    def test_construction_does_no_io_and_command_is_exact(self):
        store, client = self.make_store(FakeResponse({"result": [1, 3, 1234]}))
        self.assertEqual(client.calls, [])
        decision = store.consume(CLIENT_A, 4, 60)
        self.assertEqual(decision, RateLimitDecision(True, 3, 2))
        args, kwargs = client.calls[0]
        self.assertEqual(args, ("https://example.upstash.io/",))
        self.assertEqual(kwargs["json"], ["EVAL", RATE_LIMIT_SCRIPT, "1",
                                          "digi-dan:ratelimit:test:chat:v1:" + CLIENT_A, "4", "60000"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(kwargs["timeout"], 1.25)
        self.assertFalse(kwargs["follow_redirects"])

    def test_prefix_uses_environment_and_denials_are_validated(self):
        store, client = self.make_store(FakeResponse({"result": [0, 0, 1]}), environment="production")
        self.assertEqual(store.consume(CLIENT_B, 2, 1), RateLimitDecision(False, 0, 1))
        self.assertIn("digi-dan:ratelimit:production:chat:v1:" + CLIENT_B,
                      client.calls[0][1]["json"])

    def test_malformed_responses_status_and_network_errors_fail_closed_once(self):
        cases = [FakeResponse({"result": [True, 1, 1000]}), FakeResponse({"result": [1, 2, 0]}),
                 FakeResponse({"result": [1, 1, 604_800_001]}),
                 FakeResponse({"result": [0, 1, 1000]}), FakeResponse({}, status=500),
                 FakeResponse(error=ValueError("sensitive raw response"))]
        for response in cases:
            store, client = self.make_store(response)
            with self.subTest(response=response), self.assertRaises(RateLimitUnavailable) as caught:
                store.consume(CLIENT_A, 2, 60)
            self.assertEqual(str(caught.exception), "Rate limit service is unavailable")
            self.assertEqual(len(client.calls), 1)
        store, client = self.make_store(error=httpx.ReadTimeout("sensitive timeout"))
        with self.assertRaises(RateLimitUnavailable):
            store.consume(CLIENT_A, 2, 60)
        self.assertEqual(len(client.calls), 1)

    def test_config_and_policy_validation(self):
        for url in ["http://example.upstash.io", "https://evil.test", "https://u:p@example.upstash.io",
                    "https://example.upstash.io/path", "https://example.upstash.io/?x=1"]:
            with self.subTest(url=url), self.assertRaises(RateLimitUnavailable):
                RedisRateLimiter(url, "secret", "test", client=FakeClient())
        with self.assertRaises(RateLimitUnavailable):
            RedisRateLimiter("https://example.upstash.io", "  ", "test", client=FakeClient())
        with self.assertRaises(RateLimitUnavailable):
            RedisRateLimiter("https://example.upstash.io", "secret", "invalid", client=FakeClient())
        store, client = self.make_store(FakeResponse({"result": [1, 0, 1000]}))
        with self.assertRaises(ValueError):
            store.consume(CLIENT_A, True, 60)
        self.assertEqual(client.calls, [])


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


if __name__ == "__main__":
    unittest.main()
