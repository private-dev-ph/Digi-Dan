"""Explicit cloud construction; importing this module never contacts a service.

The application still uses its existing legacy configuration. These factories
are an opt-in seam for cloud integration, not a deployment or mode switch.
"""
import re
from urllib.parse import urlsplit

import httpx
from upstash_vector import Index

from .cloud_embeddings import GeminiEmbedder, gemini_profile
from .cloud_vectors import UpstashVectorStore
from .ports import VersioningError
from .redis_registry import UpstashRedisRegistry
from .versioning import KnowledgeService


def _url(value):
    try:
        parsed = urlsplit(value or '')
        if (parsed.scheme != 'https' or not parsed.hostname
                or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.upstash\.io', parsed.hostname)
                or parsed.username or parsed.password or parsed.port
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
        return value.rstrip('/')
    except Exception:
        raise VersioningError('invalid_cloud_url') from None


def cloud_configuration(settings):
    """Validate configuration without network calls or returning credentials."""
    environment = settings.knowledge_environment
    if environment not in {'test', 'production'}:
        raise VersioningError('invalid_cloud_environment')
    required = ('upstash_redis_rest_url', 'upstash_redis_rest_token',
                'upstash_vector_rest_url', 'upstash_vector_rest_token', 'gemini_api_key')
    if any(not isinstance(getattr(settings, key, None), str)
           or not getattr(settings, key).strip() for key in required):
        raise VersioningError('missing_cloud_credentials')
    _url(settings.upstash_redis_rest_url)
    _url(settings.upstash_vector_rest_url)
    profile = gemini_profile(dimensions=settings.embedding_dimensions,
                             model=settings.gemini_embedding_model)
    return {'environment': environment,
            'registry_key': f'digi-dan:knowledge:{environment}:registry',
            'namespace_prefix': f'digi-dan-{environment}-knowledge-',
            'embedding_profile': profile}


def _create_cloud_stores(settings, *, writable=False, production_staging=False):
    config = cloud_configuration(settings)
    # Production writes require the deployment/source-verification milestone.
    if writable and config['environment'] != 'test' and not production_staging:
        raise VersioningError('production_writes_not_enabled')
    registry = UpstashRedisRegistry(
        _url(settings.upstash_redis_rest_url), settings.upstash_redis_rest_token,
        config['registry_key'], writable=writable)
    index = Index(url=_url(settings.upstash_vector_rest_url),
                  token=settings.upstash_vector_rest_token, retries=0,
                  allow_telemetry=False)
    # upstash-vector 0.8.0 has no public timeout argument. Keep this one
    # pinned-SDK compatibility adjustment here and cover it in isolated tests.
    index._client.timeout = httpx.Timeout(15.0, connect=5.0)
    vectors = UpstashVectorStore(index, environment=config['environment'],
                                dimensions=settings.embedding_dimensions,
                                writable=writable)
    return registry, vectors


def create_cloud_stores(settings, *, writable=False):
    """Construct cloud stores; production writes remain disabled here."""
    return _create_cloud_stores(settings, writable=writable)


def create_cloud_service(settings, *, writable=False):
    registry, vectors = create_cloud_stores(settings, writable=writable)
    return KnowledgeService(registry, vectors, GeminiEmbedder(settings.gemini_api_key,
                            max_attempts=3 if writable else 1, background=writable),
                            poll_attempts=12, poll_interval=1.0)


def create_production_stager(settings, *, site_url):
    """Create a source-bound production staging service without activation APIs."""
    from .production_staging import create_production_stager as factory
    return factory(settings, site_url=site_url)
