import copy
import unittest

from api.knowledge.sync_journal import SyncJournal
from api.knowledge.ports import VersioningError


def initial():
    return {"schema_version": 1, "generation": 4, "desired": None, "active": None,
            "history": [], "versions": {}, "last_attempt": None}


class Registry:
    def __init__(self, state=None, conflicts=0):
        self.state = copy.deepcopy(state or initial())
        self.revision = 0
        self.conflicts = conflicts
        self.cas_calls = 0

    def read(self):
        return self.revision, copy.deepcopy(self.state)

    def compare_and_swap(self, revision, state):
        self.cas_calls += 1
        if self.conflicts:
            self.conflicts -= 1
            self.revision += 1
            return False
        if revision != self.revision:
            return False
        self.state = copy.deepcopy(state)
        self.revision += 1
        return True


class Clock:
    def __init__(self, value=1000):
        self.value = value

    def __call__(self):
        return self.value


class SyncJournalTests(unittest.TestCase):
    def test_out_of_order_completion_only_latest_finishes(self):
        registry, clock = Registry(), Clock()
        journal = SyncJournal(registry, clock)
        older = journal.begin("run-1", "manual")
        newer = journal.begin("run-2", "push")
        self.assertFalse(journal.finish(older, {"status": "updated"}))
        self.assertTrue(journal.finish(newer, {"status": "unchanged"}))
        result = journal.read()
        self.assertEqual(result["latest"]["attempt"], newer)
        self.assertEqual(result["latest"]["status"], "unchanged")
        self.assertEqual(result["last_success_at"], 1000.0)

    def test_retries_cas_contention_and_attempt_not_registry_generation(self):
        registry = Registry(conflicts=2)
        journal = SyncJournal(registry, Clock())
        self.assertEqual(journal.begin("local-run", "local"), 1)
        self.assertEqual(registry.cas_calls, 3)
        self.assertEqual(registry.state["generation"], 4)

    def test_unchanged_refreshes_success_without_update_time(self):
        registry, clock = Registry(), Clock(10)
        journal = SyncJournal(registry, clock)
        attempt = journal.begin("r1", "schedule")
        journal.finish(attempt, {"status": "updated"})
        clock.value = 20
        attempt = journal.begin("r2", "schedule")
        journal.finish(attempt, {"status": "unchanged"})
        result = journal.read()
        self.assertEqual(result["last_success_at"], 20)
        self.assertEqual(result["last_update_at"], 10)
        self.assertFalse(result["stale"])

    def test_failed_run_preserves_success_freshness(self):
        registry, clock = Registry(), Clock(10)
        journal = SyncJournal(registry, clock)
        attempt = journal.begin("r1", "manual")
        journal.finish(attempt, {"status": "updated"})
        clock.value = 20
        attempt = journal.begin("r2", "manual")
        journal.finish(attempt, {"status": "failed", "error_code": "upstream_error"})
        result = journal.read()
        self.assertEqual(result["last_success_at"], 10)
        self.assertEqual(result["last_update_at"], 10)

    def test_duplicate_finish_does_not_rewrite_timestamps(self):
        registry, clock = Registry(), Clock(10)
        journal = SyncJournal(registry, clock)
        attempt = journal.begin("r1", "manual")
        self.assertTrue(journal.finish(attempt, {"status": "updated"}))
        before = journal.read()
        clock.value = 20
        self.assertFalse(journal.finish(attempt, {"status": "failed", "error_code": "late_failure"}))
        after = journal.read()
        self.assertEqual(after, before)
        self.assertEqual(after["latest"]["status"], "updated")

    def test_completion_before_start_is_rejected_without_write(self):
        registry, clock = Registry(), Clock(20)
        journal = SyncJournal(registry, clock)
        attempt = journal.begin("r1", "manual")
        clock.value = 19
        with self.assertRaises(VersioningError):
            journal.finish(attempt, {"status": "unchanged"})
        self.assertEqual(journal.read()["latest"]["status"], "running")

    def test_interrupted_run_reports_timeout_and_staleness_without_write(self):
        registry, clock = Registry(), Clock(100)
        journal = SyncJournal(registry, clock)
        journal.begin("r1", "deployment")
        before = registry.cas_calls
        clock.value = 2001
        result = journal.read(stale_after_seconds=10)
        self.assertTrue(result["stale"])
        self.assertTrue(result["running_timed_out"])
        self.assertEqual(registry.cas_calls, before)

    def test_status_writes_preserve_activation_state(self):
        base = initial()
        base.update(active="0123456789abcdef", desired="0123456789abcdef",
                    versions={"0123456789abcdef": {"status": "active"}},
                    history=["0123456789abcdef"])
        registry = Registry(base)
        journal = SyncJournal(registry, Clock())
        before = {key: copy.deepcopy(registry.state[key]) for key in ("active", "desired", "history", "generation", "versions")}
        attempt = journal.begin("r1", "manual")
        journal.finish(attempt, {"status": "updated", "ticket": "a" * 16})
        for key, value in before.items():
            self.assertEqual(registry.state[key], value)

    def test_secret_fields_are_omitted_and_unsafe_allowlisted_fields_rejected(self):
        registry = Registry()
        journal = SyncJournal(registry, Clock())
        attempt = journal.begin("r1", "manual")
        journal.finish(attempt, {"status": "review_required", "ticket": "a" * 16,
                                 "source_text": "private", "url": "https://private", "token": "secret"})
        latest = journal.read()["latest"]
        self.assertNotIn("source_text", latest)
        self.assertNotIn("url", latest)
        self.assertNotIn("token", latest)
        with self.assertRaises(VersioningError):
            SyncJournal(registry, Clock()).finish(1, {"status": "failed", "error_code": "secret\nvalue"})

    def test_activation_uncertainty_flags_round_trip_without_extra_data(self):
        registry = Registry()
        journal = SyncJournal(registry, Clock())
        attempt = journal.begin("r1", "manual")
        journal.finish(attempt, {"status": "failed", "activation_attempted": True,
                                 "activation_uncertain": True, "token": "omit-me"})
        latest = journal.read()["latest"]
        self.assertTrue(latest["activation_attempted"])
        self.assertTrue(latest["activation_uncertain"])
        self.assertNotIn("token", latest)

    def test_corrupt_journal_fails_closed(self):
        registry = Registry()
        registry.state["reconciliation"] = {"attempt": 1, "latest": {"private": "data"},
                                             "last_success_at": None, "last_update_at": None}
        with self.assertRaises(VersioningError):
            SyncJournal(registry, Clock()).read()

    def test_read_rejects_non_object_registry_state(self):
        class InvalidRegistry:
            def read(self):
                return 0, None

        with self.assertRaises(VersioningError):
            SyncJournal(InvalidRegistry(), Clock()).read()

    def test_journal_rejects_inconsistent_attempt_and_timestamps(self):
        registry = Registry()
        registry.state["reconciliation"] = {"attempt": 0, "latest": {
            "attempt": 0, "status": "updated", "trigger": "manual", "run_id": "r",
            "started_at": 20, "finished_at": 21,
        }, "last_success_at": 10, "last_update_at": 11}
        with self.assertRaises(VersioningError):
            SyncJournal(registry, Clock()).read()


if __name__ == "__main__":
    unittest.main()
