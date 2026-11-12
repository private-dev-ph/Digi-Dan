import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from api.knowledge.ports import VersioningError
from api.tools.assemble_knowledge import REPO_ROOT
from api.tools.knowledge_sync import _seed_reviewed_resume
from api.tools.prepare_resume import PIPELINE_VERSION, _canonical_hash, prepare_resume


PDF = b"%PDF-1.7 reviewed resume fixture"
SITE = "https://example.test"
MANIFEST_URL = SITE + "/api/knowledge"
GOOD_TEXT = "Experienced software engineer with strong Python, cloud, data, product delivery, leadership and communication skills."
CHANGED_TEXT = "Experienced software engineer with Python, cloud, data, product delivery, leadership and communication skills."


def extraction(text=GOOD_TEXT):
    return {"page_count": 1, "pages": [{"page_number": 1, "text": text,
            "method": "text", "ocr_mean_confidence": None}],
            "extractor": {"policy_version": "2", "pypdf_version": "6.10.0", "ocr_engine": None}}


def reviewed_snapshot(*, pdf=PDF, url=SITE + "/resume.pdf", text=GOOD_TEXT):
    pages = extraction(text)["pages"]
    return {"schema_version": 1, "kind": "resume-extraction", "review_status": "pending",
            "pipeline_version": PIPELINE_VERSION,
            "source": {"url": url, "pdf_sha256": hashlib.sha256(pdf).hexdigest(), "byte_length": len(pdf)},
            "content_hash": _canonical_hash(pages), "pages": pages, "page_count": len(pages),
            "extractor": extraction(text)["extractor"]}


class Downloader:
    def __init__(self, *, pdf=PDF, url=SITE + "/resume.pdf"):
        self.pdf, self.url, self.calls = pdf, url, []
        self.manifest = json.dumps({"schema_version": 1, "complete": True,
            "source": {"site_url": SITE}, "attachments": [{"id": "resume", "title": "Resume",
            "url": url, "media_type": "application/pdf"}]}).encode("utf-8")

    def __call__(self, url, *, limit, allow_local_http=False):
        self.calls.append(url)
        if url == MANIFEST_URL:
            return self.manifest, "application/json"
        if url == self.url:
            return self.pdf, "application/pdf"
        raise AssertionError("unexpected download URL")


class ReviewedResumeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "resume-state"
        self.seed = self.root / "reviewed-resume.json"
        self.policy = self.root / "source-policy.json"
        self.snapshot = reviewed_snapshot()
        self.write_seed(self.snapshot)

    def tearDown(self):
        self.temp.cleanup()

    def write_seed(self, value):
        self.seed.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.policy.write_text(json.dumps({"resume_content_hash": value.get("content_hash")}), encoding="utf-8")

    def seed_cache(self, site=SITE):
        return _seed_reviewed_resume(self.state, site, reviewed_path=self.seed, policy_path=self.policy)

    def test_repository_review_artifact_matches_source_policy(self):
        seeded = _seed_reviewed_resume(self.state, "https://zachcodes.vercel.app")
        artifact = json.loads(seeded.read_text(encoding="utf-8"))
        policy = json.loads((REPO_ROOT / "knowledge" / "source-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(artifact["content_hash"], policy["resume_content_hash"])
        self.assertEqual(artifact["pipeline_version"], PIPELINE_VERSION)
        self.assertEqual(artifact["source"]["pdf_sha256"], "60eba1cea663de53bcb403aaf75300050196331448289d670bf84939ac42ccda")

    def test_identical_live_pdf_reuses_reviewed_text_but_still_fetches_sources(self):
        seeded = self.seed_cache()
        before = json.loads(seeded.read_text(encoding="utf-8"))
        downloader = Downloader()
        extracted = []
        status = prepare_resume(state_dir=self.state, manifest_url=MANIFEST_URL, downloader=downloader,
                                extractor=lambda *a, **k: extracted.append(True) or extraction())
        after = json.loads(seeded.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "unchanged")
        self.assertEqual(downloader.calls, [MANIFEST_URL, SITE + "/resume.pdf"])
        self.assertEqual(extracted, [])
        self.assertEqual(after["content_hash"], before["content_hash"])
        self.assertTrue((self.state / "status.json").is_file())

    def test_changed_live_pdf_runs_extraction_and_does_not_reuse_old_text(self):
        self.seed_cache()
        changed_pdf = PDF + b" changed"
        downloader = Downloader(pdf=changed_pdf)
        calls = []
        status = prepare_resume(state_dir=self.state, manifest_url=MANIFEST_URL, downloader=downloader,
                                extractor=lambda data, **kw: calls.append(data) or extraction(CHANGED_TEXT))
        stored = json.loads((self.state / "last-successful.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "changed")
        self.assertEqual(calls, [changed_pdf])
        self.assertEqual(stored["pages"][0]["text"], CHANGED_TEXT)
        self.assertEqual(stored["source"]["pdf_sha256"], hashlib.sha256(changed_pdf).hexdigest())

    def test_changed_attachment_url_forces_extraction(self):
        self.seed_cache()
        url = SITE + "/files/resume.pdf"
        downloader = Downloader(url=url)
        calls = []
        prepare_resume(state_dir=self.state, manifest_url=MANIFEST_URL, downloader=downloader,
                       extractor=lambda data, **kw: calls.append(data) or extraction())
        self.assertEqual(calls, [PDF])
        self.assertEqual(downloader.calls, [MANIFEST_URL, url])

    def test_tampered_seed_or_policy_hash_fails_closed(self):
        tampered = dict(self.snapshot)
        tampered["content_hash"] = "0" * 64
        self.write_seed(tampered)
        with self.assertRaises(VersioningError) as caught:
            self.seed_cache()
        self.assertEqual(caught.exception.code, "reviewed_resume_invalid")
        self.assertFalse(self.state.exists())

        self.write_seed(self.snapshot)
        self.policy.write_text(json.dumps({"resume_content_hash": "0" * 64}), encoding="utf-8")
        with self.assertRaises(VersioningError) as caught:
            self.seed_cache()
        self.assertEqual(caught.exception.code, "reviewed_resume_invalid")
        self.assertFalse(self.state.exists())

    def test_seed_requires_current_pipeline_and_same_source_origin(self):
        stale = dict(self.snapshot, pipeline_version="stale")
        self.write_seed(stale)
        with self.assertRaises(VersioningError):
            self.seed_cache()
        self.write_seed(reviewed_snapshot(url="https://other.test/resume.pdf"))
        with self.assertRaises(VersioningError):
            self.seed_cache()
        self.write_seed(self.snapshot)
        with self.assertRaises(VersioningError):
            _seed_reviewed_resume(self.state, "https://another.test", reviewed_path=self.seed,
                                  policy_path=self.policy)
        self.assertFalse(self.state.exists())

    def test_existing_local_snapshot_is_never_overwritten_by_seed(self):
        self.state.mkdir(parents=True)
        destination = self.state / "last-successful.json"
        destination.write_bytes(b"local state")
        self.assertEqual(self.seed_cache(), destination)
        self.assertEqual(destination.read_bytes(), b"local state")


if __name__ == "__main__":
    unittest.main()
