"""Validate production cloud credentials and optionally perform read-only checks."""
import argparse
import json
import os
from pathlib import Path

from api.core.config import Settings
from api.knowledge.cloud import cloud_configuration, create_cloud_stores
from api.knowledge.ports import VersioningError
from api.tools.knowledge_cloud_preflight import FIELDS, REPO, _values

def load_production_settings(env_file, *, base_file=None, environ=None):
    """Load a strict production overlay and a separately credentialed runtime config."""
    env_file = Path(env_file)
    if not env_file.is_file():
        raise VersioningError('production_env_file_missing')
    try:
        explicit = _values(env_file)
        for key in ('KNOWLEDGE_ENVIRONMENT', 'UPSTASH_REDIS_REST_URL',
                    'UPSTASH_REDIS_REST_TOKEN', 'UPSTASH_REDIS_REST_READ_ONLY_TOKEN'):
            if not isinstance(explicit.get(key), str) or not explicit[key].strip():
                raise ValueError()
        if explicit['KNOWLEDGE_ENVIRONMENT'] != 'production':
            raise VersioningError('production_environment_required')
        worker_token = explicit['UPSTASH_REDIS_REST_TOKEN']
        runtime_token = explicit['UPSTASH_REDIS_REST_READ_ONLY_TOKEN']
        if worker_token != worker_token.strip() or runtime_token != runtime_token.strip():
            raise ValueError()
        if worker_token == runtime_token:
            raise VersioningError('distinct_production_tokens_required')

        values = _values(base_file or REPO / '.env')
        values.update(os.environ if environ is None else environ)
        values.update(explicit)
        # Redis endpoint and both Redis credentials must originate in the file.
        values['UPSTASH_REDIS_REST_TOKEN'] = worker_token
        worker = Settings(**{field: values[key] for key, field in FIELDS.items()
                             if values.get(key) is not None})
        runtime_values = dict(values)
        runtime_values['UPSTASH_REDIS_REST_TOKEN'] = runtime_token
        runtime = Settings(**{field: runtime_values[key] for key, field in FIELDS.items()
                              if runtime_values.get(key) is not None})
    except VersioningError:
        raise
    except Exception:
        raise VersioningError('invalid_cloud_configuration') from None
    if worker.knowledge_environment != 'production' or runtime.knowledge_environment != 'production':
        raise VersioningError('production_environment_required')
    return worker, runtime


def preflight(worker_settings, runtime_settings, *, online=False):
    if worker_settings.knowledge_environment != 'production':
        raise VersioningError('production_environment_required')
    if not isinstance(runtime_settings, Settings):
        raise VersioningError('missing_runtime_configuration')
    if runtime_settings.knowledge_environment != 'production':
        raise VersioningError('production_environment_required')
    if (worker_settings.upstash_redis_rest_url != runtime_settings.upstash_redis_rest_url
            or not worker_settings.upstash_redis_rest_token
            or not runtime_settings.upstash_redis_rest_token
            or worker_settings.upstash_redis_rest_token == runtime_settings.upstash_redis_rest_token):
        raise VersioningError('distinct_production_tokens_required')
    report = cloud_configuration(worker_settings)
    cloud_configuration(runtime_settings)
    report.update({'status': 'configuration_valid', 'online': online,
                   'writes_performed': False, 'embeddings_requested': False,
                   'live_chat_mode_changed': False})
    if online:
        registry, vectors = create_cloud_stores(worker_settings, writable=False)
        revision, state = registry.read()
        vectors.inspect('knowledge-preflight')
        runtime_registry, _ = create_cloud_stores(runtime_settings, writable=False)
        runtime_revision, runtime_state = runtime_registry.read()
        report.update({'status': 'read_only_checks_passed',
                       'registry_revision': revision,
                       'registry_generation': state['generation'],
                       'active_version_present': state['active'] is not None,
                       'index_dimensions_verified': True,
                       'runtime_registry_read_succeeded': True,
                       'worker_registry_read_succeeded': True,
                       'runtime_registry_revision': runtime_revision,
                       'runtime_registry_generation': runtime_state['generation']})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=REPO / '.env.cloud-production')
    parser.add_argument('--online', action='store_true',
                        help='Read production registry and Vector index information')
    args = parser.parse_args(argv)
    try:
        worker, runtime = load_production_settings(args.env_file)
        result = preflight(worker, runtime, online=args.online)
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
