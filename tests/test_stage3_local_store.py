import math
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from api.knowledge.local_store import LocalEmbedder, SQLiteRegistry, SQLiteVectorStore
from api.knowledge.ports import VersioningError, initial_state


class SQLiteRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "registry.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_cas_has_one_winner_across_connections(self):
        registry = SQLiteRegistry(self.path)
        revision, state = registry.read()
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(value):
            other = SQLiteRegistry(self.path)
            candidate = dict(state, last_attempt={"value": value})
            barrier.wait()
            outcomes.append(other.compare_and_swap(revision, candidate))

        threads = [threading.Thread(target=attempt, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(registry.read()[0], revision + 1)

    def test_state_is_copied_and_corruption_fails_closed(self):
        registry = SQLiteRegistry(self.path)
        revision, state = registry.read()
        state["versions"]["x"] = {"nested": [1]}
        self.assertTrue(registry.compare_and_swap(revision, state))
        state["versions"]["x"]["nested"].append(2)
        self.assertEqual(registry.read()[1]["versions"]["x"]["nested"], [1])
        con = sqlite3.connect(self.path)
        try:
            con.execute("UPDATE knowledge_registry SET state='{' WHERE id=1")
            con.commit()
        finally:
            con.close()
        with self.assertRaises(VersioningError):
            registry.read()

    def test_rejects_malformed_state_pointers(self):
        registry = SQLiteRegistry(self.path)
        invalid_states = []
        state = initial_state()
        state["active"] = "missing"
        invalid_states.append(state)
        state = initial_state()
        state["desired"] = "missing"
        invalid_states.append(state)
        state = initial_state()
        state["versions"] = {"v": {}}
        state["history"] = ["v", "v"]
        invalid_states.append(state)
        state = initial_state()
        state["last_attempt"] = "invalid"
        invalid_states.append(state)
        for state in invalid_states:
            with self.subTest(state=state), self.assertRaises(VersioningError):
                registry.compare_and_swap(0, state)

    def test_restart_persists_state(self):
        first = SQLiteRegistry(self.path)
        rev, state = first.read()
        state["generation"] = 3
        self.assertTrue(first.compare_and_swap(rev, state))
        self.assertEqual(SQLiteRegistry(self.path).read(), (1, state))


class SQLiteVectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "vectors.sqlite"
        self.store = SQLiteVectorStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def record(identity, text, vector):
        return {"id": identity, "text": text, "metadata": {"title": identity}, "vector": vector}

    def test_restart_namespace_isolation_and_query_relevance(self):
        self.store.put("knowledge-one", [self.record("z", "apple", [1, 0]), self.record("a", "banana", [0, 1])])
        self.store.put("knowledge-two", [self.record("c", "pear", [1, 0])])
        reopened = SQLiteVectorStore(self.path)
        self.assertEqual(reopened.inspect("knowledge-one"), {"ready": 2, "pending": 0})
        self.assertEqual(reopened.inspect("missing"), {"ready": 0, "pending": 0})
        self.assertEqual(reopened.scan("missing"), [])
        matches = reopened.query("knowledge-one", [1, 0], 2)
        self.assertEqual([row["id"] for row in matches], ["z", "a"])
        self.assertEqual(matches[0]["score"], 1.0)
        self.assertEqual([r["id"] for r in reopened.scan("knowledge-two")], ["c"])

    def test_upsert_and_invalid_or_unmanaged_writes(self):
        self.store.put("knowledge-one", [self.record("id", "old", [1, 0])])
        self.store.put("knowledge-one", [self.record("id", "new", [0, 1])])
        self.assertEqual(self.store.inspect("knowledge-one")["ready"], 1)
        self.assertEqual(self.store.scan("knowledge-one")[0]["text"], "new")
        with self.assertRaises(VersioningError):
            self.store.put("legacy", [])
        with self.assertRaises(VersioningError):
            self.store.delete("legacy")
        for bad in ([], [0, 0], [1, float("nan")]):
            with self.subTest(vector=bad), self.assertRaises(VersioningError):
                self.store.put("knowledge-one", [self.record("bad", "bad", bad)])
        with self.assertRaises(VersioningError):
            self.store.put("knowledge-one", [self.record("bad1", "bad", [1, 2]), self.record("bad2", "bad", [1, 2, 3])])
        with self.assertRaises(VersioningError):
            self.store.put("knowledge-one", [self.record("other-dims", "bad", [1, 2, 3])])

    def test_corrupt_stored_vector_fails_as_versioning_error(self):
        self.store.put("knowledge-one", [self.record("id", "valid", [1, 0])])
        con = sqlite3.connect(self.path)
        try:
            con.execute("UPDATE knowledge_vectors SET vector='[0,0]' WHERE namespace='knowledge-one'")
            con.commit()
        finally:
            con.close()
        with self.assertRaises(VersioningError):
            self.store.scan("knowledge-one")
        with self.assertRaises(VersioningError):
            self.store.query("knowledge-one", [1, 0], 1)


class LocalEmbedderTests(unittest.TestCase):
    def setUp(self):
        self.embedder = LocalEmbedder()
        self.profile = {"provider": "local-test", "model": "token-hash-v1", "policy_version": "1", "dimensions": 64}

    def test_deterministic_counts_and_normalization(self):
        first = self.embedder.embed(["Apple apple pear", "pear apple"], self.profile)
        self.assertEqual(first, self.embedder.embed(["Apple apple pear", "pear apple"], self.profile))
        self.assertEqual(len(first), 2)
        self.assertTrue(all(math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0) for vector in first))
        self.assertNotEqual(first[0], first[1])

    def test_rejects_invalid_profiles_and_empty_text(self):
        for changes in ({"provider": "cloud"}, {"model": "other"}, {"dimensions": 0}, {"dimensions": 4097}, {"dimensions": True}, {"extra": "not allowed"}):
            profile = dict(self.profile, **changes)
            with self.subTest(profile=profile), self.assertRaises(VersioningError):
                self.embedder.embed(["test"], profile)
        with self.assertRaises(VersioningError):
            self.embedder.embed(["  !!! "], self.profile)


if __name__ == "__main__":
    unittest.main()
