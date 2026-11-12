import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api.core.config import Settings
from api.knowledge import cloud
from api.knowledge.ports import VersioningError, initial_state
from api.tools import knowledge_cloud_preflight as cli


def settings(**changes):
    values = dict(upstash_redis_rest_url='https://test-redis.upstash.io',
                  upstash_redis_rest_token='secret-redis',
                  upstash_vector_rest_url='https://test-vector.upstash.io',
                  upstash_vector_rest_token='secret-vector', gemini_api_key='secret-gemini')
    values.update(changes)
    return Settings(**values)


class CloudSetupTests(unittest.TestCase):
    def test_only_background_writer_enables_embedding_retries(self):
        with patch.object(cloud, 'create_cloud_stores', return_value=(Mock(), Mock())), \
                patch.object(cloud, 'GeminiEmbedder') as embedder:
            cloud.create_cloud_service(settings(), writable=False)
            self.assertEqual(embedder.call_args.kwargs['max_attempts'], 1)
            self.assertFalse(embedder.call_args.kwargs['background'])
            cloud.create_cloud_service(settings(), writable=True)
            self.assertEqual(embedder.call_args.kwargs['max_attempts'], 3)
            self.assertTrue(embedder.call_args.kwargs['background'])

    def test_offline_configuration_never_constructs_clients(self):
        with patch.object(cloud, 'Index', side_effect=AssertionError('client created')):
            result = cli.preflight(settings())
        self.assertEqual(result['namespace_prefix'], 'digi-dan-test-knowledge-')
        self.assertEqual(result['registry_key'], 'digi-dan:knowledge:test:registry')
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(result['writes_performed'])
        self.assertFalse(result['embeddings_requested'])

    def test_invalid_credentials_and_urls_fail_before_clients(self):
        for changes in ({'gemini_api_key': ''}, {'embedding_dimensions': 3},
                        {'gemini_embedding_model': 'other'},
                        {'upstash_vector_rest_url': 'https://example.com'},
                        {'upstash_redis_rest_url': 'https://secret@test.upstash.io'},
                        {'upstash_vector_rest_url': 'https://test.upstash.io/?token=secret'}):
            with self.subTest(changes=changes), patch.object(cloud, 'Index') as index:
                with self.assertRaises(VersioningError):
                    cloud.create_cloud_stores(settings(**changes))
                index.assert_not_called()

    def test_factory_readonly_scopes_and_timeouts(self):
        index = SimpleNamespace(_client=SimpleNamespace(timeout=None))
        with patch.object(cloud, 'Index', return_value=index) as make_index, \
                patch.object(cloud, 'UpstashRedisRegistry') as registry, \
                patch.object(cloud, 'UpstashVectorStore') as vectors:
            cloud.create_cloud_stores(settings())
        self.assertFalse(registry.call_args.kwargs['writable'])
        self.assertFalse(vectors.call_args.kwargs['writable'])
        self.assertEqual(vectors.call_args.kwargs['environment'], 'test')
        self.assertEqual(index._client.timeout.read, 15)
        self.assertEqual(index._client.timeout.connect, 5)
        self.assertEqual(make_index.call_args.kwargs['retries'], 0)
        self.assertFalse(make_index.call_args.kwargs['allow_telemetry'])

    def test_pinned_sdk_client_timeout_can_be_configured_without_network(self):
        index = cloud.Index('https://example.upstash.io', 'fake', retries=0, allow_telemetry=False)
        try:
            index._client.timeout = cloud.httpx.Timeout(15, connect=5)
            self.assertEqual(index._client.timeout.read, 15)
            self.assertFalse(index._client.follow_redirects)
        finally:
            index._client.close()

    def test_production_writes_and_preflight_are_disabled(self):
        with patch.object(cloud, 'Index') as index:
            with self.assertRaisesRegex(VersioningError, 'Knowledge') as raised:
                cloud.create_cloud_stores(settings(knowledge_environment='production'), writable=True)
            self.assertEqual(raised.exception.code, 'production_writes_not_enabled')
            index.assert_not_called()
        with self.assertRaises(VersioningError):
            cli.preflight(settings(knowledge_environment='production'))

    def test_online_checks_only_read(self):
        registry, vectors = Mock(), Mock()
        registry.read.return_value = (0, initial_state())
        with patch.object(cli, 'create_cloud_stores', return_value=(registry, vectors)) as create:
            result = cli.preflight(settings(), online=True)
        create.assert_called_once_with(unittest.mock.ANY, writable=False)
        self.assertEqual(registry.method_calls, [('read', (), {})])
        self.assertEqual(vectors.method_calls, [('inspect', ('knowledge-preflight',), {})])
        self.assertEqual(result['status'], 'read_only_checks_passed')

    def test_loader_layers_files_without_modifying_process_or_live_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, overlay = Path(tmp) / '.env', Path(tmp) / '.env.cloud-test'
            base.write_text('GEMINI_API_KEY=base\nKNOWLEDGE_MODE=versioned\n')
            overlay.write_text("GEMINI_API_KEY='local-token'\nKNOWLEDGE_ENVIRONMENT='test'\n")
            result = cli.load_test_settings(overlay, base_file=base,
                                            environ={'GEMINI_API_KEY': 'process'})
            self.assertEqual(result.gemini_api_key, 'local-token')
            self.assertEqual(result.knowledge_mode, 'legacy')
            overlay.write_text('KNOWLEDGE_ENVIRONMENT=production\n')
            with self.assertRaises(VersioningError):
                cli.load_test_settings(overlay, base_file=base, environ={})

    def test_cli_sanitizes_failure(self):
        output = io.StringIO()
        with patch.object(cli, 'load_test_settings', side_effect=ValueError('secret-token')), \
                contextlib.redirect_stdout(output):
            status = cli.main([])
        self.assertEqual(status, 1)
        self.assertNotIn('secret-token', output.getvalue())


if __name__ == '__main__':
    unittest.main()
