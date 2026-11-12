from __future__ import annotations

import json
import hashlib
import traceback
import unittest
from unittest.mock import patch

from api.knowledge.deployed_source import DeployedSourceVerifier
from api.knowledge.manifest import canonical_hash
from api.knowledge.ports import VersioningError

SITE = "https://portfolio.example"
PDF = b"%PDF-1.7\nvalid fixture"


def make_manifest(*, commit="a" * 40, deployment="deploy-1", attachment=None, text="Engineer"):
    metadata = {"section": "owner", "entity_id": "owner", "source_url": SITE,
                "owner": "portfolio", "title": "Résumé"}
    semantic = {"id": "portfolio:owner", "text": text, "metadata": metadata}
    document = {**semantic, "content_hash": canonical_hash(semantic)}
    return {"schema_version": 1, "source": {"site_url": SITE, "deployment_id": deployment,
            "commit_sha": commit}, "complete": True,
            "content_hash": canonical_hash([semantic]), "documents": [document],
            "attachments": [{"id": "resume", "title": "CV", "url": attachment or SITE + "/resume.pdf",
                             "media_type": "application/pdf"}]}


class Fetch:
    def __init__(self, manifests=None, pdf=PDF, pdf_type="application/pdf"):
        self.manifests = list(manifests or [make_manifest()])
        self.pdf, self.pdf_type, self.calls = pdf, pdf_type, []

    def __call__(self, url, *, limit):
        self.calls.append((url, limit))
        if url.endswith("/api/knowledge"):
            value = self.manifests.pop(0) if len(self.manifests) > 1 else self.manifests[0]
            return json.dumps(value).encode(), "application/json"
        return self.pdf, self.pdf_type


class DeployedSourceTests(unittest.TestCase):
    def setUp(self):
        self.blocker = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.blocker.start()

    def tearDown(self):
        self.blocker.stop()

    def test_inspect_fetches_manifest_twice_and_hashes_pdf(self):
        fetch = Fetch()
        receipt = DeployedSourceVerifier(SITE, fetch=fetch).inspect()
        self.assertEqual(receipt["portfolio_source"]["deployment_id"], "deploy-1")
        self.assertEqual(receipt["resume_source"]["pdf_sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(receipt["document_count"], 1)
        self.assertEqual([c[0] for c in fetch.calls], [SITE + "/api/knowledge", SITE + "/resume.pdf", SITE + "/api/knowledge"])

    def test_candidate_record_matches_receipt(self):
        fetch = Fetch()
        v = DeployedSourceVerifier(SITE, fetch=fetch)
        receipt = v.inspect()
        record = {"candidate_hash": "b" * 64, "source": {"assembly_input": "fetched-manifest", **receipt}}
        self.assertTrue(v(record))
        fetch = Fetch()
        v = DeployedSourceVerifier(SITE, fetch=fetch)
        verified = v.verify(record)
        self.assertEqual(verified, receipt)

    def test_manifest_or_pdf_change_is_rejected(self):
        for changed in (make_manifest(text="Changed semantic content"),
                        make_manifest(attachment=SITE + "/new-resume.pdf")):
            fetch = Fetch([make_manifest(), changed])
            with self.subTest(changed=changed["attachments"][0]["url"]), self.assertRaises(VersioningError) as caught:
                DeployedSourceVerifier(SITE, fetch=fetch).inspect()
            self.assertEqual(caught.exception.code, "source_changed")
        fetch = Fetch(pdf=b"%PDF-1.7 changed")
        v = DeployedSourceVerifier(SITE, fetch=fetch)
        rec = {"candidate_hash": "b" * 64, "source": {"assembly_input": "fetched-manifest",
               "portfolio_source": make_manifest()["source"], "portfolio_content_hash": make_manifest()["content_hash"],
               "resume_source": {"url": SITE + "/resume.pdf", "pdf_sha256": "0" * 64}}}
        with self.assertRaises(VersioningError) as caught:
            v(rec)
        self.assertEqual(caught.exception.code, "source_stale")

    def test_trusted_site_and_manifest_identity_are_required(self):
        for bad in ("http://portfolio.example", "https://localhost", "https://127.0.0.1", "https://8.8.8.8", "https://user@portfolio.example", "https://portfolio.example:444", "https://portfolio.example/path", "https://portfolio.example/\npath", "https://portfolio.example\\path"):
            with self.subTest(url=bad), self.assertRaises(VersioningError):
                DeployedSourceVerifier(bad, fetch=Fetch())
        for fields in ({"deployment": None}, {"commit": "abc123"}):
            value = make_manifest(**fields)
            with self.assertRaises(VersioningError) as caught:
                DeployedSourceVerifier(SITE, fetch=Fetch([value])).inspect()
            self.assertEqual(caught.exception.code, "missing_deployment_identity")

    def test_bad_candidate_local_url_and_untrusted_attachment_fail_before_fetch(self):
        fetch = Fetch()
        verifier = DeployedSourceVerifier(SITE, fetch=fetch)
        record = {"candidate_hash": "b" * 64, "source": {"assembly_input": "fetched-manifest",
                  "portfolio_source": {"site_url": "http://localhost"}, "resume_source": {"url": "http://localhost/x.pdf"}}}
        with self.assertRaises(VersioningError):
            verifier(record)
        self.assertEqual(fetch.calls, [])
        for value in (None, 7, [], {}, "bad"):
            with self.subTest(candidate_hash=value), self.assertRaises(VersioningError) as caught:
                verifier({"candidate_hash": value, "source": {}})
            self.assertEqual(caught.exception.code, "invalid_record")
        wrong_origin = {"candidate_hash": "b" * 64, "source": {"assembly_input": "fetched-manifest",
                        "portfolio_source": {"site_url": "https://other.example"},
                        "portfolio_content_hash": "a" * 64,
                        "resume_source": {"url": "https://other.example/resume.pdf", "pdf_sha256": "b" * 64}}}
        with self.assertRaises(VersioningError):
            verifier(wrong_origin)
        self.assertEqual(fetch.calls, [])
        bad = make_manifest(attachment="https://other.example/resume.pdf")
        fetch = Fetch([bad])
        with self.assertRaises(VersioningError):
            DeployedSourceVerifier(SITE, fetch=fetch).inspect()
        self.assertEqual(len(fetch.calls), 1)

    def test_incomplete_corrupt_and_oversized_pdf_and_fetch_failures(self):
        invalid = make_manifest()
        invalid["complete"] = False
        for fetch in (Fetch([invalid]), Fetch(pdf=b"not pdf"), Fetch(pdf=b"%PDF-" + b"x" * (5 * 1024 * 1024 + 1))):
            with self.assertRaises(VersioningError):
                DeployedSourceVerifier(SITE, fetch=fetch).inspect()
        with self.assertRaises(VersioningError) as caught:
            DeployedSourceVerifier(SITE, fetch=lambda *_a, **_k: (_ for _ in ()).throw(OSError("secret https://token"))).inspect()
        self.assertEqual(caught.exception.code, "manifest_fetch_failed")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", "".join(traceback.format_exception(caught.exception)))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_each_call_fetches_fresh_deployed_state(self):
        fetch = Fetch()
        verifier = DeployedSourceVerifier(SITE, fetch=fetch)
        record = {"candidate_hash": "b" * 64, "source": {"assembly_input": "fetched-manifest",
                  "portfolio_source": make_manifest()["source"],
                  "portfolio_content_hash": make_manifest()["content_hash"],
                  "resume_source": {"url": SITE + "/resume.pdf", "pdf_sha256": hashlib.sha256(PDF).hexdigest()}}}
        self.assertTrue(verifier(record))
        self.assertTrue(verifier(record))
        self.assertEqual(len(fetch.calls), 6)


if __name__ == "__main__":
    unittest.main()
