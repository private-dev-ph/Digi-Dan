import math
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.ports import VersioningError


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class SequencedModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def embed_content(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ProviderError(Exception):
    def __init__(self, code, *, headers=None, details=None):
        super().__init__("private provider body")
        self.code = code
        self.response = SimpleNamespace(headers=headers or {})
        self.details = details


class CloudEmbeddingTests(unittest.TestCase):
    def response(self, *vectors):
        return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in vectors])

    def test_profile_is_canonical_and_restricts_dimensions_and_model(self):
        self.assertEqual(gemini_profile(), {"provider": "gemini", "model": "gemini-embedding-001",
                                            "dimensions": 768, "policy_version": "normalized-v1"})
        for dim in (0, 769, 1024, True):
            with self.assertRaises(VersioningError):
                gemini_profile(dim)
        with self.assertRaises(VersioningError):
            gemini_profile(model="other")

    def test_invocation_sends_expected_model_contents_and_config(self):
        for dims in (768, 1536, 3072):
            models = FakeModels(self.response([1.0] * dims))
            embedder = GeminiEmbedder(client=SimpleNamespace(models=models))
            result = embedder.embed(["hello"], gemini_profile(dims))
            self.assertEqual(len(result[0]), dims)
            call = models.calls[0]
            self.assertEqual(call["model"], "gemini-embedding-001")
            self.assertEqual(call["contents"], ["hello"])
            self.assertEqual(call["config"].output_dimensionality, dims)

    def test_profile_is_validated_before_provider_call(self):
        models = FakeModels(self.response([1.0] * 768))
        adapter = GeminiEmbedder(client=SimpleNamespace(models=models))
        with self.assertRaises(VersioningError):
            adapter.embed(["hello"], {**gemini_profile(), "policy_version": "raw"})
        self.assertEqual(models.calls, [])

    def test_rejects_bad_batches(self):
        models = FakeModels(self.response())
        adapter = GeminiEmbedder(client=SimpleNamespace(models=models))
        for texts in ([], [""], ["  "], [1], ["x"] * 33, "x"):
            with self.subTest(texts=type(texts).__name__, count=len(texts) if hasattr(texts, "__len__") else 0):
                with self.assertRaises(VersioningError):
                    adapter.embed(texts, gemini_profile())
        self.assertEqual(models.calls, [])

    def test_normalizes_vectors_and_rejects_invalid_response(self):
        dims = 768
        models = FakeModels(self.response([3.0] + [0.0] * (dims - 1)))
        result = GeminiEmbedder(client=SimpleNamespace(models=models)).embed(["x"], gemini_profile())
        self.assertEqual(result[0][0], 1.0)
        self.assertAlmostEqual(math.hypot(*result[0]), 1.0)
        bad_vectors = ([0.0] * dims, [float("nan")] + [0.0] * (dims - 1),
                       [float("inf")] + [0.0] * (dims - 1), [True] + [0.0] * (dims - 1), [1.0],
                       [10 ** 10000] + [0.0] * (dims - 1))
        for vector in bad_vectors:
            adapter = GeminiEmbedder(client=SimpleNamespace(models=FakeModels(self.response(vector))))
            with self.assertRaises(VersioningError):
                adapter.embed(["x"], gemini_profile())
        adapter = GeminiEmbedder(client=SimpleNamespace(models=FakeModels(self.response([1.0] * dims))))
        with self.assertRaises(VersioningError):
            adapter.embed(["a", "b"], gemini_profile())

    def test_finite_timeout_is_passed_to_sdk_client(self):
        from google import genai
        with patch.object(genai, "Client", return_value=SimpleNamespace(models=FakeModels(self.response([1.0] * 768)))) as ctor:
            GeminiEmbedder(api_key="secret", timeout_ms=12345).embed(["x"], gemini_profile())
        options = ctor.call_args.kwargs["http_options"]
        self.assertEqual(options.timeout, 12345)
        self.assertEqual(ctor.call_args.kwargs["api_key"], "secret")

    def test_lazily_constructed_client_is_reused(self):
        from google import genai
        fake = SimpleNamespace(models=FakeModels(self.response([1.0] * 768)))
        with patch.object(genai, "Client", return_value=fake) as ctor:
            adapter = GeminiEmbedder(api_key="secret")
            adapter.embed(["first"], gemini_profile())
            adapter.embed(["second"], gemini_profile())
        ctor.assert_called_once()
        self.assertEqual(len(fake.models.calls), 2)

    def test_provider_errors_are_sanitized_and_not_chained(self):
        secret_error = RuntimeError("secret-key response body leaked")
        adapter = GeminiEmbedder(client=SimpleNamespace(models=FakeModels(error=secret_error)))
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(["x"], gemini_profile())
        self.assertEqual(caught.exception.code, "embedding_provider_error")
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_429_retries_same_payload_and_parses_retry_info(self):
        rate_limit = ProviderError(429, details=[{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                  "retryDelay": "1.25s"}])
        models = SequencedModels([rate_limit, self.response([2.0] + [0.0] * 767)])
        sleeps = []
        result = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=2,
                                sleep=sleeps.append).embed(["batch"], gemini_profile())
        self.assertEqual(result[0][0], 1.0)
        self.assertEqual(sleeps, [1.25])
        self.assertEqual(models.calls[0], models.calls[1])

    def test_sdk_nested_error_details_retry_info_shape(self):
        error = ProviderError(429, details={"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.75s"}
        ]}})
        models = SequencedModels([error, self.response([1.0] + [0.0] * 767)])
        sleeps = []
        GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=2,
                       sleep=sleeps.append).embed(["x"], gemini_profile())
        self.assertEqual(sleeps, [0.75])
        self.assertEqual(len(models.calls), 2)

    def test_429_delay_header_fallback_and_bounds(self):
        cases = [({"Retry-After": "2.5"}, None, [2.5, 2.5]), ({}, None, [60.0, 60.0]),
                 ({"Retry-After": "61"}, None, None),
                 ({}, [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                       "retryDelay": "bad"}], [60.0, 60.0])]
        for headers, details, expected_sleep in cases:
            with self.subTest(headers=headers, details=details):
                error = ProviderError(429, headers=headers, details=details)
                models = SequencedModels([error, error, error])
                sleeps = []
                adapter = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=3,
                                         sleep=sleeps.append)
                with self.assertRaises(VersioningError) as caught:
                    adapter.embed(["x"], gemini_profile())
                self.assertEqual(caught.exception.code, "embedding_rate_limited")
                self.assertEqual(len(models.calls), 1 if expected_sleep is None else 3)
                self.assertEqual(sleeps, [] if expected_sleep is None else expected_sleep)

    def test_default_no_retry_and_non_429_errors_are_not_retried(self):
        for error in (ProviderError(401), ProviderError(500), RuntimeError("secret"),
                      ProviderError("429")):
            with self.subTest(error=type(error).__name__, code=getattr(error, "code", None)):
                models = SequencedModels([error])
                sleeps = []
                adapter = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=3,
                                         sleep=sleeps.append)
                with self.assertRaises(VersioningError) as caught:
                    adapter.embed(["x"], gemini_profile())
                self.assertEqual(caught.exception.code, "embedding_provider_error")
                self.assertEqual(len(models.calls), 1)
                self.assertEqual(sleeps, [])

    def test_default_429_fails_without_sleep_or_retry(self):
        models = SequencedModels([ProviderError(429), ProviderError(429)])
        sleeps = []
        adapter = GeminiEmbedder(client=SimpleNamespace(models=models), sleep=sleeps.append)
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(["x"], gemini_profile())
        self.assertEqual(caught.exception.code, "embedding_rate_limited")
        self.assertEqual(len(models.calls), 1)
        self.assertEqual(sleeps, [])

    def test_malformed_response_does_not_retry(self):
        models = SequencedModels([self.response([1.0])])
        adapter = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=3,
                                  sleep=lambda _: self.fail("must not sleep"))
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(["x"], gemini_profile())
        self.assertEqual(caught.exception.code, "invalid_embedding_response")
        self.assertEqual(len(models.calls), 1)

    def test_max_attempts_validation(self):
        for value in (0, 4, True, 1.5):
            with self.subTest(value=value), self.assertRaises(VersioningError):
                GeminiEmbedder(max_attempts=value)


if __name__ == "__main__":
    unittest.main()
