import copy
import hashlib
import json
import unittest

from api.knowledge.manifest import (
    KnowledgeValidationError,
    canonical_hash,
    validate_portfolio_manifest,
)


SITE = "https://portfolio.example"
META = {"section": "owner", "entity_id": "owner", "source_url": SITE, "owner": "portfolio", "title": "Résumé"}


def make_manifest():
    semantic = {"id": "portfolio:owner", "text": "Développeuse 🚀", "metadata": dict(META)}
    doc = {**semantic, "content_hash": canonical_hash(semantic)}
    return {
        "schema_version": 1,
        "source": {"site_url": SITE, "deployment_id": None, "commit_sha": "abc123"},
        "complete": True,
        "content_hash": canonical_hash([semantic]),
        "documents": [doc],
        "attachments": [{"id": "resume", "title": "CV", "url": SITE + "/resume.pdf", "media_type": "application/pdf"}],
    }


class KnowledgeManifestTests(unittest.TestCase):
    def validate(self, value):
        return validate_portfolio_manifest(json.dumps(value, ensure_ascii=False).encode(), site_url=SITE)

    def test_unicode_hash_matches_typescript_json_stringify_fixture(self):
        value = {"é": "🚀", "a": [1, True, None]}
        expected = hashlib.sha256('{"a":[1,true,null],"é":"🚀"}'.encode()).hexdigest()
        self.assertEqual(canonical_hash(value), expected)

    def test_valid_manifest_and_document_order_independence(self):
        manifest = make_manifest()
        self.assertEqual(self.validate(manifest), manifest)
        extra = copy.deepcopy(manifest)
        sem2 = {"id": "portfolio:skills", "text": "Python", "metadata": {**META, "section": "skills", "entity_id": "skills", "title": "Skills"}}
        extra["documents"].append({**sem2, "content_hash": canonical_hash(sem2)})
        extra["content_hash"] = canonical_hash(sorted([sem2, {k: v for k, v in extra["documents"][0].items() if k != "content_hash"}], key=lambda x: x["id"]))
        extra["documents"].reverse()
        self.validate(extra)

    def test_rejects_duplicate_ids_bad_hash_and_schema_bool(self):
        manifest = make_manifest()
        cases = []
        duplicate = copy.deepcopy(manifest)
        duplicate["documents"].append(copy.deepcopy(duplicate["documents"][0]))
        cases.append(duplicate)
        bad_hash = copy.deepcopy(manifest)
        bad_hash["documents"][0]["content_hash"] = "0" * 64
        cases.append(bad_hash)
        bool_version = copy.deepcopy(manifest)
        bool_version["schema_version"] = True
        cases.append(bool_version)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(KnowledgeValidationError):
                self.validate(case)

    def test_rejects_duplicate_json_keys_and_nonfinite_values(self):
        for body in (b'{"x":1,"x":2}', b'{"x":NaN}'):
            with self.subTest(body=body), self.assertRaises(KnowledgeValidationError):
                validate_portfolio_manifest(body, site_url=SITE)

    def test_rejects_source_mismatch_incomplete_bad_url_and_size(self):
        wrong_origin = make_manifest()
        wrong_origin["source"]["site_url"] = "https://other.example"
        incomplete = make_manifest()
        incomplete["complete"] = False
        bad_url = make_manifest()
        bad_url["attachments"][0]["url"] = "/resume.pdf"
        too_large = b" " * (1024 * 1024 + 1)
        for case in (wrong_origin, incomplete, bad_url):
            with self.subTest(case=case), self.assertRaises(KnowledgeValidationError):
                self.validate(case)
        with self.assertRaises(KnowledgeValidationError):
            validate_portfolio_manifest(too_large, site_url=SITE)

    def test_rejects_document_source_mismatch_and_bounds(self):
        manifest = make_manifest()
        manifest["documents"][0]["metadata"]["source_url"] = "https://other.example/x"
        with self.assertRaises(KnowledgeValidationError):
            self.validate(manifest)
        manifest = make_manifest()
        manifest["documents"][0]["text"] = "x" * 30001
        with self.assertRaises(KnowledgeValidationError):
            self.validate(manifest)


if __name__ == "__main__":
    unittest.main()
