"""Persistence and input-protection tests for the offline assembly command."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.knowledge.manifest import KnowledgeValidationError, canonical_hash
from api.tools.assemble_knowledge import build_candidate


class AssemblyCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.site = "https://portfolio.example"
        metadata = {"section": "owner", "entity_id": "owner", "source_url": self.site,
                    "owner": "portfolio", "title": "Profile"}
        doc = {"id": "portfolio:owner", "text": "A current portfolio fact.", "metadata": metadata}
        self.manifest = {"schema_version": 1, "complete": True,
                         "source": {"site_url": self.site, "commit_sha": None, "deployment_id": None},
                         "content_hash": canonical_hash([doc]),
                         "documents": [{**doc, "content_hash": canonical_hash(doc)}],
                         "attachments": [{"id": "resume", "title": "Resume", "url": self.site + "/resume.pdf",
                                          "media_type": "application/pdf"}]}
        self.candidate = {"kind": "knowledge-candidate", "complete": True, "review_status": "policy-validated",
                          "content_hash": canonical_hash([doc]), "chunks": [doc], "provenance": {}}
        self.write("manifest.json", self.manifest)
        self.write("resume.json", {"kind": "resume-extraction"})
        self.write("status.json", {"status": "changed", "last_successful_available": True})
        self.write("policy.json", {})
        self.write("legacy.json", {})
        self.assembler = patch("api.tools.assemble_knowledge.assemble_knowledge", side_effect=lambda *a, **kw: copy.deepcopy(self.candidate)).start()
        self.addCleanup(patch.stopall)

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def run_build(self, **overrides):
        args = dict(output_dir=self.output, site_url=self.site, manifest_path=self.root / "manifest.json",
                    resume_path=self.root / "resume.json", policy_path=self.root / "policy.json",
                    legacy_path=self.root / "legacy.json")
        args.update(overrides)
        return build_candidate(**args)

    def test_success_then_noop_preserves_semantics_and_updates_provenance(self):
        self.assertEqual(self.run_build()["status"], "changed")
        self.assertEqual(self.run_build()["status"], "unchanged")
        output = json.loads((self.output / "last-successful.json").read_text())
        self.assertEqual(output["provenance"]["assembly_input"], "local-manifest")
        self.assertNotIn("documents", output)

    def test_invalid_manifest_keeps_previous_candidate_and_marks_failed(self):
        self.run_build()
        path = self.output / "last-successful.json"
        previous = path.read_bytes()
        self.manifest["complete"] = False
        self.write("manifest.json", self.manifest)
        with self.assertRaises(KnowledgeValidationError):
            self.run_build()
        self.assertEqual(path.read_bytes(), previous)
        status = json.loads((self.output / "status.json").read_text())
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["last_successful_available"])

    def test_failed_resume_attempt_cannot_reuse_retained_snapshot(self):
        self.write("status.json", {"status": "failed", "last_successful_available": True})
        with self.assertRaises(KnowledgeValidationError) as raised:
            self.run_build()
        self.assertEqual(raised.exception.code, "resume_preparation_failed")
        self.assembler.assert_not_called()
        self.assertFalse((self.output / "last-successful.json").exists())

    def test_changed_review_policy_failure_keeps_previous_candidate(self):
        self.run_build()
        path = self.output / "last-successful.json"
        previous = path.read_bytes()
        self.assembler.side_effect = KnowledgeValidationError("resume_review_required")
        with self.assertRaises(KnowledgeValidationError):
            self.run_build()
        self.assertEqual(path.read_bytes(), previous)

    def test_existing_lock_leaves_state_untouched(self):
        self.run_build()
        before = (self.output / "status.json").read_bytes()
        (self.output / ".assemble.lock").write_text("busy")
        with self.assertRaises(KnowledgeValidationError) as raised:
            self.run_build()
        self.assertEqual(raised.exception.code, "state_locked")
        self.assertEqual((self.output / "status.json").read_bytes(), before)
        self.assertTrue((self.output / ".assemble.lock").exists())

    def test_output_cannot_overwrite_resume_status(self):
        before = (self.root / "status.json").read_bytes()
        with self.assertRaises(KnowledgeValidationError) as raised:
            self.run_build(output_dir=self.root)
        self.assertEqual(raised.exception.code, "output_conflict")
        self.assertEqual((self.root / "status.json").read_bytes(), before)

    def test_manifest_origin_checked_before_network(self):
        with patch("api.tools.assemble_knowledge.resume_source.fetch_bytes") as fetch:
            with self.assertRaises(KnowledgeValidationError) as raised:
                self.run_build(manifest_path=None, manifest_url="https://different.example/api/knowledge")
        self.assertEqual(raised.exception.code, "origin_mismatch")
        fetch.assert_not_called()

    def test_duplicate_policy_keys_rejected(self):
        (self.root / "policy.json").write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaises(ValueError):
            self.run_build()
        self.assembler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
