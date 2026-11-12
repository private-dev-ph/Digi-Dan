import json
import traceback
import unittest

import httpx

from api.knowledge.ports import VersioningError, initial_state
from api.knowledge.redis_registry import MAX_STATE_BYTES, UpstashRedisRegistry


class MockRedis:
    def __init__(self):
        self.fields = {}
        self.commands = []
        self.timeout_after_commit = False
        self.timeout_then_competing_write = False
        self.sync_seen = []

    def handler(self, request):
        self.sync_seen.append(request.headers.get("Upstash-Sync-Token"))
        command = json.loads(request.content)
        self.commands.append(command)
        if command[0] == "HLEN":
            result = len(self.fields)
        elif command[0] == "HMGET":
            result = [self.fields.get(name) for name in command[2:]]
        elif command[0] == "EVAL":
            _, _script, _n, _key, expected, state, op_id = command
            current = self.fields.get("revision", "0")
            if current == str(int(expected) + 1) and self.fields.get("operation_id") == op_id:
                result = 1
            elif current != expected:
                result = 0
            else:
                self.fields.update(revision=str(int(expected) + 1), state=state, operation_id=op_id)
                result = 1
                if self.timeout_after_commit:
                    self.timeout_after_commit = False
                    if self.timeout_then_competing_write:
                        self.timeout_then_competing_write = False
                        self.fields.update(revision="2", operation_id="different-writer")
                    raise httpx.ReadTimeout("secret response body", request=request)
        else:
            result = None
        return httpx.Response(200, json={"result": result}, headers={"Upstash-Sync-Token": "sync-1"})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def registry(redis, writable=True):
    return UpstashRedisRegistry("https://example.upstash.io", "super-secret-token",
                                "digi-dan:knowledge:test:registry", client=redis.client(),
                                writable=writable)


class CloudRegistryTests(unittest.TestCase):
    def setUp(self):
        self.redis = MockRedis()
        self.store = registry(self.redis)

    def tearDown(self):
        self.store._client.close()

    def test_initial_and_read_after_write(self):
        self.assertEqual(self.store.read(), (0, initial_state()))
        state = initial_state()
        state["generation"] = 1
        self.assertTrue(self.store.compare_and_swap(0, state))
        self.assertEqual(self.store.read(), (1, state))
        self.assertEqual(self.redis.sync_seen, [None, "sync-1", "sync-1", "sync-1", "sync-1"])

    def test_corrupt_and_partial_hash_fail_closed(self):
        self.redis.fields.update(revision="1")
        with self.assertRaises(VersioningError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "registry_corrupt")
        self.redis.fields.update(state='{"schema_version":1,"schema_version":1}')
        with self.assertRaises(VersioningError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "registry_corrupt")

    def test_cas_script_guards_partial_hash_and_safe_integer_formatting(self):
        self.store.compare_and_swap(0, initial_state())
        script = next(cmd[1] for cmd in self.redis.commands if cmd[0] == "EVAL")
        self.assertIn("HLEN", script)
        self.assertIn("count ~= 3", script)
        self.assertIn("string.format('%.0f'", script)
        self.assertIn("(value ~= '0' and not string.match(value, '^[1-9][0-9]*$'))", script)
        self.assertNotIn("^(0|[1-9][0-9]*)$", script)
        self.assertNotIn("cjson", script)

    def test_conflict_and_idempotent_retry_after_timeout(self):
        state = initial_state()
        state["generation"] = 1
        self.assertFalse(self.store.compare_and_swap(1, state))
        self.redis.timeout_after_commit = True
        self.assertTrue(self.store.compare_and_swap(0, state))
        writes = [cmd for cmd in self.redis.commands if cmd[0] == "EVAL"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(writes[1], writes[2])
        self.assertEqual(self.store.read(), (1, state))

    def test_retry_conflict_after_timeout_is_uncertain(self):
        state = initial_state()
        state["generation"] = 1
        self.redis.timeout_after_commit = True
        self.redis.timeout_then_competing_write = True
        with self.assertRaises(VersioningError) as caught:
            self.store.compare_and_swap(0, state)
        self.assertEqual(caught.exception.code, "registry_write_uncertain")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_configuration_is_strict_and_operation_hash_is_order_independent(self):
        for url in ("https://a.b.upstash.io", "https://a.upstash.io:443", "https://a.upstash.io/x"):
            with self.assertRaises(VersioningError):
                UpstashRedisRegistry(url, "token", "digi-dan:knowledge:test:registry")
        with self.assertRaises(VersioningError):
            UpstashRedisRegistry("https://a.upstash.io", "token", "digi-dan:knowledge:test:registry", writable=1)
        state = initial_state()
        self.assertTrue(self.store.compare_and_swap(0, state))
        reordered = dict(reversed(list(state.items())))
        self.assertTrue(self.store.compare_and_swap(0, reordered))
        writes = [cmd for cmd in self.redis.commands if cmd[0] == "EVAL"]
        self.assertEqual(writes[0][-1], writes[1][-1])

    def test_read_only_and_size_limit(self):
        readonly = registry(self.redis, writable=False)
        with self.assertRaises(VersioningError) as caught:
            readonly.compare_and_swap(0, initial_state())
        self.assertEqual(caught.exception.code, "registry_read_only")
        readonly._client.close()
        state = initial_state()
        state["extra"] = "x" * MAX_STATE_BYTES
        with self.assertRaises(VersioningError) as caught:
            self.store.compare_and_swap(0, state)
        self.assertEqual(caught.exception.code, "invalid_state")

    def test_no_secrets_in_error_messages(self):
        def failing(_request):
            return httpx.Response(500, text="super-secret-token https://example.upstash.io payload")
        client = httpx.Client(transport=httpx.MockTransport(failing))
        bad = UpstashRedisRegistry("https://example.upstash.io", "super-secret-token",
                                   "digi-dan:knowledge:test:registry", client=client)
        with self.assertRaises(VersioningError) as caught:
            bad.read()
        rendered = str(caught.exception)
        self.assertNotIn("super-secret-token", rendered)
        self.assertNotIn("example.upstash.io", rendered)
        self.assertNotIn("payload", rendered)
        rendered_traceback = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("super-secret-token", rendered_traceback)
        self.assertNotIn("example.upstash.io", rendered_traceback)
        client.close()


if __name__ == "__main__":
    unittest.main()
