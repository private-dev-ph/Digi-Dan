"""Controlled activation and rollback for reviewed production knowledge builds."""
from __future__ import annotations

import copy
import re

from api.knowledge.ports import VersioningError
from api.knowledge.versioning import _validate


_TICKET = re.compile(r"[0-9a-f]{16}\Z")


def _generation(value):
    if type(value) is not int or value < 0:
        raise VersioningError("invalid_generation")


class ProductionRollout:
    """Activate or roll back only a verified record matching a reviewed receipt."""

    def __init__(self, service, embedding_profile, verifier):
        self._service = service
        self._embedding_profile = copy.deepcopy(embedding_profile)
        self._verifier = verifier

    def _matching_record(self, ticket, candidate):
        if not isinstance(ticket, str) or not _TICKET.fullmatch(ticket):
            raise VersioningError("invalid_ticket")
        rec = self._service._record(ticket)
        _, candidate_hash, version_id = _validate(
            candidate, self._embedding_profile, rec.get("probes"),
        )
        if (rec.get("candidate_hash") != candidate_hash or
                rec.get("version_id") != version_id or
                rec.get("source") != candidate.get("provenance") or
                rec.get("embedding") != self._embedding_profile):
            raise VersioningError("candidate_mismatch")
        return rec

    def activate(self, ticket, candidate, *, expected_generation):
        _generation(expected_generation)
        rec = self._matching_record(ticket, candidate)
        state = self._service._read()[1]
        if rec.get("status") != "verified" or state.get("desired") != ticket:
            raise VersioningError("version_not_verified")
        result = self._service.activate(
            ticket, verify_source=self._verifier,
            expected_generation=expected_generation,
        )
        return {**result, "activation_performed": True,
                "physical_namespace": "digi-dan-production-" + result["namespace"]}

    def rollback(self, ticket, candidate, *, expected_generation):
        _generation(expected_generation)
        self._matching_record(ticket, candidate)
        state = self._service._read()[1]
        if ticket not in ([state.get("active")] + state.get("history", [])):
            raise VersioningError("version_not_verified")
        result = self._service.rollback(
            ticket, expected_generation=expected_generation,
            verify_source=self._verifier,
        )
        return {**result, "activation_performed": True,
                "physical_namespace": "digi-dan-production-" + result["namespace"]}


def create_production_rollout(settings, site_url):
    """Construct the production activation service with vectors explicitly read-only."""
    from api.knowledge.cloud import _create_cloud_stores, cloud_configuration
    from api.knowledge.cloud_embeddings import GeminiEmbedder
    from api.knowledge.deployed_source import DeployedSourceVerifier
    from api.knowledge.versioning import KnowledgeService

    config = cloud_configuration(settings)
    if config["environment"] != "production":
        raise VersioningError("production_environment_required")
    # Fail malformed or untrusted deployment URLs before constructing clients.
    verifier = DeployedSourceVerifier(site_url)
    registry, vectors = _create_cloud_stores(
        settings, writable=True, production_staging=True,
    )
    vectors.writable = False
    service = KnowledgeService(
        registry, vectors,
        GeminiEmbedder(settings.gemini_api_key, max_attempts=3),
        poll_attempts=12, poll_interval=1.0,
    )
    return ProductionRollout(service, config["embedding_profile"], verifier)
