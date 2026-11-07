import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api.knowledge.manifest import KnowledgeValidationError, canonical_hash
from api.knowledge.ports import VersioningError
from api.tools import knowledge_production_check as cli


class FakeVerifier:
    receipt = {
        "portfolio_source": {"site_url": "https://example.com", "commit_sha": "a" * 40,
                             "deployment_id": "deploy-1"},
        "portfolio_content_hash": "b" * 64,
        "resume_source": {"url": "https://example.com/resume.pdf", "pdf_sha256": "c" * 64},
        "document_count": 1,
    }
    failure = None
    records = []
    inspect_calls = 0
    verify_calls = 0
    verified_receipt = {**receipt, "portfolio_content_hash": "d" * 64}

    def __init__(self, site_url, manifest_url=None):
        self.site_url = site_url
        self.manifest_url = manifest_url

    def inspect(self):
        type(self).inspect_calls += 1
        if self.failure:
            raise self.failure
        return self.receipt

    def verify(self, record):
        type(self).verify_calls += 1
        self.records.append(record)
        if self.failure:
            raise self.failure
        return self.verified_receipt

    def __call__(self, record):
        self.records.append(record)
        if self.failure:
            raise self.failure
        return True


def candidate_data():
    chunks = [{"id": "chunk-1", "text": "safe fixture", "metadata": {}}]
    return {"kind": "knowledge-candidate", "complete": True,
            "review_status": "policy-validated", "chunks": chunks,
            "content_hash": canonical_hash(chunks),
            "provenance": {"assembly_input": "fetched-manifest", "portfolio": {"source": "x"}}}


class ProductionCheckTests(unittest.TestCase):
    def setUp(self):
        FakeVerifier.failure = None
        FakeVerifier.records = []
        FakeVerifier.inspect_calls = 0
        FakeVerifier.verify_calls = 0

    def test_source_only_report_has_receipt(self):
        with patch.object(cli, "DeployedSourceVerifier", FakeVerifier):
            report = cli.check("https://example.com")
        self.assertEqual(report["status"], "source_ready")
        self.assertEqual(report["receipt"], FakeVerifier.receipt)

    def test_candidate_checks_hash_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            candidate = candidate_data()
            path.write_text(json.dumps(candidate), encoding="utf-8")
            with patch.object(cli, "DeployedSourceVerifier", FakeVerifier):
                report = cli.check("https://example.com", candidate_path=path)
        self.assertEqual(report["status"], "candidate_sources_match")
        self.assertEqual(report["receipt"], FakeVerifier.verified_receipt)
        self.assertEqual(FakeVerifier.verify_calls, 1)
        self.assertEqual(FakeVerifier.inspect_calls, 0)
        self.assertEqual(FakeVerifier.records, [{"candidate_hash": candidate["content_hash"],
                                                 "source": candidate["provenance"]}])

    def test_stale_source_failure_is_sanitized(self):
        FakeVerifier.failure = VersioningError("source_stale", "secret diagnostic")
        output = io.StringIO()
        with patch.object(cli, "DeployedSourceVerifier", FakeVerifier), contextlib.redirect_stdout(output):
            result = cli.main(["--site-url", "https://example.com"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "not_ready", "error_code": "source_stale"})
        self.assertNotIn("secret", output.getvalue())

    def test_unexpected_failure_is_generic(self):
        FakeVerifier.failure = RuntimeError("token=private")
        output = io.StringIO()
        with patch.object(cli, "DeployedSourceVerifier", FakeVerifier), contextlib.redirect_stdout(output):
            result = cli.main(["--site-url", "https://example.com"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "production_check_failed")
        self.assertNotIn("private", output.getvalue())

    def test_invalid_candidate_and_output_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            with patch.object(cli, "DeployedSourceVerifier", FakeVerifier):
                with self.assertRaises(KnowledgeValidationError):
                    cli.check("https://example.com", candidate_path=path)
                self.assertEqual(FakeVerifier.inspect_calls, 0)
                self.assertEqual(FakeVerifier.verify_calls, 0)
                original = path.read_text(encoding="utf-8")
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = cli.main(["--site-url", "https://example.com", "--candidate", str(path),
                                       "--output", str(path)])
                self.assertEqual(result, 1)
                self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_output_is_json_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "reports" / "check.json"
            output = io.StringIO()
            with patch.object(cli, "DeployedSourceVerifier", FakeVerifier), contextlib.redirect_stdout(output):
                result = cli.main(["--site-url", "https://example.com", "--output", str(target)])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), json.loads(output.getvalue()))

    def test_output_write_failure_is_sanitized(self):
        output = io.StringIO()
        with patch.object(cli, "DeployedSourceVerifier", FakeVerifier), \
                patch.object(cli, "_atomic_json", side_effect=OSError("private path")), \
                contextlib.redirect_stdout(output):
            result = cli.main(["--site-url", "https://example.com", "--output", "report.json"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "not_ready", "error_code": "report_write_failed"})
        self.assertNotIn("private", output.getvalue())


if __name__ == "__main__":
    unittest.main()
