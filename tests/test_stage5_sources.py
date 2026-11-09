import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.core.config import Settings
from api.core.models import ChatRequest, Source
from api.endpoint.application import create_app
from api.query import rag


class SourceTraceabilityTests(unittest.TestCase):
    def test_boundaries_include_headers_and_separator(self):
        sources = [Source(id="a", text="one"), Source(id="b", text="two")]
        expected = "[a]\none\n\n[b]\ntwo\n"
        for limit in (0, -1, 1, len("[a]\none\n") - 1):
            with self.subTest(limit=limit):
                self.assertEqual(rag._assemble_context(sources, limit), ("", []))
        context, admitted = rag._assemble_context(sources, len(expected))
        self.assertEqual(context, expected)
        self.assertEqual([source.id for source in admitted], ["a", "b"])
        context, admitted = rag._assemble_context(sources, len(expected) - 1)
        self.assertEqual(context, "[a]\none\n")
        self.assertEqual([source.id for source in admitted], ["a"])

    def test_empty_blank_oversized_unicode_and_no_mutation(self):
        self.assertEqual(rag._assemble_context([], 100), ("", []))
        self.assertEqual(rag._assemble_context([Source(id="blank", text=" \n ")], 100), ("", []))
        self.assertEqual(rag._assemble_context([Source(id="large", text="x" * 100)], 10), ("", []))
        sources = [
            Source(id="large", text="x" * 100),
            Source(id="blank", text="  \n "),
            Source(id="unicode", text="  😀é  ", score=0.75, metadata={"tag": "value"}),
            Source(id="last", text="z"),
        ]
        original = copy.deepcopy(sources)
        expected = "[unicode]\n😀é\n\n[last]\nz\n"
        context, admitted = rag._assemble_context(sources, len(expected))
        self.assertEqual(context, expected)
        self.assertEqual([source.id for source in admitted], ["unicode", "last"])
        self.assertEqual(admitted[0].text, "😀é")
        self.assertEqual(admitted[0].score, 0.75)
        self.assertEqual(admitted[0].metadata, {"tag": "value"})
        self.assertIsNot(admitted[0], sources[2])
        self.assertEqual(sources, original)
        self.assertNotIn("[large]", context)
        self.assertNotIn("[blank]", context)

    def test_answer_uses_only_admitted_sources_for_both_modes_and_providers(self):
        records = [
            {"id": "big", "text": "excluded " * 50, "score": 0.1, "metadata": {"kind": "excluded"}},
            {"id": "one", "text": " first fact ", "score": 0.8, "metadata": {"kind": "first"}},
            {"id": "two", "text": "second fact", "score": 0.7, "metadata": {"kind": "second"}},
        ]
        expected_context = "[one]\nfirst fact\n\n[two]\nsecond fact\n"
        history = [{"role": "user", "content": "history marker"}, {"role": "assistant", "content": "history reply"}]

        class Service:
            def __init__(self):
                self.calls = 0

            def retrieve(self, query, top_k):
                self.calls += 1
                return {"version_id": "captured-v"}, records

        for mode in ("legacy", "versioned"):
            for provider in ("gemini", "deepseek"):
                with self.subTest(mode=mode, provider=provider):
                    service = Service()
                    settings = Settings(knowledge_mode=mode, max_context_chars=len(expected_context))
                    request = ChatRequest(message="What happened?", provider=provider, history=history)
                    with patch.object(rag, "query_sources", return_value=[Source(**record) for record in records]) as legacy, patch.object(rag, "generate_answer", return_value="answer") as generate:
                        result = rag.answer_question(request, settings, knowledge_service=service)
                    prompt = generate.call_args.args[1]
                    self.assertEqual(generate.call_args.args[0], provider)
                    self.assertEqual(prompt.split("Retrieved context:\n", 1)[1].split("\n\nUntrusted conversation history", 1)[0], expected_context)
                    self.assertNotIn("[big]", prompt)
                    self.assertIn("history marker", prompt)
                    self.assertEqual([source.id for source in result.sources], ["one", "two"])
                    self.assertEqual([source.text for source in result.sources], ["first fact", "second fact"])
                    self.assertEqual([(s.score, s.metadata) for s in result.sources], [(0.8, {"kind": "first"}), (0.7, {"kind": "second"})])
                    self.assertEqual(result.provider, provider)
                    self.assertEqual(result.knowledge_version, "captured-v" if mode == "versioned" else None)
                    self.assertEqual(service.calls, int(mode == "versioned"))
                    self.assertEqual(legacy.call_count, int(mode == "legacy"))

    def test_http_serializes_only_admitted_sources(self):
        class Service:
            def retrieve(self, query, top_k):
                return {"version_id": "http-v"}, [
                    {"id": "excluded", "text": "x" * 100},
                    {"id": "kept", "text": " fact ", "score": 0.6, "metadata": {"kind": "http"}},
                ]

        settings = Settings(knowledge_mode="versioned", max_context_chars=len("[kept]\nfact\n"), chat_rate_limit=0)
        with patch.object(rag, "generate_answer", return_value="answer"):
            with TestClient(create_app(settings=settings, knowledge_service=Service())) as client:
                response = client.post("/api/chat", json={"message": "hello", "provider": "deepseek"}, headers={"origin": "https://zachcodes.vercel.app"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["knowledge_version"], "http-v")
        self.assertEqual(body["provider"], "deepseek")
        self.assertEqual(body["sources"], [{"id": "kept", "text": "fact", "score": 0.6, "metadata": {"kind": "http"}}])


if __name__ == "__main__":
    unittest.main()
