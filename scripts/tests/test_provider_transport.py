import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))

from api.core.config import Settings
from api.query import providers


class ProviderTransportTests(unittest.TestCase):
    def invoke(self, provider, response_body, status=200, **settings_kwargs):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(status, content=response_body, request=request)
        transport = httpx.MockTransport(handler)
        timeout = settings_kwargs.pop("generation_timeout_seconds", 20)
        settings = Settings(**settings_kwargs)
        object.__setattr__(settings, "generation_timeout_seconds", timeout)
        with patch.object(providers.httpx, "AsyncHTTPTransport", return_value=transport):
            result = providers.generate_answer(provider, "prompt", "system", settings)
        return result, calls

    def test_gemini_and_deepseek_request_shapes_and_single_call(self):
        result, calls = self.invoke("gemini", json.dumps({"candidates": [{"content": {"parts": [{"text": "  hello  "}]}}]}), gemini_api_key="secret", gemini_model="models/model/a")
        self.assertEqual(result, "hello")
        self.assertEqual(len(calls), 1)
        self.assertIn("models/model%2Fa:generateContent", str(calls[0].url))
        self.assertEqual(calls[0].headers["x-goog-api-key"], "secret")
        self.assertNotIn("secret", str(calls[0].url))
        payload = json.loads(calls[0].content)
        self.assertEqual(payload["systemInstruction"]["parts"][0]["text"], "system")
        thought_result, _ = self.invoke("gemini", json.dumps({"candidates": [{"content": {"parts": [{"text": "internal", "thought": True}, {"text": "public refusal text"}]}}]}), gemini_api_key="secret")
        self.assertEqual(thought_result, "public refusal text")
        refusal_text, _ = self.invoke("deepseek", json.dumps({"choices": [{"message": {"content": "I can’t help with that request."}}]}), deepseek_api_key="secret")
        self.assertEqual(refusal_text, "I can’t help with that request.")
        result, calls = self.invoke("deepseek", json.dumps({"choices": [{"message": {"content": "answer"}}]}), deepseek_api_key="secret", deepseek_base_url="https://api.test/v1/", deepseek_model="chat")
        self.assertEqual(result, "answer")
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(calls[0].url), "https://api.test/v1/chat/completions")
        self.assertEqual(calls[0].headers["authorization"], "Bearer secret")
        payload = json.loads(calls[0].content)
        self.assertEqual(payload["messages"], [{"role": "system", "content": "system"}, {"role": "user", "content": "prompt"}])
        self.assertFalse(payload["stream"])

    def test_policy_refusals_detected_before_answer(self):
        bodies = [
            ("gemini", {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}),
            ("gemini", {"promptFeedback": {"safetyRatings": [{"blocked": True}]}}),
            ("gemini", {"candidates": [{"safetyRatings": [{"blocked": True}], "content": {"parts": [{"text": "ignored"}]}}]}),
            ("gemini", {"candidates": [{"finishReason": "BLOCKLIST", "content": {"parts": [{"text": "ignored"}]}}]}),
            ("gemini", {"candidates": [{"content": {"parts": [{"text": "ignored", "safetyRatings": [{"blocked": True}]}]}}]}),
            ("deepseek", {"choices": [{"finish_reason": "content_filter", "message": {"content": "ignored"}}]}),
            ("deepseek", {"choices": [{"message": {"refusal": "No", "content": "ignored"}}]}),
        ]
        for provider, body in bodies:
            kwargs = {"gemini_api_key": "k"} if provider == "gemini" else {"deepseek_api_key": "k"}
            with self.subTest(provider=provider, body=body), self.assertRaises(providers.ProviderFailure) as raised:
                self.invoke(provider, json.dumps(body), **kwargs)
            self.assertEqual(raised.exception.category, "refusal")

    def test_malformed_and_empty_payloads(self):
        for provider, key in (("gemini", {"gemini_api_key": "k"}), ("deepseek", {"deepseek_api_key": "k"})):
            bodies = [b"not json", b"{}", json.dumps({"candidates": [{"content": {"parts": [{"text": "  "}]}}]}).encode() if provider == "gemini" else json.dumps({"choices": [{"message": {"content": " "}}]}).encode()]
            if provider == "gemini":
                bodies.append(json.dumps({"candidates": [{"finishReason": ["bad"], "content": {"parts": [{"text": "ignored"}]}}]}).encode())
            for body in bodies:
                with self.subTest(provider=provider), self.assertRaises(providers.ProviderFailure) as raised:
                    self.invoke(provider, body, **key)
                self.assertEqual(raised.exception.category, "malformed")

    def test_http_categories_are_sanitized_and_nonretryable_categories(self):
        expected = {401: "configuration", 403: "configuration", 404: "configuration", 429: "rate_limit", 500: "server", 408: "timeout", 400: "rejected"}
        for status, category in expected.items():
            with self.subTest(status=status), self.assertRaises(providers.ProviderFailure) as raised:
                self.invoke("deepseek", b"secret provider body", status=status, deepseek_api_key="TOP_SECRET")
            self.assertEqual(str(raised.exception), category)
            self.assertNotIn("TOP_SECRET", str(raised.exception))
            self.assertNotIn("secret provider body", str(raised.exception))
            self.assertEqual(raised.exception.retryable, category in {"timeout", "connection", "rate_limit", "server", "malformed"})

    def test_connection_and_timeout_exceptions_are_sanitized(self):
        for exc, category in ((httpx.ConnectError("secret connect detail"), "connection"), (httpx.ReadTimeout("secret timeout detail"), "timeout")):
            class FailingTransport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    raise exc
                async def aclose(self):
                    pass
            with patch.object(providers.httpx, "AsyncHTTPTransport", return_value=FailingTransport()), self.assertRaises(providers.ProviderFailure) as raised:
                providers.generate_answer("deepseek", "p", "s", Settings(deepseek_api_key="TOP_SECRET"))
            self.assertEqual(raised.exception.category, category)
            self.assertNotIn("secret", str(raised.exception))
            self.assertNotIn("TOP_SECRET", str(raised.exception))

    def test_transport_disables_retries_and_redirects_and_rejects_empty_model(self):
        seen = {}
        real_transport = httpx.AsyncHTTPTransport
        def make_transport(*, retries):
            seen["retries"] = retries
            return httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=request))
        real_client = httpx.AsyncClient
        def make_client(*args, follow_redirects, **kwargs):
            seen["follow_redirects"] = follow_redirects
            return real_client(*args, follow_redirects=follow_redirects, **kwargs)
        with patch.object(providers.httpx, "AsyncHTTPTransport", side_effect=make_transport), patch.object(providers.httpx, "AsyncClient", side_effect=make_client):
            providers.generate_answer("deepseek", "p", "s", Settings(deepseek_api_key="k"))
        self.assertEqual(seen, {"retries": 0, "follow_redirects": False})
        with patch.object(providers.httpx, "AsyncHTTPTransport", side_effect=AssertionError("must not request")):
            with self.assertRaises(providers.ProviderFailure) as raised:
                providers.generate_answer("gemini", "p", "s", Settings(gemini_api_key="k", gemini_model="models/"))
        self.assertEqual(raised.exception.category, "configuration")

    def test_missing_keys_and_models_fail_before_http(self):
        with patch.object(providers.httpx, "AsyncHTTPTransport", side_effect=AssertionError("transport created")):
            for kwargs in ({}, {"gemini_api_key": " ", "gemini_model": "m"}, {"deepseek_api_key": "k", "deepseek_model": " "}):
                provider = "deepseek" if "deepseek_api_key" in kwargs else "gemini"
                with self.subTest(kwargs=kwargs), self.assertRaises(providers.ProviderFailure) as raised:
                    providers.generate_answer(provider, "p", "s", Settings(**kwargs))
                self.assertEqual(raised.exception.category, "configuration")

    def test_timeout_cancels_request_and_closes_client(self):
        state = {"cancelled": False, "closed": False}
        class SlowTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    raise
            async def aclose(self):
                state["closed"] = True
        transport = SlowTransport()
        with patch.object(providers.httpx, "AsyncHTTPTransport", return_value=transport), self.assertRaises(providers.ProviderFailure) as raised:
            providers.generate_answer("deepseek", "p", "s", Settings(deepseek_api_key="k", generation_timeout_seconds=0.02))
        self.assertEqual(raised.exception.category, "timeout")
        self.assertTrue(state["cancelled"])
        self.assertTrue(state["closed"])

    def test_categories_are_fixed_and_unknown_is_sanitized(self):
        err = providers.ProviderFailure("private detail")
        self.assertEqual(err.category, "unexpected")
        self.assertEqual(str(err), "unexpected")
        self.assertFalse(err.retryable)


if __name__ == "__main__":
    unittest.main()
