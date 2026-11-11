"""Quota classification uses structured provider details, never exception text."""
import unittest
from types import SimpleNamespace
from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.ports import VersioningError
from tests.test_stage3_cloud_embeddings import ProviderError, SequencedModels


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
