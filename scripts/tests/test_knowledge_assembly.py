from __future__ import annotations

import copy
import unittest

from api.knowledge.assembly import assemble_knowledge, MAX_CHUNK_CHARS, portfolio_review_hash
from api.knowledge.manifest import KnowledgeValidationError, canonical_hash
from api.tools.prepare_resume import _canonical_hash

SITE = "https://example.test"
PAGE_TEXT = ("Daniel Zachary is an experienced software engineer with extensive experience in Python, "
             "FastAPI, cloud systems, and reliable backend development. He has delivered many production projects.")


def portfolio_manifest():
    metadata = {"section": "projects", "entity_id": "project:alpha", "source_url": SITE + "/projects/alpha", "owner": "portfolio", "title": "Alpha project"}
    item = {"id": "portfolio:project:alpha", "text": "The Alpha project used Python and FastAPI.", "metadata": metadata}
    item["content_hash"] = canonical_hash({k: v for k, v in item.items() if k != "content_hash"})
    semantic = [{k: v for k, v in item.items() if k != "content_hash"}]
    return {"schema_version": 1, "source": {"site_url": SITE, "deployment_id": None, "commit_sha": None},
            "complete": True, "content_hash": canonical_hash(semantic), "documents": [item],
            "attachments": [{"id": "resume", "title": "Resume", "url": SITE + "/resume.pdf", "media_type": "application/pdf"}]}


def resume_snapshot():
    pages = [{"page_number": 1, "text": PAGE_TEXT, "method": "text", "ocr_mean_confidence": None}]
    return {"schema_version": 1, "kind": "resume-extraction", "review_status": "pending", "pipeline_version": "2",
            "source": {"url": SITE + "/resume.pdf", "pdf_sha256": "a" * 64, "byte_length": 1000},
            "content_hash": _canonical_hash(pages), "pages": pages, "page_count": 1,
            "extractor": {"policy_version": "2", "pypdf_version": "1.0", "ocr_engine": None}}


def legacy_data():
    return {"documents": [{"id": "old:alpha", "text": "Earlier portfolio said Alpha was a Java project.", "metadata": {"section": "legacy"}}]}


def policy_for(resume, legacy):
    doc = legacy["documents"][0]
    return {"schema_version": 1, "policy_version": "2026-11", "resume_content_hash": resume["content_hash"],
            "portfolio_review_hashes": {"portfolio:project:alpha": portfolio_review_hash(portfolio_manifest()["documents"][0]), "portfolio:missing:entity": "0" * 64},
            "legacy_inventory": [{"id": doc["id"], "content_hash": canonical_hash(doc), "disposition": "replaced", "reason": "Current portfolio is authoritative."}],
            "supplemental_facts": [
                {"id": "supplement:resume:skill", "entity_id": "person:daniel", "fact_key": "skill:python", "section": "skills", "title": "Resume skill",
                 "text": "Daniel has extensive experience with Python.", "owner": "resume",
                 "sources": [{"kind": "resume", "page_number": 1, "quote": "extensive experience in Python"}],
                 "requires_portfolio_ids": [], "portfolio_wins": ["portfolio:project:alpha"]},
                {"id": "supplement:legacy:java", "entity_id": "project:alpha", "fact_key": "language:java", "section": "projects", "title": "Old project claim",
                 "text": "Alpha was a Java project.", "owner": "curated",
                 "sources": [{"kind": "legacy", "document_id": "old:alpha", "quote": "Alpha was a Java project"}],
                 "requires_portfolio_ids": ["portfolio:missing:entity"], "portfolio_wins": []}]}


def refresh_portfolio(portfolio):
    for item in portfolio["documents"]:
        item["content_hash"] = canonical_hash({k: v for k, v in item.items() if k != "content_hash"})
    portfolio["content_hash"] = canonical_hash(sorted(
        [{k: v for k, v in item.items() if k != "content_hash"} for item in portfolio["documents"]], key=lambda item: item["id"]))


class AssemblyTests(unittest.TestCase):
    def make_inputs(self):
        resume, legacy = resume_snapshot(), legacy_data()
        return portfolio_manifest(), resume, legacy, policy_for(resume, legacy)

    def test_conflicts_suppressed_and_only_portfolio_survives(self):
        result = assemble_knowledge(*self.make_inputs())
        self.assertEqual([c["id"] for c in result["chunks"]], ["portfolio:project:alpha"])
        self.assertEqual([x["status"] for x in result["audit"]["supplemental"]], ["suppressed", "suppressed"])
        self.assertNotIn("documents", result)

    def test_changed_resume_requires_review(self):
        p, r, l, pol = self.make_inputs()
        pol["resume_content_hash"] = "0" * 64
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "resume_review_required")

    def test_changed_or_new_legacy_rejected(self):
        for mutation in ("changed", "new"):
            p, r, l, pol = self.make_inputs()
            if mutation == "changed":
                l["documents"][0]["text"] += " Updated."
            else:
                l["documents"].append({"id": "old:new", "text": "New text", "metadata": {}})
            with self.subTest(mutation=mutation), self.assertRaises(KnowledgeValidationError) as cm:
                assemble_knowledge(p, r, l, pol)
            self.assertEqual(cm.exception.code, "legacy_inventory_mismatch")

    def test_bad_quote_rejected_even_when_fact_would_be_suppressed(self):
        p, r, l, pol = self.make_inputs()
        pol["supplemental_facts"][0]["sources"][0]["quote"] = "not on the page"
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "source_quote_missing")

    def test_unsuppressed_resume_fact_included(self):
        p, r, l, pol = self.make_inputs()
        pol["supplemental_facts"] = pol["supplemental_facts"][:1]
        pol["supplemental_facts"][0]["portfolio_wins"] = []
        result = assemble_knowledge(p, r, l, pol)
        self.assertEqual(len(result["chunks"]), 2)
        self.assertEqual(result["audit"]["counts"]["supplemental_included"], 1)
        self.assertEqual(result["chunks"][1]["metadata"]["source_url"], SITE + "/resume.pdf")

    def test_referenced_portfolio_fact_requires_unchanged_anchor(self):
        p, r, l, pol = self.make_inputs()
        fact = pol["supplemental_facts"][0]
        fact["requires_portfolio_ids"] = ["portfolio:project:alpha"]
        fact["portfolio_wins"] = []
        p["documents"][0]["text"] += " Updated factual detail."
        refresh_portfolio(p)
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "portfolio_review_required")

    def test_missing_anchor_suppresses_fact_but_unrelated_addition_does_not_invalidate(self):
        p, r, l, pol = self.make_inputs()
        fact = pol["supplemental_facts"][0]
        fact["requires_portfolio_ids"] = ["portfolio:project:alpha"]
        fact["portfolio_wins"] = []
        p["documents"][0]["metadata"]["source_url"] = SITE + "/projects/renamed-location"
        refresh_portfolio(p)
        result = assemble_knowledge(p, r, l, pol)
        self.assertEqual(next(x["status"] for x in result["audit"]["supplemental"] if x["id"] == fact["id"]), "included")
        extra = {"id": "portfolio:project:beta", "text": "A new unrelated project.",
                 "metadata": {"section": "projects", "entity_id": "project:beta", "source_url": SITE + "/projects/beta", "owner": "portfolio", "title": "Beta project"}}
        p["documents"].append(extra)
        refresh_portfolio(p)
        result = assemble_knowledge(p, r, l, pol)
        self.assertEqual(next(x["status"] for x in result["audit"]["supplemental"] if x["id"] == fact["id"]), "included")
        p["documents"][0]["id"] = "portfolio:project:gamma"
        refresh_portfolio(p)
        result = assemble_knowledge(p, r, l, pol)
        self.assertEqual(next(x["status"] for x in result["audit"]["supplemental"] if x["id"] == fact["id"]), "suppressed")

    def test_duplicate_fact_identity_rejected(self):
        p, r, l, pol = self.make_inputs()
        fact = copy.deepcopy(pol["supplemental_facts"][0])
        fact["id"] = "supplement:resume:other"
        pol["supplemental_facts"].append(fact)
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "duplicate_fact_identity")

    def test_deterministic_across_document_and_fact_order(self):
        p, r, l, pol = self.make_inputs()
        first = assemble_knowledge(p, r, l, pol)
        p["documents"].reverse()
        pol["supplemental_facts"].reverse()
        second = assemble_knowledge(p, r, l, pol)
        self.assertEqual(first, second)

    def test_chunks_bounded_and_preserve_source_content(self):
        p, r, l, pol = self.make_inputs()
        body = ("A substantial verified fact sentence. " * 100)
        pol["supplemental_facts"] = [copy.deepcopy(pol["supplemental_facts"][0])]
        f = pol["supplemental_facts"][0]
        f.update(id="supplement:resume:long", text=body, title="Long evidence")
        f["portfolio_wins"] = []
        result = assemble_knowledge(p, r, l, pol)
        chunks = [c for c in result["chunks"] if c["id"].startswith("supplement:resume:long")]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c["text"]) <= MAX_CHUNK_CHARS for c in chunks))
        self.assertEqual("".join(c["text"].split("\n", 1)[1] for c in chunks), body)

    def test_chunk_boundary_prefers_sentence_and_never_exceeds_limit_at_edge(self):
        p, r, l, pol = self.make_inputs()
        fact = copy.deepcopy(pol["supplemental_facts"][0])
        fact.update(id="supplement:resume:edge", title="Edge", portfolio_wins=[])
        room = MAX_CHUNK_CHARS - len("Edge\n")
        body = "x" * (room - 30) + ". " + ("Next sentence has enough words. " * 20)
        fact["text"] = body
        pol["supplemental_facts"] = [fact]
        result = assemble_knowledge(p, r, l, pol)
        chunks = [c for c in result["chunks"] if c["id"].startswith("supplement:resume:edge")]
        self.assertTrue(all(len(c["text"]) <= MAX_CHUNK_CHARS for c in chunks))
        self.assertTrue(chunks[0]["text"].endswith("."))
        self.assertEqual("".join(c["text"].split("\n", 1)[1] for c in chunks), body)

    def test_final_chunk_id_collision_rejected(self):
        p, r, l, pol = self.make_inputs()
        long_fact = copy.deepcopy(pol["supplemental_facts"][0])
        long_fact.update(id="supplement:collision", title="Collision", text="A long fact. " * 200, portfolio_wins=[])
        short_fact = copy.deepcopy(long_fact)
        short_fact.update(id="supplement:collision:chunk:0001", entity_id="person:other", fact_key="other:key", text="Short fact.")
        pol["supplemental_facts"] = [long_fact, short_fact]
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "duplicate_chunk_id")

    def test_malformed_unhashable_policy_values_raise_validation_error(self):
        p, r, l, pol = self.make_inputs()
        pol["supplemental_facts"][0]["sources"][0]["kind"] = []
        with self.assertRaises(KnowledgeValidationError):
            assemble_knowledge(p, r, l, pol)
        p, r, l, pol = self.make_inputs()
        pol["supplemental_facts"][0]["requires_portfolio_ids"] = [[]]
        with self.assertRaises(KnowledgeValidationError):
            assemble_knowledge(p, r, l, pol)

    def test_pdf_revision_changes_provenance_not_semantic_hash(self):
        p, r, l, pol = self.make_inputs()
        first = assemble_knowledge(p, r, l, pol)
        r["source"]["pdf_sha256"] = "b" * 64
        r["source"]["byte_length"] += 1
        second = assemble_knowledge(p, r, l, pol)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertNotEqual(first["provenance"], second["provenance"])

    def test_corrupt_resume_is_rejected(self):
        p, r, l, pol = self.make_inputs()
        r["content_hash"] = "0" * 64
        with self.assertRaises(KnowledgeValidationError) as cm:
            assemble_knowledge(p, r, l, pol)
        self.assertEqual(cm.exception.code, "invalid_resume")

    def test_input_objects_are_not_mutated(self):
        values = self.make_inputs()
        before = copy.deepcopy(values)
        assemble_knowledge(*values)
        self.assertEqual(values, before)


if __name__ == "__main__":
    unittest.main()
