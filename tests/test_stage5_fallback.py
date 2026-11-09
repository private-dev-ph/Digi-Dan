import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api.core.config import Settings
from api.query.generation import generate_with_fallback
from api.query.providers import ProviderFailure


def settings(**overrides):
    values = {
        "generation_timeout_seconds": 20,
        "generation_total_timeout_seconds": 40,
        "generation_fallback_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


class FallbackCoordinatorTests(unittest.TestCase):
    def test_primary_success_does_not_call_backup(self):
        calls = []

        def generate(provider, prompt, system, attempt_settings):
            calls.append(provider)
            return "answer"

        self.assertEqual(generate_with_fallback("gemini", False, "p", "s", settings(), generate=generate), ("answer", "gemini"))
        self.assertEqual(calls, ["gemini"])

    def test_transient_failure_uses_other_provider_with_identical_inputs(self):
        calls = []

        def generate(provider, prompt, system, attempt_settings):
            calls.append((provider, prompt, system, attempt_settings))
            if provider == "gemini":
                raise ProviderFailure("connection")
            return "backup"

        answer, provider = generate_with_fallback("gemini", False, "same prompt", "same system", settings(), generate=generate)
        self.assertEqual((answer, provider), ("backup", "deepseek"))
        self.assertEqual([(c[0], c[1], c[2]) for c in calls], [
            ("gemini", "same prompt", "same system"),
            ("deepseek", "same prompt", "same system"),
        ])
        self.assertEqual(calls[1][3].generation_timeout_seconds, 20)

    def test_logs_both_failures_without_exception_or_prompt(self):
        with self.assertLogs("api.query.generation", level="WARNING") as logs:
            with self.assertRaises(HTTPException) as caught:
                generate_with_fallback(
                    "gemini", False, "SECRET PROMPT", "SECRET SYSTEM", settings(),
                    generate=lambda *args: (_ for _ in ()).throw(ProviderFailure("connection" if args[0] == "gemini" else "server")),
                )
        self.assertEqual(caught.exception.status_code, 503)
        output = "\n".join(logs.output)
        self.assertIn("gemini", output)
        self.assertIn("deepseek", output)
        self.assertIn("connection", output)
        self.assertIn("server", output)
        self.assertNotIn("SECRET", output)

    def test_explicit_provider_never_falls_back(self):
        calls = []
        with self.assertRaises(HTTPException):
            generate_with_fallback(
                "gemini", True, "p", "s", settings(),
                generate=lambda provider, *args: (calls.append(provider), (_ for _ in ()).throw(ProviderFailure("timeout")))[1],
            )
        self.assertEqual(calls, ["gemini"])

    def test_explicit_selection_of_each_provider_never_falls_back(self):
        for selected in ("gemini", "deepseek"):
            calls = []
            with self.subTest(provider=selected), self.assertRaises(HTTPException):
                generate_with_fallback(
                    selected,
                    True,
                    "p",
                    "s",
                    settings(),
                    generate=lambda provider, *args: (
                        calls.append(provider),
                        (_ for _ in ()).throw(ProviderFailure("timeout")),
                    )[1],
                )
            self.assertEqual(calls, [selected])

    def test_disabled_fallback_never_uses_other_provider(self):
        calls = []
        with self.assertRaises(HTTPException):
            generate_with_fallback(
                "gemini", False, "p", "s", settings(generation_fallback_enabled=False),
                generate=lambda provider, *args: (calls.append(provider), (_ for _ in ()).throw(ProviderFailure("server")))[1],
            )
        self.assertEqual(calls, ["gemini"])

    def test_nontransient_categories_and_unexpected_errors_do_not_fallback(self):
        for category, expected_status in (("configuration", 503), ("refusal", 422), ("rejected", 502)):
            calls = []
            with self.subTest(category=category), self.assertRaises(HTTPException) as caught:
                generate_with_fallback(
                    "gemini", False, "p", "s", settings(),
                    generate=lambda provider, *args: (calls.append(provider), (_ for _ in ()).throw(ProviderFailure(category)))[1],
                )
            self.assertEqual(caught.exception.status_code, expected_status)
            self.assertIsInstance(caught.exception.detail, str)
            self.assertEqual(calls, ["gemini"])
        calls = []
        with self.assertRaises(HTTPException) as caught:
            generate_with_fallback(
                "gemini", False, "p", "s", settings(),
                generate=lambda provider, *args: (calls.append(provider), (_ for _ in ()).throw(ValueError("private")))[1],
            )
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("private", caught.exception.detail)
        self.assertEqual(calls, ["gemini"])

    def test_empty_answer_is_malformed_and_can_fall_back(self):
        calls = []

        def generate(provider, *args):
            calls.append(provider)
            return " \n " if provider == "gemini" else "ok"

        self.assertEqual(generate_with_fallback("gemini", False, "p", "s", settings(), generate=generate), ("ok", "deepseek"))
        self.assertEqual(calls, ["gemini", "deepseek"])

    def test_shared_deadline_reduces_timeout_and_blocks_new_attempt_after_expiry(self):
        clock = [100.0]
        observed = []
        with patch("api.query.generation.time.monotonic", side_effect=lambda: clock[0]):
            def generate(provider, prompt, system, attempt_settings):
                observed.append((provider, attempt_settings.generation_timeout_seconds))
                if provider == "gemini":
                    clock[0] = 141.0
                    raise ProviderFailure("timeout")
                return "late"

            with self.assertRaises(HTTPException) as caught:
                generate_with_fallback("gemini", False, "p", "s", settings(generation_total_timeout_seconds=40), generate=generate)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(observed, [("gemini", 20.0)])

    def test_remaining_deadline_caps_next_attempt_timeout(self):
        clock = [10.0]
        observed = []
        with patch("api.query.generation.time.monotonic", side_effect=lambda: clock[0]):
            def generate(provider, prompt, system, attempt_settings):
                observed.append((provider, attempt_settings.generation_timeout_seconds))
                if provider == "gemini":
                    clock[0] = 35.0
                    raise ProviderFailure("rate_limit")
                return "ok"

            result = generate_with_fallback("gemini", False, "p", "s", settings(generation_total_timeout_seconds=40), generate=generate)
        self.assertEqual(result, ("ok", "deepseek"))
        self.assertEqual(observed, [("gemini", 20.0), ("deepseek", 15.0)])

    def test_success_after_deadline_is_rejected(self):
        clock = [0.0]
        with patch("api.query.generation.time.monotonic", side_effect=lambda: clock[0]):
            def generate(provider, *args):
                clock[0] = 41.0
                return "late"

            with self.assertRaises(HTTPException) as caught:
                generate_with_fallback("gemini", True, "p", "s", settings(), generate=generate)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(caught.exception.detail, "Digi-Dan is temporarily unable to answer. Please try again shortly.")



class RagFallbackIntegrationTests(unittest.TestCase):
    def _settings(self, **overrides):
        values = dict(
            knowledge_mode="versioned",
            generation_timeout_seconds=20,
            generation_total_timeout_seconds=40,
            generation_fallback_enabled=True,
            chat_rate_limit=0,
        )
        values.update(overrides)
        return Settings(**values)

    def test_rag_fallback_reuses_one_retrieval_result_and_preserves_response_metadata(self):
        from api.core.models import ChatRequest, ConversationMessage
        from api.query import rag

        class Service:
            calls = 0
            active_version = "captured-v1"

            def retrieve(self, query, top_k):
                self.calls += 1
                version = self.active_version
                self.active_version = "later-v2"
                return {"version_id": version}, [{"id": "evidence", "text": "stable fact", "score": 0.7, "metadata": {"source": "test"}}]

        service = Service()
        calls = []
        prompts = []

        def flaky(provider, prompt, system, attempt_settings):
            calls.append(provider)
            prompts.append((prompt, system))
            if provider == "gemini":
                raise ProviderFailure("connection")
            return "answer from backup"

        with patch.object(rag, "generate_answer", side_effect=flaky):
            response = rag.answer_question(
                ChatRequest(
                    message="question",
                    history=[
                        ConversationMessage(role="user", content="earlier"),
                        ConversationMessage(role="assistant", content="earlier reply"),
                    ],
                ),
                self._settings(),
                knowledge_service=service,
            )
        self.assertEqual(service.calls, 1)
        self.assertEqual(calls, ["gemini", "deepseek"])
        self.assertEqual(response.answer, "answer from backup")
        self.assertEqual(response.provider, "deepseek")
        self.assertEqual(response.knowledge_version, "captured-v1")
        self.assertEqual([source.id for source in response.sources], ["evidence"])
        self.assertEqual(prompts[0], prompts[1])
        self.assertIn("stable fact", prompts[0][0])
        self.assertIn("earlier reply", prompts[0][0])

    def test_retrieval_failure_does_not_invoke_generation(self):
        from api.core.models import ChatRequest
        from api.knowledge.ports import VersioningError
        from api.query import rag

        class Service:
            def retrieve(self, query, top_k):
                raise VersioningError("offline")

        with patch.object(rag, "generate_answer") as generate:
            with self.assertRaises(HTTPException):
                rag.answer_question(ChatRequest(message="question"), self._settings(), knowledge_service=Service())
        generate.assert_not_called()

    def test_http_fallback_is_charged_once_and_second_question_is_rate_limited(self):
        from fastapi.testclient import TestClient
        from api.endpoint import application
        from api.knowledge.ports import VersioningError
        from api.query import rag

        class Service:
            def retrieve(self, query, top_k):
                return {"version_id": "v1"}, [{"id": "s1", "text": "evidence"}]

        charges = []

        def charge_once(request, configured_settings):
            charges.append(request)
            if len(charges) > 1:
                raise HTTPException(status_code=429, detail="Too many requests")

        generated = []

        def flaky(provider, *args):
            generated.append(provider)
            if provider == "gemini":
                raise ProviderFailure("server")
            return "ok"

        with patch.object(application, "enforce_chat_rate_limit", side_effect=charge_once), patch.object(rag, "generate_answer", side_effect=flaky):
            with TestClient(application.create_app(settings=self._settings(), knowledge_service=Service())) as client:
                headers = {"origin": "https://zachcodes.vercel.app"}
                first = client.post("/api/chat", json={"message": "first"}, headers=headers)
                second = client.post("/api/chat", json={"message": "second"}, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["provider"], "deepseek")
        self.assertEqual(second.status_code, 429)
        self.assertEqual(len(charges), 2)
        self.assertEqual(generated, ["gemini", "deepseek"])

    def test_invalid_body_and_origin_never_invoke_generation(self):
        from fastapi.testclient import TestClient
        from api.endpoint.application import create_app

        with patch("api.query.rag.generate_answer") as generate:
            with TestClient(create_app(settings=self._settings(chat_rate_limit=0), knowledge_service=object())) as client:
                invalid = client.post("/api/chat", json={"message": "  "}, headers={"origin": "https://zachcodes.vercel.app"})
                forbidden = client.post("/api/chat", json={"message": "hello"}, headers={"origin": "https://not-allowed.example"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(forbidden.status_code, 403)
        generate.assert_not_called()



if __name__ == "__main__":
    unittest.main()
