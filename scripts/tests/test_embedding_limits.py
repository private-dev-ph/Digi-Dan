"""Embedding request pacing and quota handling."""
import unittest
from types import SimpleNamespace
from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.ports import VersioningError
from scripts.tests.test_cloud_embeddings import ProviderError, SequencedModels


class Clock:
    def __init__(self): self.now = 0; self.sleeps = []
    def time(self): return self.now
    def sleep(self, seconds): self.sleeps.append(seconds); self.now += seconds


class Models:
    def __init__(self): self.calls = []
    def embed_content(self, **kwargs):
        self.calls.append(kwargs['contents'][:])
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1] + [0]*767) for _ in kwargs['contents']])


class PacingTests(unittest.TestCase):
    def setup_adapter(self, background=True):
        clock, models = Clock(), Models()
        return GeminiEmbedder(client=SimpleNamespace(models=models), background=background, monotonic=clock.time, sleep=clock.sleep), clock, models
    def test_rolling_count_and_probe(self):
        adapter, clock, models = self.setup_adapter()
        for _ in range(2): adapter.embed(['x']*32, gemini_profile())
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.sleeps, [61])
        self.assertEqual(len(models.calls), 3)
    def test_bytes_split_order_and_oversize(self):
        adapter, clock, models = self.setup_adapter()
        texts = ['é'*6000, 'b'*12001, 'z']
        self.assertEqual(len(adapter.embed(texts, gemini_profile())), 3)
        self.assertEqual(models.calls, [texts[:1], texts[1:]])
        self.assertEqual(clock.sleeps, [61])
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(['x'*24001], gemini_profile())
        self.assertEqual(caught.exception.code, 'invalid_embedding_input')
        self.assertEqual(len(models.calls), 2)
    def test_rolling_expiry_does_not_reset_newer_attempts(self):
        adapter, clock, models = self.setup_adapter()
        adapter.embed(['a'] * 32, gemini_profile())
        clock.now = 30
        adapter.embed(['b'] * 32, gemini_profile())
        adapter.embed(['c'] * 32, gemini_profile())
        self.assertEqual(clock.now, 61)
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.now, 91)
        self.assertEqual(clock.sleeps, [31, 30])

    def test_subbatch_vectors_keep_input_order(self):
        adapter, clock, models = self.setup_adapter()
        texts = ['a' * 12001, 'b' * 12001, 'c']
        def vectors(**kwargs):
            models.calls.append(kwargs['contents'][:])
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1, ord(text[0])] + [0]*766)
                                               for text in kwargs['contents']])
        models.embed_content = vectors
        result = adapter.embed(texts, gemini_profile())
        for text, vector in zip(texts, result):
            self.assertAlmostEqual(vector[1] / vector[0], ord(text[0]))
        self.assertEqual(models.calls, [texts[:1], texts[1:]])

    def test_runtime_not_paced(self):
        adapter, clock, models = self.setup_adapter(False)
        for _ in range(3): adapter.embed(['x']*32, gemini_profile())
        self.assertEqual(clock.sleeps, [])
    def test_retry_reserves_attempt(self):
        adapter, clock, models = self.setup_adapter()
        original = models.embed_content
        class RateLimit(Exception):
            code = 429
            response = SimpleNamespace(headers={"Retry-After": "0"})
        def call(**kwargs):
            if not models.calls:
                models.calls.append(kwargs['contents'][:])
                raise RateLimit()
            return original(**kwargs)
        models.embed_content = call
        adapter._max_attempts = 2
        adapter.embed(['x']*32, gemini_profile())
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.sleeps, [1.0, 60.0])


def quota_error(violations, *, nested=True):
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": violations}]
    return ProviderError(429, details={"error": {"details": details}} if nested else details)


class EmbeddingQuotaTests(unittest.TestCase):
    def assert_quota_stop(self, error, expected):
        models = SequencedModels([error, error, error])
        sleeps = []
        adapter = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=3, sleep=sleeps.append)
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(["x"], gemini_profile())
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(len(models.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertNotIn("secret", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_daily_quota_stops_without_futile_retries(self):
        for violation in (
            {"quotaId": "EmbedContentRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "1000"},
            {"quotaMetric": "generativelanguage.googleapis.com/embed_content_free_tier_requests_per_day"},
        ):
            with self.subTest(violation=violation):
                self.assert_quota_stop(quota_error([violation]), "embedding_daily_quota_exceeded")

    def test_explicit_zero_quota_takes_priority_over_daily(self):
        for zero in (0, "0"):
            self.assert_quota_stop(quota_error([
                {"quotaId": "RequestsPerDay", "quotaValue": "1000"},
                {"quotaId": "RequestsPerMinute", "quotaValue": zero},
            ], nested=False), "embedding_quota_unavailable")

    def test_unknown_or_malformed_quota_does_not_guess_daily(self):
        for violations in (None, {}, [None], [{"quotaValue": False}],
                           [{"description": "secret RequestsPerDay 0"}],
                           [{"quotaId": "RequestsPerMinute", "quotaValue": "100"}]):
            error = quota_error(violations)
            models = SequencedModels([error, error, error])
            sleeps = []
            adapter = GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=3, sleep=sleeps.append)
            with self.assertRaises(VersioningError) as caught:
                adapter.embed(["x"], gemini_profile())
            self.assertEqual(caught.exception.code, "embedding_rate_limited")
            self.assertEqual(len(models.calls), 3)
            self.assertEqual(sleeps, [61.0, 61.0])

    def test_zero_retry_hint_still_has_positive_margin(self):
        response = SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0] + [0.0] * 767)])
        models = SequencedModels([ProviderError(429, headers={"Retry-After": "0"}), response])
        sleeps = []
        GeminiEmbedder(client=SimpleNamespace(models=models), max_attempts=2, sleep=sleeps.append).embed(["x"], gemini_profile())
        self.assertEqual(sleeps, [1.0])

    def test_longer_retry_info_is_not_overridden_by_short_header(self):
        self.assert_quota_stop(ProviderError(429, headers={"Retry-After": "1"}, details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "90s"}
        ]), "embedding_rate_limited")


if __name__ == "__main__":
    unittest.main()
