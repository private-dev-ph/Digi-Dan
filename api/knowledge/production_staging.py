"""Narrow production build path: stage verified versions without activation."""
from __future__ import annotations

from api.knowledge.ports import VersioningError
from api.knowledge.versioning import _validate


class ProductionStager:
    """Build and verify a production version while leaving active untouched."""

    def __init__(self, service, embedding_profile, verifier):
        self._service = service
        self._embedding_profile = embedding_profile
        self._verifier = verifier

    def _verify_candidate(self, candidate):
        self._verifier.verify({"candidate_hash": candidate["content_hash"],
                               "source": candidate["provenance"]})

    def stage(self, candidate, *, expected_generation, probes):
        # Validate all caller-controlled structure before any remote reads.
        _validate(candidate, self._embedding_profile, probes)
        if type(expected_generation) is not int or expected_generation < 0:
            raise VersioningError("invalid_generation")

        self._verify_candidate(candidate)
        vectors = self._service.vectors
        vectors.inspect("knowledge-preflight")

        result = self._service.sync(
            candidate, self._embedding_profile,
            expected_generation=expected_generation,
            verify_source=self._verifier,
            probes=probes,
            activate=False,
        )
        # Source can move during embedding/upload/probes; verify again before
        # treating the inactive record as a successful stage.
        self._verify_candidate(candidate)
        if result.get("status") not in {"staged", "unchanged"}:
            raise VersioningError("stage_not_created")
        physical_namespace = "digi-dan-production-{}".format(result["namespace"])
        return {**result,
                "chunk_count": len(candidate["chunks"]),
                "probe_count": len(probes),
                "candidate_hash": candidate["content_hash"],
                "physical_namespace": physical_namespace,
                "activation_performed": False}


def create_production_stager(settings, *, site_url):
    """Create the sole production writable path, bound to a trusted site."""
    from api.knowledge.cloud import _create_cloud_stores, cloud_configuration
    from api.knowledge.cloud_embeddings import GeminiEmbedder
    from api.knowledge.deployed_source import DeployedSourceVerifier
    from api.knowledge.versioning import KnowledgeService

    config = cloud_configuration(settings)
    if config["environment"] != "production":
        raise VersioningError("production_environment_required")
    verifier = DeployedSourceVerifier(site_url)
    registry, vectors = _create_cloud_stores(settings, writable=True,
                                             production_staging=True)
    service = KnowledgeService(registry, vectors,
                               GeminiEmbedder(settings.gemini_api_key, max_attempts=3, background=True),
                               poll_attempts=12, poll_interval=1.0)
    return ProductionStager(service, config["embedding_profile"],
                            verifier)
