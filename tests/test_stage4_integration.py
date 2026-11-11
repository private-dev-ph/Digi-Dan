"""Coordinator integration against real Stage3 lifecycle and SyncJournal."""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.knowledge.manifest import canonical_hash
from api.knowledge.production_rollout import ProductionRollout
from api.knowledge.sync_journal import SyncJournal
from api.tools import knowledge_production, knowledge_sync
from tests.test_stage3_production_staging import MutableVerifier, make_stager
from tests.test_stage3_versioning import PROBES, candidate as base_candidate


SITE = "https://zachcodes.vercel.app"


def make_candidate(ids: tuple[str, ...], label: str) -> dict:
    """Build a valid candidate with deterministic IDs and existing test provenance."""
    rows = []
    for index, entity_id in enumerate(ids):
        metadata = {
            "title": entity_id.title(), "section": "projects", "entity_id": entity_id,
            "owner": "portfolio", "source_url": f"https://example.test/{entity_id}",
            "parent_id": entity_id, "chunk_index": 1, "chunk_count": 1,
        }
        row = {"id": entity_id, "text": f"{label}: approved facts for {entity_id}.", "metadata": metadata}
        row["content_hash"] = canonical_hash(row)
        rows.append(row)
    payload = [{"id": row["id"], "text": row["text"], "metadata": row["metadata"]} for row in rows]
    result = copy.deepcopy(base_candidate())
    result["chunks"] = rows
    result["content_hash"] = canonical_hash(payload)
    return result


class CoordinatorStage4IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.verifier = MutableVerifier()
        self.stager, self.registry, self.index, _ = make_stager(verifier=self.verifier)
        self.service = self.stager._service
        self.profile = self.stager._embedding_profile
        self.current_candidate = make_candidate(("alpha",), "initial")
        self.activation_calls = []
        self.after_stage = None
        self.journal = SyncJournal(self.registry)
        self.temp = tempfile.TemporaryDirectory(prefix="stage4-coordinator-")
        self.addCleanup(self.temp.cleanup)

        self.env = patch.dict(os.environ, {
            "KNOWLEDGE_ENVIRONMENT": "production",
            "KNOWLEDGE_MODE": "versioned",
            "KNOWLEDGE_SYNC_ENABLED": "true",
            "UPSTASH_REDIS_REST_URL": "https://redis.upstash.io",
            "UPSTASH_REDIS_REST_TOKEN": "worker-token",
            "UPSTASH_REDIS_REST_READ_ONLY_TOKEN": "reader-token",
            "UPSTASH_VECTOR_REST_URL": "https://vector.upstash.io",
            "UPSTASH_VECTOR_REST_TOKEN": "vector-token",
            "GEMINI_API_KEY": "gemini-test-key",
            "GEMINI_EMBEDDING_MODEL": "gemini-embedding-001",
            "EMBEDDING_DIMENSIONS": "768",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        self.actual_verify = knowledge_production.verify
        self.patches = [
            patch.object(knowledge_sync, "_candidate", side_effect=self._candidate),
            patch.object(knowledge_sync, "_journal", return_value=self.journal),
            patch.object(knowledge_sync, "_status", side_effect=self._status),
            patch.object(knowledge_production, "stage", side_effect=self._stage),
            patch.object(knowledge_production, "activate", side_effect=self._activate),
            patch.object(knowledge_production, "verify", side_effect=self._verify),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def _candidate(self, site_url, state_dir, tesseract_cmd="tesseract"):
        candidate_path = Path(state_dir) / "candidate.json"
        resume_path = Path(state_dir) / "resume.json"
        return candidate_path, resume_path, self.current_candidate, PROBES

    def _status(self, runtime):
        _, state = self.registry.read()
        active = state["versions"].get(state["active"]) if state.get("active") else None
        return {
            "generation": state["generation"],
            "active": copy.deepcopy(active),
            "desired": state["desired"],
        }

    def _stage(self, worker, *, candidate_path, site_url, expected_generation, resume_path, probes_path):
        result = self.stager.stage(self.current_candidate, expected_generation=expected_generation, probes=PROBES)
        if self.after_stage:
            callback, self.after_stage = self.after_stage, None
            callback()
        return result

    def _activate(self, worker, *, ticket, candidate_path, site_url, expected_generation, resume_path):
        rollout = ProductionRollout(self.service, self.profile, self.verifier)
        result = rollout.activate(ticket, self.current_candidate, expected_generation=expected_generation)
        self.activation_calls.append(ticket)
        return result

    def _verify(self, runtime, *, ticket, site_url, **kwargs):
        with patch("api.knowledge.cloud.create_cloud_service", return_value=self.service), \
             patch("api.knowledge.deployed_source.DeployedSourceVerifier", return_value=self.verifier):
            return self.actual_verify(runtime, ticket=ticket, site_url=site_url, **kwargs)

    def run_sync(self, run_id):
        return knowledge_sync.run("sync", SITE, Path(self.temp.name), None, "manual", run_id)

    def test_first_sync_updates_journal_and_active_version_consistently(self):
        report = self.run_sync("integration-first")
        status = self._status(None)
        journal = self.journal.read()
        self.assertEqual(report["status"], "updated")
        self.assertTrue(report["activation_performed"])
        self.assertEqual(status["active"]["ticket"], report["ticket"])
        self.assertEqual(status["active"]["version_id"], report["version_id"])
        self.assertEqual(journal["latest"]["status"], "updated")
        self.assertEqual(journal["latest"]["ticket"], status["active"]["ticket"])
        self.assertEqual(journal["latest"]["version_id"], status["active"]["version_id"])
        self.assertEqual(journal["latest"]["candidate_hash"], report["candidate_hash"])

    def test_identical_candidate_is_unchanged_without_namespace_or_activation(self):
        first = self.run_sync("integration-first")
        namespace_count = len(self.index.rows)
        activations = len(self.activation_calls)
        with patch.object(self.service.embedder, "embed", side_effect=AssertionError("unexpected embedding")) as embed, \
             patch.object(self.service.vectors, "query", wraps=self.service.vectors.query) as query:
            result = self.run_sync("integration-same")
        embed.assert_not_called()
        self.assertEqual(query.call_count, 2 * len(PROBES))
        self.assertEqual(result["verification"]["stage"]["mode"], "cached")
        self.assertEqual(result["verification"]["runtime"]["embedding_inputs"], 0)
        self.assertEqual(first["ticket"], result["ticket"])
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(len(self.index.rows), namespace_count)
        self.assertEqual(len(self.activation_calls), activations)
        self.assertEqual(self.journal.read()["latest"]["status"], "unchanged")

    def test_candidate_removal_drops_removed_ids_from_active_content(self):
        self.current_candidate = make_candidate(("alpha", "beta"), "two-source")
        initial = self.run_sync("integration-two-ids")
        initial_record = self.service.get_active()
        self.assertEqual(set(initial_record["expected_ids"]), {"alpha", "beta"})

        self.current_candidate = make_candidate(("alpha",), "one-source")
        updated = self.run_sync("integration-remove-beta")
        active = self.service.get_active()
        stored_ids = {row["id"] for row in self.service.vectors.scan(active["namespace"])}
        self.assertNotEqual(initial["ticket"], updated["ticket"])
        self.assertEqual(active["ticket"], updated["ticket"])
        self.assertEqual(active["expected_ids"], ["alpha"])
        self.assertEqual(stored_ids, {"alpha"})

    def test_source_failure_before_activation_preserves_old_active_and_finishes_failed_journal(self):
        baseline = self.run_sync("integration-baseline")
        self.current_candidate = make_candidate(("alpha",), "changed")
        self.after_stage = lambda: setattr(self.verifier, "current", False)
        with self.assertRaises(knowledge_sync._ReportedFailure) as caught:
            self.run_sync("integration-source-failure")

        state = self._status(None)
        journal = self.journal.read()
        self.assertEqual(caught.exception.report["error_code"], "source_stale")
        self.assertEqual(state["active"]["ticket"], baseline["ticket"])
        self.assertEqual(self.activation_calls, [baseline["ticket"]])
        self.assertEqual(journal["latest"]["status"], "failed")
        self.assertEqual(journal["latest"]["error_code"], "source_stale")

    def test_newer_generation_after_stage_is_reported_superseded(self):
        baseline = self.run_sync("integration-baseline")
        self.current_candidate = make_candidate(("alpha",), "older-job")
        newer_candidate = make_candidate(("alpha",), "newer-job")

        def stage_newer_generation():
            generation = self.registry.read()[1]["generation"]
            self.service.sync(newer_candidate, self.profile, expected_generation=generation,
                              verify_source=self.verifier, probes=PROBES, activate=False)

        self.after_stage = stage_newer_generation
        with self.assertRaises(knowledge_sync._ReportedFailure) as caught:
            self.run_sync("integration-stale-job")

        state = self.registry.read()[1]
        journal = self.journal.read()
        self.assertEqual(caught.exception.report["status"], "superseded")
        self.assertEqual(state["versions"][state["active"]]["ticket"], baseline["ticket"])
        self.assertNotEqual(state["desired"], baseline["ticket"])
        self.assertEqual(journal["latest"]["status"], "superseded")
        self.assertEqual(journal["latest"]["error_code"], "generation_conflict")
        self.assertEqual(self.activation_calls, [baseline["ticket"]])


if __name__ == "__main__":
    unittest.main()
