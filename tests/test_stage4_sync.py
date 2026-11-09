import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.tools import knowledge_sync as sync
from api.knowledge.ports import VersioningError
from api.knowledge.versioning import _validate as actual_validate
from tests.test_stage3_versioning import PROFILE, PROBES, candidate as valid_candidate


class Stage4SyncTests(unittest.TestCase):
    def setUp(self):
        self.worker = object()
        self.runtime = object()
        self.candidate = {"content_hash": "a" * 64, "chunks": [{"id": "x"}], "provenance": {}}
        self.journal = Mock()
        self.journal.begin.return_value = 1
        self.journal.finish.return_value = True
        self.status = {"generation": 4, "active": {"version_id": "old"}}
        self.cloud_config = patch.object(sync, "cloud_configuration", return_value={"embedding_profile": {}})
        self.validate = patch.object(sync, "_validate", return_value=([], "a" * 64, "c" * 64))
        self.cloud_config.start()
        self.validate.start()
        self.addCleanup(self.validate.stop)
        self.addCleanup(self.cloud_config.stop)

    def patches(self):
        return [
            patch.object(sync, "_settings", return_value=(self.worker, self.runtime)),
            patch.object(sync, "_journal", return_value=self.journal),
            patch.object(sync, "_candidate", return_value=(Path("candidate"), Path("resume"), self.candidate, [])),
            patch.object(sync.production, "status", side_effect=lambda _: dict(self.status)),
            patch.object(sync.production, "stage", return_value={"status": "staged", "ticket": "a" * 16, "chunk_count": 1}),
            patch.object(sync.production, "activate", return_value={"activation_performed": True}),
            patch.object(sync.production, "verify", return_value={"version_id": "c" * 64}),
        ]

    def test_sync_stages_then_checks_generation_then_activates_and_verifies(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[3].side_effect = [dict(self.status), {"generation": 5, "active": {"version_id": "old"}}]
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            result = sync.run("sync", "https://example.test", Path("state"), None, "manual", "run-1")
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["activation_performed"])
        mocks[4].assert_called_once()
        self.assertEqual(mocks[4].call_args.kwargs["expected_generation"], 4)
        mocks[5].assert_called_once()
        self.assertEqual(mocks[5].call_args.kwargs["expected_generation"], 5)
        mocks[6].assert_called_once()

    def test_generation_that_moves_after_stage_is_not_adopted(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[3].side_effect = [dict(self.status), {"generation": 6, "active": {"version_id": "someone-else"}}]
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            with self.assertRaises(sync._ReportedFailure) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "manual", "run-1")
        self.assertEqual(caught.exception.report["error_code"], "generation_conflict")
        mocks[5].assert_not_called()

    def test_unchanged_stage_skips_activation(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[4].return_value = {"status": "unchanged", "ticket": "b" * 16, "chunk_count": 1}
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            result = sync.run("sync", "https://example.test", Path("state"), None, "manual", "run-1")
        self.assertEqual(result["status"], "unchanged")
        mocks[5].assert_not_called()
        mocks[6].assert_called_once()

    def test_post_activation_verify_failure_keeps_activation_truth(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[3].side_effect = [dict(self.status), {"generation": 5, "active": {"version_id": "old"}}]
        mocks[6].side_effect = RuntimeError("secret detail")
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            with self.assertRaises(sync._ReportedFailure) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "manual", "run-1")
        self.assertTrue(caught.exception.report["activation_performed"])
        self.assertEqual(caught.exception.report["error_code"], "sync_failed")
        self.assertNotIn("secret", repr(caught.exception.report))

    def test_source_failure_is_journaled_as_review_required(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[2].side_effect = type("E", (Exception,), {"code": "portfolio_review_required"})()
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            with self.assertRaises(sync._ReportedFailure) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "push", "run-1")
        self.assertEqual(caught.exception.report["status"], "review_required")
        self.journal.finish.assert_called_once_with(1, caught.exception.report)

    def test_check_is_read_only_and_reports_candidate_change(self):
        with patch.object(sync, "_settings", return_value=(self.worker, self.runtime)), \
             patch.object(sync, "preflight") as preflight, \
             patch.object(sync, "_status", return_value=self.status), \
             patch.object(sync, "_candidate", return_value=(Path("c"), Path("r"), self.candidate, [])), \
             patch.object(sync, "_validate", return_value=(None, None, "new")), \
             patch.object(sync, "cloud_configuration", return_value={"embedding_profile": {}}), \
             patch.object(sync, "DeployedSourceVerifier") as verifier, \
             patch.object(sync, "_create_cloud_stores") as stores, \
             patch("api.knowledge.cloud_embeddings.GeminiEmbedder") as embedder:
            result = sync.run("check", "https://example.test", Path("state"), None, "local", None)
        self.assertTrue(result["change_needed"])
        preflight.assert_called_once_with(self.worker, self.runtime, online=True)
        verifier.return_value.verify.assert_called_once()
        stores.assert_not_called()
        embedder.assert_not_called()

    def test_review_and_conflict_error_buckets(self):
        self.assertEqual(sync._error(type("E", (), {"code": "resume_review_required"})())["status"], "review_required")
        self.assertEqual(sync._error(type("E", (), {"code": "generation_conflict"})())["status"], "superseded")

    def test_missing_process_configuration_fails_with_safe_code(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(Exception) as caught:
                sync._settings(None)
        self.assertEqual(getattr(caught.exception, "code", None), "invalid_cloud_configuration")
        self.assertNotIn("TOKEN", str(caught.exception))

    def test_process_configuration_requires_distinct_read_only_token(self):
        values = {
            "UPSTASH_REDIS_REST_URL": "https://one.upstash.io", "UPSTASH_REDIS_REST_TOKEN": "worker",
            "UPSTASH_REDIS_REST_READ_ONLY_TOKEN": "worker", "UPSTASH_VECTOR_REST_URL": "https://two.upstash.io",
            "UPSTASH_VECTOR_REST_TOKEN": "vector", "GEMINI_API_KEY": "gemini",
            "GEMINI_EMBEDDING_MODEL": "gemini-embedding-001", "EMBEDDING_DIMENSIONS": "768",
            "KNOWLEDGE_ENVIRONMENT": "production",
        }
        with patch.dict("os.environ", values, clear=True):
            with self.assertRaises(VersioningError) as caught:
                sync._settings(None)
        self.assertEqual(caught.exception.code, "distinct_production_tokens_required")

    def test_disabled_sync_does_not_open_journal(self):
        with patch.object(sync, "_settings", return_value=(self.worker, self.runtime)), \
             patch.object(sync, "_journal") as journal, patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(VersioningError) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "local", None)
        self.assertEqual(caught.exception.code, "sync_disabled")
        journal.assert_not_called()

    def test_status_reads_journal_without_preparing_candidate(self):
        self.journal.read.return_value = {"attempt": 0}
        with patch.object(sync, "_settings", return_value=(self.worker, self.runtime)), \
             patch.object(sync, "_journal", return_value=self.journal), \
             patch.object(sync, "_status", return_value={"active": {"ticket": "a" * 16}}), \
             patch.object(sync, "_candidate") as candidate:
            report = sync.run("status", "https://example.test", Path("state"), None, "local", None)
        self.assertEqual(report["journal"], {"attempt": 0})
        candidate.assert_not_called()

    def test_default_cli_mode_is_check(self):
        with patch.object(sync, "run", return_value={"status": "check_ready"}) as run, \
             patch.object(sync, "_emit", return_value=0):
            self.assertEqual(sync.main(["--site-url", "https://example.test", "--state-dir", "state"]), 0)
        self.assertEqual(run.call_args.args[0], "check")

    def test_missing_probe_target_is_review_required_before_stage(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[3].side_effect = [dict(self.status)]
        bad_probes = [{**PROBES[0], "expected_ids": ["not-in-candidate"]}]
        mocks[2].return_value = (Path("candidate"), Path("resume"), valid_candidate(), bad_probes)
        with patch.object(sync, "_validate", side_effect=actual_validate), \
             patch.object(sync, "cloud_configuration", return_value={"embedding_profile": PROFILE}), \
             patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            with self.assertRaises(sync._ReportedFailure) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "push", "run-1")
        self.assertEqual(caught.exception.report["status"], "review_required")
        self.assertEqual(caught.exception.report["error_code"], "retrieval_probe_review_required")
        mocks[4].assert_not_called()

    def test_uncertain_activation_error_is_reported_without_retry(self):
        stack = self.patches()
        mocks = [p.start() for p in stack]
        self.addCleanup(lambda: [p.stop() for p in reversed(stack)])
        mocks[3].side_effect = [dict(self.status), {"generation": 5, "active": self.status["active"]},
                               {"generation": 6, "active": self.status["active"]}]
        mocks[5].side_effect = RuntimeError("write outcome unknown")
        with patch.dict("os.environ", {"KNOWLEDGE_SYNC_ENABLED": "true"}):
            with self.assertRaises(sync._ReportedFailure) as caught:
                sync.run("sync", "https://example.test", Path("state"), None, "manual", "run-1")
        self.assertTrue(caught.exception.report["activation_attempted"])
        self.assertTrue(caught.exception.report["activation_uncertain"])
        self.assertFalse(caught.exception.report["activation_performed"])
        mocks[5].assert_called_once()

    def test_malformed_exception_codes_are_sanitized(self):
        report = sync._error(type("E", (), {"code": "secret value"})())
        self.assertEqual(report, {"status": "failed", "error_code": "sync_failed"})


if __name__ == "__main__":
    unittest.main()
