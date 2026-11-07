import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from api.core.config import Settings
from api.knowledge.ports import VersioningError
from api.tools import knowledge_production as cli
from tests.test_stage3_versioning import candidate as valid_candidate, PROBES


def candidate_value():
    return valid_candidate()


class ProductionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate.json"
        self.probes = self.root / "probes.json"
        self.candidate.write_text(json.dumps(candidate_value()), encoding="utf-8")
        self.probes.write_text(json.dumps({"schema_version": 1,
                                           "kind": "production-retrieval-probes",
                                           "probes": PROBES}),
                               encoding="utf-8")
        self.settings = Settings(knowledge_environment="production",
            upstash_redis_rest_url="https://example.upstash.io", upstash_redis_rest_token="worker",
            upstash_vector_rest_url="https://vector.upstash.io", upstash_vector_rest_token="vector",
            gemini_api_key="embedding-key")

    def tearDown(self):
        self.temp.cleanup()

    def _stage(self, **kwargs):
        values = dict(candidate_path=self.candidate, site_url="https://example.com",
                      expected_generation=4, resume_path=self.root / "resume.json",
                      probes_path=self.probes)
        values.update(kwargs)
        return cli.stage(self.settings, **values)

    def test_missing_tampered_and_stale_candidate_never_construct_writer(self):
        with patch("api.knowledge.cloud.create_production_stager", create=True) as factory:
            with self.assertRaises(Exception):
                self._stage(candidate_path=self.root / "missing.json")
            self.assertFalse(factory.called)

            self.candidate.write_text("{}", encoding="utf-8")
            with self.assertRaises(Exception):
                self._stage()
            self.assertFalse(factory.called)

            self.candidate.write_text(json.dumps(candidate_value()), encoding="utf-8")
            with patch.object(cli, "_fresh_candidate", side_effect=VersioningError("candidate_review_required")):
                with self.assertRaises(VersioningError):
                    self._stage()
            self.assertFalse(factory.called)

    def test_invalid_probes_and_generation_never_construct_writer(self):
        with patch("api.knowledge.cloud.create_production_stager", create=True) as factory:
            self.probes.write_text('{"schema_version":1,"kind":"local-retrieval-probes","probes":[]}', encoding="utf-8")
            with self.assertRaises(VersioningError):
                self._stage()
            self.probes.write_text(json.dumps({"schema_version": 1, "kind": "production-retrieval-probes",
                                               "probes": [{"question": "q", "expected_ids": ["not-a-candidate-id"], "top_k": 1}]}),
                                   encoding="utf-8")
            with self.assertRaises(VersioningError):
                self._stage()
            with self.assertRaises(VersioningError):
                self._stage(expected_generation=-1)
            factory.assert_not_called()

    def test_stage_validates_then_delegates_expected_generation(self):
        stager = type("Stager", (), {"stage": lambda self, candidate, **kw: {"status": "staged", **kw}})()
        with patch.object(cli, "_validate", wraps=cli._validate) as validate, \
                patch.object(cli, "_fresh_candidate") as fresh, \
                patch("api.knowledge.cloud.create_production_stager", create=True, return_value=stager) as factory:
            result = self._stage()
        validate.assert_called_once()
        fresh.assert_called_once()
        factory.assert_called_once_with(self.settings, site_url="https://example.com")
        self.assertEqual(result["expected_generation"], 4)

    def test_fresh_candidate_compares_full_rebuilt_value(self):
        expected = candidate_value()
        def rebuild(*, output_dir, **_):
            (Path(output_dir) / "last-successful.json").write_text(json.dumps(expected), encoding="utf-8")
        with patch.object(cli, "build_candidate", side_effect=rebuild):
            cli._fresh_candidate(expected, site_url="https://example.com", resume_path=self.root / "resume.json")
        changed = dict(expected, policy_version="changed")
        with patch.object(cli, "build_candidate", side_effect=rebuild):
            with self.assertRaises(VersioningError) as caught:
                cli._fresh_candidate(changed, site_url="https://example.com", resume_path=self.root / "resume.json")
        self.assertEqual(caught.exception.code, "candidate_review_required")

    def test_activation_and_rollback_review_candidate_before_factory(self):
        rollout = type("Rollout", (), {
            "activate": lambda self, ticket, candidate, **kw: {"status": "active", "ticket": ticket, **kw},
            "rollback": lambda self, ticket, candidate, **kw: {"status": "rolled_back", "ticket": ticket, **kw},
        })()
        module = types.ModuleType("api.knowledge.production_rollout")
        module.create_production_rollout = lambda worker, site_url: rollout
        with patch.dict("sys.modules", {"api.knowledge.production_rollout": module}), \
                patch.object(cli, "_fresh_candidate") as fresh, \
                patch.object(cli, "_validate") as validate:
            result = cli.activate(self.settings, ticket="0123456789abcdef", candidate_path=self.candidate,
                                  site_url="https://example.com", expected_generation=8,
                                  resume_path=self.root / "resume.json")
        fresh.assert_called_once()
        validate.assert_called_once()
        self.assertEqual(result, {"status": "active", "ticket": "0123456789abcdef", "expected_generation": 8})
        with patch.dict("sys.modules", {"api.knowledge.production_rollout": module}), \
                patch.object(cli, "_fresh_candidate"), patch.object(cli, "_validate"):
            result = cli.rollback(self.settings, ticket="0123456789abcdef", candidate_path=self.candidate,
                                  site_url="https://example.com", expected_generation=9,
                                  resume_path=self.root / "resume.json")
        self.assertEqual(result, {"status": "rolled_back", "ticket": "0123456789abcdef",
                                  "expected_generation": 9})

    def test_activation_rejects_bad_ticket_and_stale_candidate_before_factory(self):
        with patch("api.knowledge.production_rollout.create_production_rollout", create=True) as factory:
            with self.assertRaises(VersioningError):
                cli.activate(self.settings, ticket="ticket-1", candidate_path=self.candidate,
                             site_url="https://example.com", expected_generation=8,
                             resume_path=self.root / "resume.json")
            with patch.object(cli, "_validate"), \
                    patch.object(cli, "_fresh_candidate", side_effect=VersioningError("candidate_review_required")):
                with self.assertRaises(VersioningError):
                    cli.activate(self.settings, ticket="0123456789abcdef", candidate_path=self.candidate,
                                 site_url="https://example.com", expected_generation=8,
                                 resume_path=self.root / "resume.json")
            factory.assert_not_called()

    def test_verify_checks_selected_active_record_and_smokes_read_only_service(self):
        record = {"ticket": "0123456789abcdef", "version_id": "v2", "expected_count": 12,
                  "probes": [{"question": "q"}]}
        service = type("Service", (), {"get_active": lambda self: record,
            "_validate_storage": lambda self, rec: None, "_run_probes": lambda self, rec: None})()
        with patch("api.knowledge.cloud.create_cloud_service", return_value=service) as factory, \
                patch("api.knowledge.deployed_source.DeployedSourceVerifier") as verifier:
            result = cli.verify(self.settings, ticket="0123456789abcdef", site_url="https://example.com")
        factory.assert_called_once_with(self.settings, writable=False)
        verifier.return_value.verify.assert_called_once_with(record)
        self.assertEqual(result, {"ticket": "0123456789abcdef", "version_id": "v2",
                                  "expected_count": 12, "probe_count": 1})

    def test_verify_rejects_active_pointer_change_after_smoke_checks(self):
        initial = {"ticket": "0123456789abcdef", "version_id": "v2", "expected_count": 12,
                   "probes": [{"question": "q"}]}
        changed = dict(initial, ticket="fedcba9876543210", version_id="v3")
        service = type("Service", (), {
            "get_active": lambda self: next(self.records),
            "_validate_storage": lambda self, rec: None,
            "_run_probes": lambda self, rec: None,
            "records": iter([initial, changed]),
        })()
        with patch("api.knowledge.cloud.create_cloud_service", return_value=service), \
                patch("api.knowledge.deployed_source.DeployedSourceVerifier") as verifier:
            with self.assertRaises(VersioningError) as caught:
                cli.verify(self.settings, ticket="0123456789abcdef", site_url="https://example.com")
        self.assertEqual(caught.exception.code, "active_version_changed")
        verifier.return_value.verify.assert_not_called()

    def test_cli_sanitizes_verify_source_failure(self):
        output = io.StringIO()
        service = type("Service", (), {"get_active": lambda self: {
            "ticket": "0123456789abcdef", "version_id": "v2", "expected_count": 1,
            "probes": [{"question": "q"}]}, "_validate_storage": lambda self, rec: None,
            "_run_probes": lambda self, rec: None})()
        with patch.object(cli, "load_production_settings", return_value=("worker", self.settings)), \
                patch("api.knowledge.cloud.create_cloud_service", return_value=service), \
                patch("api.knowledge.deployed_source.DeployedSourceVerifier") as verifier, \
                contextlib.redirect_stdout(output):
            verifier.return_value.verify.side_effect = VersioningError("source_stale")
            result = cli.main(["verify", "--ticket", "0123456789abcdef", "--site-url", "https://example.com"])
        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "source_stale")

    def test_status_uses_runtime_token_and_returns_only_selected_fields(self):
        state = {"generation": 7, "active": "ticket-1", "desired": "ticket-1",
                 "versions": {"ticket-1": {"ticket": "ticket-1", "version_id": "v1",
                    "namespace": "n1", "status": "active", "expected_count": 9,
                    "error_code": None, "private": "credential"}}}
        with patch.object(cli, "load_production_settings", return_value=("worker", "runtime")), \
                patch.object(cli, "preflight"), \
                patch.object(cli, "create_cloud_stores", return_value=(type("R", (), {"read": lambda self: (3, state)})(), object())) as stores, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = cli.main(["status"])
        stores.assert_called_once_with("runtime", writable=False)
        payload = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(payload["result"]["registry_revision"], 3)
        self.assertNotIn("private", output.getvalue())

    def test_cli_sanitizes_unexpected_error(self):
        output = io.StringIO()
        with patch.object(cli, "load_production_settings", side_effect=RuntimeError("token=secret")), \
                contextlib.redirect_stdout(output):
            result = cli.main(["status"])
        self.assertEqual(result, 1)
        self.assertNotIn("secret", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error_code"], "production_operation_failed")


if __name__ == "__main__":
    unittest.main()
