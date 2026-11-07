"""Validate isolated cloud configuration; --online performs read-only checks."""
import argparse
import json
import logging
import os
from pathlib import Path

from dotenv import dotenv_values

from api.core.config import Settings
from api.knowledge.cloud import cloud_configuration, create_cloud_stores
from api.knowledge.ports import VersioningError

REPO = Path(__file__).resolve().parents[2]
FIELDS = {
    'UPSTASH_REDIS_REST_URL': 'upstash_redis_rest_url',
    'UPSTASH_REDIS_REST_TOKEN': 'upstash_redis_rest_token',
    'UPSTASH_VECTOR_REST_URL': 'upstash_vector_rest_url',
    'UPSTASH_VECTOR_REST_TOKEN': 'upstash_vector_rest_token',
    'GEMINI_API_KEY': 'gemini_api_key',
    'GEMINI_EMBEDDING_MODEL': 'gemini_embedding_model',
    'EMBEDDING_DIMENSIONS': 'embedding_dimensions',
    'KNOWLEDGE_ENVIRONMENT': 'knowledge_environment',
}


def _values(path):
    # Existing .env may contain unrelated prose. Never evaluate it as shell
    # code, interpolate environment references, or print its values/errors.
    logger = logging.getLogger('dotenv.main')
    previous = logger.disabled
    logger.disabled = True
    try:
        return dotenv_values(path, interpolate=False)
    finally:
        logger.disabled = previous


def load_test_settings(env_file, *, base_file=None, environ=None):
    env_file = Path(env_file)
    if not env_file.is_file():
        raise VersioningError('test_env_file_missing')
    try:
        values = _values(base_file or REPO / '.env')
        values.update(os.environ if environ is None else environ)
        values.update(_values(env_file))
        settings = Settings(**{field: values[key] for key, field in FIELDS.items()
                               if values.get(key) is not None})
    except Exception:
        raise VersioningError('invalid_cloud_configuration') from None
    if settings.knowledge_environment != 'test':
        raise VersioningError('test_environment_required')
    return settings


def preflight(settings, *, online=False):
    if settings.knowledge_environment != 'test':
        raise VersioningError('test_environment_required')
    report = cloud_configuration(settings)
    report.update({'status': 'configuration_valid', 'online': online,
                   'writes_performed': False, 'embeddings_requested': False,
                   'live_chat_mode_changed': False})
    if online:
        registry, vectors = create_cloud_stores(settings, writable=False)
        revision, state = registry.read()
        # Reserved managed name: inspect only, never creates this namespace.
        vectors.inspect('knowledge-preflight')
        report.update({'status': 'read_only_checks_passed', 'registry_revision': revision,
                       'registry_generation': state['generation'],
                       'active_version_present': state['active'] is not None,
                       'index_dimensions_verified': True})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=REPO / '.env.cloud-test')
    parser.add_argument('--online', action='store_true',
                        help='Read test Redis registry and Vector index information; no writes or embeddings')
    args = parser.parse_args(argv)
    try:
        result = preflight(load_test_settings(args.env_file), online=args.online)
    except VersioningError as exc:
        print(json.dumps({'status': 'not_ready', 'error_code': exc.code}))
        return 1
    except Exception:
        print(json.dumps({'status': 'not_ready', 'error_code': 'cloud_preflight_failed'}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
