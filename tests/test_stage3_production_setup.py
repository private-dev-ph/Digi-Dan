import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api.knowledge import cloud
from api.knowledge.ports import VersioningError, initial_state
from api.tools import knowledge_production_preflight as cli


class ProductionSetupTests(unittest.TestCase):
    def test_requires_explicit_production_environment_and_redis_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, prod = Path(tmp) / '.env', Path(tmp) / '.env.cloud-production'
            base.write_text('KNOWLEDGE_ENVIRONMENT=production\nUPSTASH_REDIS_REST_URL=https://old.upstash.io\nUPSTASH_REDIS_REST_TOKEN=old-worker\n')
            prod.write_text('UPSTASH_REDIS_REST_READ_ONLY_TOKEN=runtime\n')
            with self.assertRaises(VersioningError):
                cli.load_production_settings(prod, base_file=base, environ={})
            prod.write_text('KNOWLEDGE_ENVIRONMENT=production\nUPSTASH_REDIS_REST_URL=https://new.upstash.io\nUPSTASH_REDIS_REST_TOKEN=worker\nUPSTASH_REDIS_REST_READ_ONLY_TOKEN=runtime\n')
            worker, runtime = cli.load_production_settings(prod, base_file=base, environ={})
            self.assertEqual(worker.upstash_redis_rest_url, 'https://new.upstash.io')
            self.assertEqual(worker.upstash_redis_rest_token, 'worker')
            self.assertEqual(runtime.upstash_redis_rest_token, 'runtime')

    def test_rejects_nonproduction_or_same_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / 'prod'
            for mode, runtime_token in [('test', 'runtime'), ('production', 'worker')]:
                env.write_text(f'KNOWLEDGE_ENVIRONMENT={mode}\nUPSTASH_REDIS_REST_URL=https://redis.upstash.io\nUPSTASH_REDIS_REST_TOKEN=worker\nUPSTASH_REDIS_REST_READ_ONLY_TOKEN={runtime_token}\n')
                with self.assertRaises(VersioningError):
                    cli.load_production_settings(env, base_file=Path(tmp) / 'missing', environ={})

    def test_offline_does_not_construct_clients(self):
        worker, runtime = self.settings()
        with patch.object(cli, 'create_cloud_stores', side_effect=AssertionError('client')):
            result = cli.preflight(worker, runtime)
        self.assertEqual(result['environment'], 'production')
        self.assertFalse(result['writes_performed'])
        self.assertFalse(result['embeddings_requested'])
        self.assertFalse(result['live_chat_mode_changed'])
        self.assertNotIn('secret', json.dumps(result))

    def test_invalid_runtime_config_fails_before_clients(self):
        worker, runtime = self.settings()
        runtime.upstash_vector_rest_token = None
        with patch.object(cli, 'create_cloud_stores') as create:
            with self.assertRaises(VersioningError):
                cli.preflight(worker, runtime, online=True)
        create.assert_not_called()

    def test_callable_rejects_missing_or_mismatched_runtime_credentials(self):
        worker, runtime = self.settings()
        with self.assertRaises(VersioningError):
            cli.preflight(worker, None, online=True)
        runtime.upstash_redis_rest_url = 'https://other.upstash.io'
        with self.assertRaises(VersioningError):
            cli.preflight(worker, runtime, online=True)

    def test_online_uses_both_tokens_only_with_writable_false(self):
        worker, runtime = self.settings()
        registry, vectors = Mock(), Mock()
        registry.read.return_value = (1, initial_state())
        runtime_registry = Mock()
        runtime_registry.read.return_value = (1, initial_state())
        with patch.object(cli, 'create_cloud_stores', side_effect=[(registry, vectors), (runtime_registry, Mock())]) as create:
            result = cli.preflight(worker, runtime, online=True)
        self.assertEqual(create.call_count, 2)
        self.assertTrue(all(call.kwargs == {'writable': False} for call in create.call_args_list))
        self.assertEqual(create.call_args_list[0].args[0].upstash_redis_rest_token, 'worker-secret')
        self.assertEqual(create.call_args_list[1].args[0].upstash_redis_rest_token, 'runtime-secret')
        self.assertEqual(vectors.method_calls, [('inspect', ('knowledge-preflight',), {})])
        self.assertTrue(result['worker_registry_read_succeeded'])
        self.assertTrue(result['runtime_registry_read_succeeded'])
        self.assertFalse(result['active_version_present'])

    def test_cli_error_is_sanitized(self):
        output = io.StringIO()
        with patch.object(cli, 'load_production_settings', side_effect=ValueError('secret-token')), contextlib.redirect_stdout(output):
            status = cli.main([])
        self.assertEqual(status, 1)
        self.assertNotIn('secret-token', output.getvalue())

    @staticmethod
    def settings():
        from api.core.config import Settings
        shared = dict(knowledge_environment='production', upstash_redis_rest_url='https://redis.upstash.io',
                      upstash_vector_rest_url='https://vector.upstash.io', upstash_vector_rest_token='vector-secret',
                      gemini_api_key='gemini-secret')
        return (Settings(**shared, upstash_redis_rest_token='worker-secret'),
                Settings(**shared, upstash_redis_rest_token='runtime-secret'))


if __name__ == '__main__':
    unittest.main()
