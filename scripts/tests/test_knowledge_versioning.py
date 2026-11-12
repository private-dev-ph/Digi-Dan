from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from api.knowledge.manifest import canonical_hash
from api.knowledge.ports import VersioningError, initial_state
from api.knowledge.versioning import KnowledgeService


PROFILE = {"provider": "local-test", "model": "tiny", "dimensions": 3, "policy_version": "1"}
PROBES = [{"question": "alpha project", "expected_ids": ["alpha"], "top_k": 1}]


def candidate(text="Alpha project facts.", provenance=None):
    metadata = {"title": "Alpha", "section": "projects", "entity_id": "alpha", "owner": "portfolio",
                "source_url": "https://example.test/alpha", "parent_id": "alpha",
                "chunk_index": 1, "chunk_count": 1}
    row = {"id": "alpha", "text": text, "metadata": metadata}
    row["content_hash"] = canonical_hash(row)
    payload = [{"id": row["id"], "text": row["text"], "metadata": metadata}]
    return {"schema_version": 1, "kind": "knowledge-candidate", "complete": True,
            "review_status": "policy-validated", "policy_version": "p1",
            "content_hash": canonical_hash(payload), "chunks": [row],
            "provenance": provenance or {
                "portfolio_source": {"site_url": "https://example.test"},
                "portfolio_content_hash": "a" * 64,
                "resume_source": {"url": "https://example.test/resume.pdf", "pdf_sha256": "b" * 64},
                "resume_extractor": {},
                "resume_content_hash": "c" * 64,
                "legacy_content_hash": "d" * 64,
                "policy_hash": "e" * 64,
            }}


def make_chunk(chunk_id, text, *, title=None):
    metadata = {"title": title or chunk_id.title(), "section": "projects", "entity_id": chunk_id,
                "owner": "portfolio", "source_url": f"https://example.test/{chunk_id}",
                "parent_id": chunk_id, "chunk_index": 1, "chunk_count": 1}
    row = {"id": chunk_id, "text": text, "metadata": metadata}
    row["content_hash"] = canonical_hash(row)
    return row


def candidate_with_chunks(*rows):
    result = candidate()
    rows = sorted((copy.deepcopy(row) for row in rows), key=lambda row: row["id"])
    result["chunks"] = rows
    result["content_hash"] = canonical_hash([
        {"id": row["id"], "text": row["text"], "metadata": row["metadata"]} for row in rows
    ])
    return result


class Registry:
    def __init__(self):
        self.revision, self.state = 0, initial_state()

    def read(self):
        return self.revision, copy.deepcopy(self.state)

    def compare_and_swap(self, revision, state):
        if revision != self.revision:
            return False
        self.revision += 1
        self.state = copy.deepcopy(state)
        return True


class Vectors:
    def __init__(self):
        self.data, self.deletes, self.queries = {}, [], 0

    def put(self, namespace, records):
        self.data.setdefault(namespace, {}).update({r["id"]: copy.deepcopy(r) for r in records})

    def inspect(self, namespace):
        return {"ready": len(self.data.get(namespace, {})), "pending": 0}

    def scan(self, namespace):
        rows = list(copy.deepcopy(self.data.get(namespace, {})).values())
        for row in rows:
            row.pop("content_hash", None)
        return rows

    def query(self, namespace, vector, top_k):
        self.queries += 1
        return [{**r, "score": 1.0} for r in self.scan(namespace)[:top_k]]

    def delete(self, namespace):
        self.deletes.append(namespace)
        self.data.pop(namespace, None)


class Embedder:
    def embed(self, texts, profile):
        return [[1.0, 2.0, 3.0] for _ in texts]


class FakeClock:
    def __init__(self):
        self.now = 1000

    def __call__(self):
        return self.now


class KnowledgeVersioningTests(unittest.TestCase):
    def setUp(self):
        self.network_blocker = patch("socket.create_connection", side_effect=AssertionError("network access is forbidden in isolated tests"))
        self.network_blocker.start()
        self.registry, self.vectors, self.clock = Registry(), Vectors(), FakeClock()
        self.service = KnowledgeService(self.registry, self.vectors, Embedder(), clock=self.clock, sleep=lambda _: None)
        self.verify = lambda receipt: True

    def tearDown(self):
        self.network_blocker.stop()

    def sync(self, cand=None, **kw):
        return self.service.sync(cand or candidate(), PROFILE, expected_generation=self.registry.state["generation"],
                                 verify_source=self.verify, probes=PROBES, **kw)

    def test_changed_sync_reuses_only_exact_active_rows(self):
        first_candidate = candidate()
        alpha = copy.deepcopy(first_candidate["chunks"][0])
        beta = copy.deepcopy(alpha)
        beta.update(id="beta", text="Beta project facts.")
        beta["metadata"].update(title="Beta", entity_id="beta", parent_id="beta")
        beta["content_hash"] = canonical_hash({"id": beta["id"], "text": beta["text"], "metadata": beta["metadata"]})
        first_candidate["chunks"] = [alpha, beta]
        first_candidate["content_hash"] = canonical_hash([
            {"id": row["id"], "text": row["text"], "metadata": row["metadata"]}
            for row in sorted(first_candidate["chunks"], key=lambda row: row["id"])
        ])
        first = self.sync(first_candidate)
        original_rows = self.vectors.scan(first["namespace"])

        updated = candidate()
        gamma = copy.deepcopy(updated["chunks"][0])
        gamma.update(id="gamma", text="Gamma project facts.")
        gamma["metadata"].update(title="Gamma", entity_id="gamma", parent_id="gamma")
        gamma["content_hash"] = canonical_hash({"id": gamma["id"], "text": gamma["text"], "metadata": gamma["metadata"]})
        updated["chunks"] = [updated["chunks"][0], gamma]
        updated["content_hash"] = canonical_hash([
            {"id": row["id"], "text": row["text"], "metadata": row["metadata"]}
            for row in sorted(updated["chunks"], key=lambda row: row["id"])
        ])
        embedded_calls = []
        class CountingEmbedder:
            def embed(inner_self, texts, profile):
                embedded_calls.extend(texts)
                return [[1.0, 2.0, 3.0] for _ in texts]
        self.service.embedder = CountingEmbedder()
        result = self.sync(updated)
        self.assertEqual((result["reused_chunk_count"], result["embedded_chunk_count"]), (1, 1))
        self.assertEqual(embedded_calls.count("Alpha project facts."), 0)
        self.assertEqual({r["id"] for r in self.vectors.scan(result["namespace"])}, {"alpha", "gamma"})
        self.assertEqual(self.vectors.scan(first["namespace"]), original_rows)

    def test_corrupt_compatible_active_namespace_fails_closed(self):
        first = self.sync()
        self.vectors.data[first["namespace"]]["alpha"]["vector"] = [0.0, 0.0, 0.0]
        changed = candidate("Changed facts.")
        with self.assertRaises(VersioningError) as caught:
            self.sync(changed)
        self.assertEqual(caught.exception.code, "invalid_vectors")
        self.assertEqual(self.service.get_active()["ticket"], first["ticket"])

    def test_metadata_only_change_does_not_reuse_embedding(self):
        self.sync()
        updated_row = make_chunk("alpha", "Alpha project facts.", title="Updated Alpha")
        updated = candidate_with_chunks(updated_row)
        calls = []
        class CountingEmbedder:
            def embed(inner_self, texts, profile):
                calls.extend(texts)
                return [[1.0, 2.0, 3.0] for _ in texts]
        self.service.embedder = CountingEmbedder()
        result = self.sync(updated)
        self.assertEqual((result["reused_chunk_count"], result["embedded_chunk_count"]), (0, 1))
        self.assertIn("Alpha project facts.", calls)

    def test_profile_mismatch_embeds_all_candidate_chunks(self):
        self.sync()
        calls = []
        class CountingEmbedder:
            def embed(inner_self, texts, profile):
                calls.append((tuple(texts), copy.deepcopy(profile)))
                return [[1.0, 2.0, 3.0] for _ in texts]
        self.service.embedder = CountingEmbedder()
        candidate2 = candidate_with_chunks(make_chunk("alpha", "Alpha project facts."),
                                           make_chunk("beta", "Beta project facts."))
        profile = {**PROFILE, "model": "different"}
        result = self.service.sync(candidate2, profile, expected_generation=self.registry.state["generation"],
                                   verify_source=self.verify, probes=PROBES)
        self.assertEqual((result["reused_chunk_count"], result["embedded_chunk_count"]), (0, 2))
        self.assertTrue(any(set(texts) == {"Alpha project facts.", "Beta project facts."} and p == profile
                            for texts, p in calls))

    def test_nonactive_staged_namespace_is_never_a_reuse_source(self):
        self.sync()
        staged_candidate = candidate_with_chunks(make_chunk("beta", "Beta project facts."))
        staged = self.service.sync(staged_candidate, PROFILE, expected_generation=self.registry.state["generation"],
                                  verify_source=self.verify,
                                  probes=[{"question": "beta project", "expected_ids": ["beta"], "top_k": 1}],
                                  activate=False)
        self.assertEqual(staged["status"], "staged")
        original_query = self.vectors.query
        self.vectors.query = lambda namespace, vector, top_k: []
        try:
            with self.assertRaises(VersioningError) as caught:
                self.service.sync(staged_candidate, PROFILE,
                                 expected_generation=self.registry.state["generation"],
                                 verify_source=self.verify,
                                 probes=[{"question": "beta project", "expected_ids": ["beta"], "top_k": 1}])
            self.assertEqual(caught.exception.code, "probe_failed")
        finally:
            self.vectors.query = original_query
        self.assertEqual(self.registry.state["versions"][self.registry.state["last_attempt"]["ticket"]]["status"], "failed")
        calls = []
        class CountingEmbedder:
            def embed(inner_self, texts, profile):
                calls.extend(texts)
                return [[1.0, 2.0, 3.0] for _ in texts]
        self.service.embedder = CountingEmbedder()
        combined = candidate_with_chunks(make_chunk("alpha", "Alpha project facts."),
                                         make_chunk("beta", "Beta project facts."))
        result = self.sync(combined)
        self.assertEqual((result["reused_chunk_count"], result["embedded_chunk_count"]), (1, 1))
        self.assertIn("Beta project facts.", calls)

    def test_corrupt_active_text_fails_before_provider_or_target_put(self):
        first = self.sync()
        self.vectors.data[first["namespace"]]["alpha"]["text"] = "corrupted stored text"
        original_put = self.vectors.put
        put_calls = []
        def tracking_put(namespace, records):
            put_calls.append((namespace, copy.deepcopy(records)))
            original_put(namespace, records)
        self.vectors.put = tracking_put
        calls = []
        class CountingEmbedder:
            def embed(inner_self, texts, profile):
                calls.extend(texts)
                return [[1.0, 2.0, 3.0] for _ in texts]
        self.service.embedder = CountingEmbedder()
        with self.assertRaises(VersioningError) as caught:
            self.sync(candidate("Changed facts."))
        self.assertEqual(caught.exception.code, "vector_contents_mismatch")
        self.assertEqual(put_calls, [])
        self.assertEqual(calls, [])
        self.assertEqual(self.registry.state["active"], first["ticket"])

    def test_build_activation_noop_and_profile_identity(self):
        first = self.sync()
        self.assertEqual(first["status"], "active")
        active = self.service.get_active()
        self.assertEqual(active["ticket"], first["ticket"])
        unchanged = self.sync()
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual((unchanged["reused_chunk_count"], unchanged["embedded_chunk_count"]), (0, 0))
        profile = {**PROFILE, "model": "different"}
        second = self.service.sync(candidate(), profile, expected_generation=self.registry.state["generation"],
                                   verify_source=self.verify, probes=PROBES)
        self.assertEqual(second["status"], "active")
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertEqual(self.registry.state["history"], [first["ticket"]])

    def test_validation_source_failure_and_wrong_probe_preserve_active(self):
        first = self.sync()
        malformed = candidate()
        malformed["chunks"][0]["text"] = "tampered"
        with self.assertRaises(VersioningError) as caught:
            self.sync(malformed)
        self.assertEqual(caught.exception.code, "chunk_hash_mismatch")
        with self.assertRaises(VersioningError):
            self.service.sync(candidate("other words"), PROFILE, expected_generation=self.registry.state["generation"],
                              verify_source=lambda _: False, probes=PROBES)
        self.assertEqual(self.service.get_active()["ticket"], first["ticket"])
        latest = self.registry.state["last_attempt"]
        self.assertEqual(latest["status"], "failed")

    def test_stage_then_activate_and_rollback(self):
        one = self.sync()
        self.clock.now += 1
        staged = self.sync(candidate("Second approved fact."), activate=False)
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(self.service.get_active()["ticket"], one["ticket"])
        result = self.service.activate(staged["ticket"], verify_source=self.verify)
        self.assertEqual(result["status"], "active")
        rolled = self.service.rollback(one["ticket"], expected_generation=self.registry.state["generation"],
                                       verify_source=self.verify)
        self.assertEqual(rolled["ticket"], one["ticket"])
        self.assertEqual(self.service.get_active()["ticket"], one["ticket"])

    def test_identical_content_refreshes_active_deployment_receipt(self):
        first = self.sync()
        updated = candidate(provenance={**candidate()["provenance"],
            "portfolio_source": {"site_url": "https://example.test", "deployment_id": "new"}})
        result = self.service.sync(updated, PROFILE,
            expected_generation=self.registry.state["generation"],
            verify_source=self.verify, probes=PROBES, activate=False)
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["ticket"], first["ticket"])
        self.assertEqual(self.registry.state["versions"][first["ticket"]]["source"], updated["provenance"])
        self.assertEqual(len(self.registry.state["versions"]), 1)
    def test_stale_source_on_activation_cannot_flip_pointer(self):
        first = self.sync()
        self.clock.now += 1
        staged = self.sync(candidate("Second fact."), activate=False)
        with self.assertRaises(VersioningError) as caught:
            self.service.activate(staged["ticket"], verify_source=lambda _: False)
        self.assertEqual(caught.exception.code, "source_stale")
        self.assertEqual(self.service.get_active()["ticket"], first["ticket"])

    def test_old_activation_cannot_steal_a_newer_intent(self):
        old = self.sync(activate=False)
        self.clock.now += 1
        newer = self.sync(candidate("Newer fact."), activate=False)
        desired = self.registry.state["desired"]
        with self.assertRaises(VersioningError) as caught:
            self.service.activate(old["ticket"], verify_source=self.verify)
        self.assertEqual(caught.exception.code, "superseded")
        self.assertEqual(self.registry.state["desired"], desired)
        self.assertEqual(self.registry.state["active"], None)

    def test_noop_requires_fresh_source_before_generation_changes(self):
        first = self.sync()
        generation = self.registry.state["generation"]
        with self.assertRaises(VersioningError) as caught:
            self.service.sync(candidate(), PROFILE, expected_generation=generation,
                              verify_source=lambda _: False, probes=PROBES)
        self.assertEqual(caught.exception.code, "source_stale")
        self.assertEqual(self.registry.state["generation"], generation)
        self.assertEqual(self.service.get_active()["ticket"], first["ticket"])

    def test_noop_requires_same_pipeline_policy_and_runs_incoming_probes(self):
        first = self.sync()
        previous_queries = self.vectors.queries
        result = self.service.sync(candidate(), PROFILE, expected_generation=self.registry.state["generation"],
                                   verify_source=self.verify,
                                   probes=[{"question": "new retrieval check", "expected_ids": ["alpha"], "top_k": 1}])
        self.assertEqual(result["status"], "unchanged")
        self.assertGreater(self.vectors.queries, previous_queries)
        different_policy = candidate()
        different_policy["policy_version"] = "p2"
        second = self.service.sync(different_policy, PROFILE, expected_generation=self.registry.state["generation"],
                                   verify_source=self.verify, probes=PROBES)
        self.assertEqual(second["status"], "active")
        self.assertNotEqual(first["version_id"], second["version_id"])

    def test_source_metadata_and_service_bounds_are_validated(self):
        malformed = candidate()
        malformed["chunks"][0]["metadata"]["source_url"] = "file:///private"
        with self.assertRaises(VersioningError) as caught:
            self.sync(malformed)
        self.assertEqual(caught.exception.code, "invalid_chunk_metadata")
        for args in ({"batch_size": 0}, {"poll_attempts": 0}, {"poll_interval": float("nan")}):
            with self.assertRaises(VersioningError):
                KnowledgeService(self.registry, self.vectors, Embedder(), **args)

    def test_retrieval_wraps_provider_errors_and_malformed_active_record(self):
        active = self.sync()
        class BrokenEmbedder:
            def embed(self, *args):
                raise RuntimeError("provider failure")
        self.service.embedder = BrokenEmbedder()
        with self.assertRaises(VersioningError) as caught:
            self.service.retrieve("alpha", 1)
        self.assertEqual(caught.exception.code, "retrieval_failed")
        state = self.registry.state
        state["versions"][active["ticket"]] = {"status": "verified"}
        self.registry.state = state
        with self.assertRaises(VersioningError):
            self.service.get_active()
        valid_shape = {
            "ticket": active["ticket"], "status": "verified", "namespace": active["namespace"],
            "version_id": "a" * 64, "candidate_hash": "b" * 64, "embedding": PROFILE,
            "expected_ids": [], "expected_hashes": {}, "expected_count": 0,
        }
        self.registry.state["versions"][active["ticket"]] = valid_shape
        with self.assertRaises(VersioningError):
            self.service.get_active()

    def test_retrieval_rejects_duplicate_rows_and_nonfinite_scores(self):
        self.sync()
        self.vectors.query = lambda namespace, vector, top_k: [
            {**self.vectors.scan(namespace)[0], "score": 1.0},
            {**self.vectors.scan(namespace)[0], "score": 0.5},
        ]
        with self.assertRaises(VersioningError) as caught:
            self.service.retrieve("alpha", 2)
        self.assertEqual(caught.exception.code, "invalid_query_results")
        self.vectors.query = lambda namespace, vector, top_k: [
            {**self.vectors.scan(namespace)[0], "score": float("inf")}
        ]
        with self.assertRaises(VersioningError) as caught:
            self.service.retrieve("alpha", 1)
        self.assertEqual(caught.exception.code, "invalid_query_results")
        self.vectors.query = lambda namespace, vector, top_k: []
        with self.assertRaises(VersioningError) as caught:
            self.service.retrieve("alpha", 1)
        self.assertEqual(caught.exception.code, "invalid_query_results")

    def test_verified_orphan_can_be_cleaned_after_verification_grace(self):
        orphan = self.sync(activate=False)
        self.clock.now += 1
        active = self.sync(candidate("Active version."), activate=True)
        self.clock.now += 4000
        removed = self.service.cleanup(retain_previous=0, grace_seconds=3600)
        self.assertIn(orphan["namespace"], removed)
        self.assertEqual(self.service.get_active()["ticket"], active["ticket"])

    def test_cleanup_uses_retirement_grace_and_retries_deleting_tombstone(self):
        first = self.sync()
        self.clock.now += 1
        second = self.sync(candidate("Second fact."))
        self.clock.now += 4000
        original_delete = self.vectors.delete
        failed_once = []
        def fail_once(namespace):
            if not failed_once:
                failed_once.append(namespace)
                raise OSError("temporary storage failure")
            original_delete(namespace)
        self.vectors.delete = fail_once
        self.assertEqual(self.service.cleanup(retain_previous=0, grace_seconds=3600), [])
        self.assertEqual(self.registry.state["versions"][first["ticket"]]["status"], "deleting")
        self.assertEqual(self.service.cleanup(retain_previous=0, grace_seconds=3600), [first["namespace"]])
        self.assertEqual(self.registry.state["versions"][first["ticket"]]["status"], "deleted")
        self.assertEqual(self.service.get_active()["ticket"], second["ticket"])

    def test_readiness_contents_and_cleanup_grace(self):
        first = self.sync()
        self.clock.now += 2
        staged = self.sync(candidate("Second fact."), activate=False)
        rec = self.registry.state["versions"][staged["ticket"]]
        self.vectors.data[rec["namespace"]]["alpha"]["text"] = "corruption"
        with self.assertRaises(VersioningError) as caught:
            self.service.activate(staged["ticket"], verify_source=self.verify)
        self.assertEqual(caught.exception.code, "vector_contents_mismatch")
        # It remains non-active and the active namespace is never a cleanup candidate.
        self.assertEqual(self.service.get_active()["ticket"], first["ticket"])
        self.clock.now += 4000
        self.assertEqual(self.service.cleanup(retain_previous=0, grace_seconds=3600), [])
        self.assertIn(first["namespace"], self.vectors.data)

    def test_retrieval_is_bound_to_single_verified_namespace(self):
        active = self.sync()
        record, rows = self.service.retrieve("alpha", 3)
        self.assertEqual(record["namespace"], active["namespace"])
        self.assertEqual(rows[0]["id"], "alpha")
        with self.assertRaises(VersioningError):
            self.service.retrieve("alpha", 21)


if __name__ == "__main__":
    unittest.main()
