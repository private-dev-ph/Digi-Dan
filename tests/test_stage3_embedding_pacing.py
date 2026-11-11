import unittest
from types import SimpleNamespace
from api.knowledge.cloud_embeddings import GeminiEmbedder, gemini_profile
from api.knowledge.ports import VersioningError

class Clock:
    def __init__(self): self.now = 0; self.sleeps = []
    def time(self): return self.now
    def sleep(self, seconds): self.sleeps.append(seconds); self.now += seconds

class Models:
    def __init__(self): self.calls = []
    def embed_content(self, **kwargs):
        self.calls.append(kwargs['contents'][:])
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1] + [0]*767) for _ in kwargs['contents']])

class PacingTests(unittest.TestCase):
    def setup_adapter(self, background=True):
        clock, models = Clock(), Models()
        return GeminiEmbedder(client=SimpleNamespace(models=models), background=background, monotonic=clock.time, sleep=clock.sleep), clock, models
    def test_rolling_count_and_probe(self):
        adapter, clock, models = self.setup_adapter()
        for _ in range(2): adapter.embed(['x']*32, gemini_profile())
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.sleeps, [61])
        self.assertEqual(len(models.calls), 3)
    def test_bytes_split_order_and_oversize(self):
        adapter, clock, models = self.setup_adapter()
        texts = ['é'*6000, 'b'*12001, 'z']
        self.assertEqual(len(adapter.embed(texts, gemini_profile())), 3)
        self.assertEqual(models.calls, [texts[:1], texts[1:]])
        self.assertEqual(clock.sleeps, [61])
        with self.assertRaises(VersioningError) as caught:
            adapter.embed(['x'*24001], gemini_profile())
        self.assertEqual(caught.exception.code, 'invalid_embedding_input')
        self.assertEqual(len(models.calls), 2)
    def test_rolling_expiry_does_not_reset_newer_attempts(self):
        adapter, clock, models = self.setup_adapter()
        adapter.embed(['a'] * 32, gemini_profile())
        clock.now = 30
        adapter.embed(['b'] * 32, gemini_profile())
        adapter.embed(['c'] * 32, gemini_profile())
        self.assertEqual(clock.now, 61)
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.now, 91)
        self.assertEqual(clock.sleeps, [31, 30])

    def test_subbatch_vectors_keep_input_order(self):
        adapter, clock, models = self.setup_adapter()
        texts = ['a' * 12001, 'b' * 12001, 'c']
        def vectors(**kwargs):
            models.calls.append(kwargs['contents'][:])
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1, ord(text[0])] + [0]*766)
                                               for text in kwargs['contents']])
        models.embed_content = vectors
        result = adapter.embed(texts, gemini_profile())
        for text, vector in zip(texts, result):
            self.assertAlmostEqual(vector[1] / vector[0], ord(text[0]))
        self.assertEqual(models.calls, [texts[:1], texts[1:]])

    def test_runtime_not_paced(self):
        adapter, clock, models = self.setup_adapter(False)
        for _ in range(3): adapter.embed(['x']*32, gemini_profile())
        self.assertEqual(clock.sleeps, [])
    def test_retry_reserves_attempt(self):
        adapter, clock, models = self.setup_adapter()
        original = models.embed_content
        class RateLimit(Exception):
            code = 429
            response = SimpleNamespace(headers={"Retry-After": "0"})
        def call(**kwargs):
            if not models.calls:
                models.calls.append(kwargs['contents'][:])
                raise RateLimit()
            return original(**kwargs)
        models.embed_content = call
        adapter._max_attempts = 2
        adapter.embed(['x']*32, gemini_profile())
        adapter.embed(['probe'], gemini_profile())
        self.assertEqual(clock.sleeps, [1.0, 60.0])

if __name__ == '__main__': unittest.main()
