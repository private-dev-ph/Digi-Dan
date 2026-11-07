import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from api.knowledge import resume_source
from api.knowledge.resume_source import SourceError
from api.tools.prepare_resume import PreparationError, prepare_resume

PDF = b"%PDF-1.7 sample"
GOOD_TEXT = "Experienced software engineer with strong Python, cloud, data, product delivery, leadership and communication skills."
EXTRACT = {"page_count": 1, "pages": [{"page_number": 1, "text": GOOD_TEXT, "method": "text", "ocr_mean_confidence": None}], "extractor": {"policy_version": "2", "pypdf_version": "6.10.0", "ocr_engine": None}}
MANIFEST = {"schema_version": 1, "complete": True, "source": {"site_url": "https://example.test"}, "attachments": [{"id": "resume", "title": "CV", "url": "https://example.test/resume.pdf", "media_type": "application/pdf"}]}
MANIFEST_URL = "https://example.test/api/knowledge"


class Downloader:
    def __init__(self, manifest=None, pdf=PDF, failure=None):
        self.manifest = json.dumps(manifest or MANIFEST).encode()
        self.pdf, self.failure, self.calls = pdf, failure, []

    def __call__(self, url, *, limit, allow_local_http=False):
        self.calls.append(url)
        if self.failure == url:
            raise SourceError("fetch_failed", "Source could not be fetched")
        if url.endswith("/api/knowledge"):
            return self.manifest, "application/json"
        return self.pdf, "application/pdf"


class ResumeSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_prepare(self, downloader=None, extractor=None):
        return prepare_resume(state_dir=self.state, manifest_url=MANIFEST_URL,
                              downloader=downloader or Downloader(), extractor=extractor or (lambda data, **kw: EXTRACT))

    def test_first_success_then_unchanged_still_fetches_pdf_and_skips_extraction(self):
        d, extracted = Downloader(), []
        self.assertEqual(self.run_prepare(d, lambda b, **k: extracted.append(b) or EXTRACT)["status"], "changed")
        before = (self.state / "last-successful.json").read_bytes()
        self.assertEqual(self.run_prepare(d, lambda b, **k: extracted.append(b) or EXTRACT)["status"], "unchanged")
        self.assertEqual(len(d.calls), 4)
        self.assertEqual(len(extracted), 1)
        self.assertEqual((self.state / "last-successful.json").read_bytes(), before)

    def test_changed_pdf_bytes_reextract(self):
        d, calls = Downloader(), []
        self.run_prepare(d, lambda b, **k: calls.append(b) or EXTRACT)
        d.pdf += b"changed"
        self.assertEqual(self.run_prepare(d, lambda b, **k: calls.append(b) or EXTRACT)["status"], "changed")
        self.assertEqual(len(calls), 2)

    def test_ocr_exception_preserves_prior_snapshot_and_hides_diagnostic(self):
        d = Downloader()
        self.run_prepare(d)
        d.pdf += b"new scan"
        path = self.state / "last-successful.json"
        previous = path.read_bytes()
        with self.assertRaises(PreparationError):
            self.run_prepare(d, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private text must not escape")))
        self.assertEqual(path.read_bytes(), previous)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("private text", json.dumps(status))
        self.assertTrue(status["last_successful_available"])

    def test_bad_pdf_and_404_preserve_snapshot(self):
        d = Downloader()
        self.run_prepare(d)
        path, previous = self.state / "last-successful.json", (self.state / "last-successful.json").read_bytes()
        for mode in ("pdf", "404"):
            with self.subTest(mode=mode):
                d.pdf = b"not PDF" if mode == "pdf" else PDF
                d.failure = "https://example.test/resume.pdf" if mode == "404" else None
                with self.assertRaises(PreparationError):
                    self.run_prepare(d)
                self.assertEqual(path.read_bytes(), previous)

    def test_incomplete_manifest_preserves_snapshot(self):
        d = Downloader()
        self.run_prepare(d)
        path, previous = self.state / "last-successful.json", (self.state / "last-successful.json").read_bytes()
        d.manifest = json.dumps({**MANIFEST, "complete": False}).encode()
        with self.assertRaises(PreparationError):
            self.run_prepare(d)
        self.assertEqual(path.read_bytes(), previous)

    def test_corrupt_cache_rebuilds_and_failed_rebuild_marks_unavailable(self):
        self.run_prepare()
        path = self.state / "last-successful.json"
        path.write_text("corrupt")
        self.assertEqual(self.run_prepare()["status"], "changed")
        path.write_text("corrupt")
        with self.assertRaises(PreparationError):
            self.run_prepare(extractor=lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        self.assertFalse(json.loads((self.state / "status.json").read_text())["last_successful_available"])

    def test_invalid_or_empty_extraction_preserves_snapshot(self):
        self.run_prepare()
        path, previous = self.state / "last-successful.json", (self.state / "last-successful.json").read_bytes()
        d = Downloader(pdf=PDF + b"new scan")
        invalid = [dict(EXTRACT, pages=[]),
                   {**EXTRACT, "pages": [{**EXTRACT["pages"][0], "text": "tiny"}]},
                   {**EXTRACT, "pages": [{**EXTRACT["pages"][0], "text": "D A N I E L Z A C H A R Y E X P E R I E N C E D S O F T W A R E E N G I N E E R"}]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PreparationError):
                self.run_prepare(downloader=d, extractor=lambda *a, **k: value)
            self.assertEqual(path.read_bytes(), previous)

    def test_lock_failure_does_not_write_status(self):
        status = self.state / "status.json"
        status.write_text("old status")
        (self.state / ".prepare.lock").write_text("busy")
        with self.assertRaises(PreparationError):
            self.run_prepare()
        self.assertEqual(status.read_text(), "old status")

    def test_manifest_schema_and_attachment_url_validation(self):
        no_url = {k: v for k, v in MANIFEST["attachments"][0].items() if k != "url"}
        for altered in ({**MANIFEST, "schema_version": True}, {**MANIFEST, "attachments": [no_url]},
                        {**MANIFEST, "attachments": [{**MANIFEST["attachments"][0], "url": "/resume.pdf"}]}):
            with self.subTest(altered=altered), self.assertRaises(SourceError):
                resume_source.parse_manifest(json.dumps(altered).encode())

    def test_rejects_credentials_query_fragment_control_and_backslash(self):
        urls = ("https://u:p@example.test/cv", "https://example.test/cv?q=1", "https://example.test/cv#f",
                "https://example.test/a\\b", "https://example.test/a\n", "http://example.test/cv")
        for url in urls:
            with self.subTest(url=url), self.assertRaises(SourceError):
                resume_source.validate_url(url, allow_local_http=True)

    def test_cross_origin_attachment_rejected(self):
        obj = json.loads(json.dumps(MANIFEST))
        obj["attachments"][0]["url"] = "https://elsewhere.test/resume.pdf"
        parsed = resume_source.parse_manifest(json.dumps(obj).encode())
        with self.assertRaises(SourceError):
            resume_source.validate_manifest_origins(MANIFEST_URL, parsed)

    def test_pdf_size_header_and_content_type_validation(self):
        for data, content_type in ((b"%PDF", "application/pdf"), (b"%PDF-" + b"x" * resume_source.MAX_PDF_BYTES, "application/pdf"), (PDF, "text/plain")):
            with self.subTest(content_type=content_type), self.assertRaises(SourceError):
                resume_source.validate_pdf(data, content_type)

    def test_real_fetch_rejects_non_200_and_enforces_size_limit(self):
        response = Mock(status=206, headers={"Content-Type": "application/pdf"})
        response.__enter__, response.__exit__ = Mock(return_value=response), Mock(return_value=False)
        with patch("api.knowledge.resume_source.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(SourceError):
                resume_source.fetch_bytes("https://example.test/file", limit=10)
        with patch("api.knowledge.resume_source.urllib.request.build_opener") as build:
            build.return_value.open.side_effect = urllib.error.HTTPError("https://example.test/file", 302, "redirect", {}, None)
            with self.assertRaisesRegex(SourceError, "Redirects"):
                resume_source.fetch_bytes("https://example.test/file", limit=10)
        response.status, response.read1 = 200, Mock(side_effect=[b"123456", b"78901"])
        with patch("api.knowledge.resume_source.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(SourceError):
                resume_source.fetch_bytes("https://example.test/file", limit=10)

    def test_snapshot_validation_uses_explicit_checks(self):
        self.run_prepare()
        import api.tools.prepare_resume as module
        snapshot = json.loads((self.state / "last-successful.json").read_text())
        snapshot["source"]["pdf_sha256"] = "z" * 64
        with self.assertRaises(PreparationError):
            module._validate_snapshot(json.dumps(snapshot).encode())


if __name__ == "__main__":
    unittest.main()
