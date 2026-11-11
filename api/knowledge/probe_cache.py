"""Validated, bounded receipts for previously verified retrieval probes."""
from __future__ import annotations

import copy
import json
import math
import re

from api.knowledge.manifest import canonical_hash
from api.knowledge.ports import VersioningError

PROBE_CACHE_POLICY = "retrieval-probes-v1"
_MAX_BYTES = 256 * 1024
_MAX_PROBES = 32
_FIELDS = {"schema_version", "policy_version", "binding", "embedding_dimensions", "probe_count", "vectors", "verified_at", "content_hash"}


def _fail() -> None:
    raise VersioningError("invalid_probe_cache")


def _serialized_size(value) -> int:
    try:
        return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                              allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise VersioningError("invalid_probe_cache") from exc


def _validate_vector(vector, dimensions: int) -> None:
    if not isinstance(vector, list) or len(vector) != dimensions:
        _fail()
    for value in vector:
        if type(value) not in (int, float) or not math.isfinite(value):
            _fail()
    try:
        norm = math.hypot(*vector)
    except (OverflowError, TypeError, ValueError) as exc:
        raise VersioningError("invalid_probe_cache") from exc
    if not math.isfinite(norm) or norm == 0:
        _fail()


def _binding(record, probes) -> str:
    if not isinstance(record, dict) or not isinstance(record.get("embedding"), dict):
        _fail()
    try:
        return canonical_hash({"ticket": record["ticket"], "namespace": record["namespace"],
                               "version_id": record["version_id"], "candidate_hash": record["candidate_hash"],
                               "embedding": record["embedding"], "probes": probes})
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise VersioningError("invalid_probe_cache") from exc


def make_probe_cache(record, probes, vectors, verified_at) -> dict:
    """Build a defensive-copy receipt, rejecting data that cannot be safely persisted."""
    try:
        if not isinstance(probes, list) or not 1 <= len(probes) <= _MAX_PROBES:
            _fail()
        dimensions = record["embedding"]["dimensions"]
        if type(dimensions) is not int or not 1 <= dimensions <= 65536:
            _fail()
        if not isinstance(vectors, list) or len(vectors) != len(probes):
            _fail()
        for vector in vectors:
            _validate_vector(vector, dimensions)
        if type(verified_at) not in (int, float) or not math.isfinite(verified_at) or verified_at < 0:
            _fail()
        binding = _binding(record, probes)
        payload = {"schema_version": 1, "policy_version": PROBE_CACHE_POLICY, "binding": binding,
                   "embedding_dimensions": dimensions, "probe_count": len(probes),
                   "vectors": copy.deepcopy(vectors), "verified_at": verified_at}
        receipt = {**payload, "content_hash": canonical_hash(payload)}
        if _serialized_size(receipt) > _MAX_BYTES:
            _fail()
        return receipt
    except VersioningError as exc:
        if exc.code == "invalid_probe_cache":
            raise
        raise VersioningError("invalid_probe_cache") from exc
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise VersioningError("invalid_probe_cache") from exc


def read_probe_cache(record, probes) -> dict | None:
    """Return a validated cache; return None when a valid receipt is stale or absent."""
    if not isinstance(record, dict) or "probe_cache" not in record:
        return None
    receipt = record["probe_cache"]
    try:
        if not isinstance(receipt, dict) or set(receipt) != _FIELDS or _serialized_size(receipt) > _MAX_BYTES:
            _fail()
        if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
            _fail()
        if not isinstance(receipt["policy_version"], str):
            _fail()
        if not receipt["policy_version"].strip() or len(receipt["policy_version"]) > 128:
            _fail()
        if not isinstance(receipt["binding"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["binding"]):
            _fail()
        dimensions, count = receipt["embedding_dimensions"], receipt["probe_count"]
        if type(dimensions) is not int or not 1 <= dimensions <= 65536 or type(count) is not int or not 1 <= count <= _MAX_PROBES:
            _fail()
        vectors = receipt["vectors"]
        if not isinstance(vectors, list) or len(vectors) != count:
            _fail()
        for vector in vectors:
            _validate_vector(vector, dimensions)
        timestamp = receipt["verified_at"]
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
            _fail()
        content_hash = receipt["content_hash"]
        payload = {key: receipt[key] for key in _FIELDS if key != "content_hash"}
        if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash) or canonical_hash(payload) != content_hash:
            _fail()
        # Integrity and internal shape are checked before deciding a receipt is stale.
        if receipt["policy_version"] != PROBE_CACHE_POLICY:
            return None
        if not isinstance(probes, list) or len(probes) != count:
            return None
        current_embedding = record.get("embedding")
        if not isinstance(current_embedding, dict):
            _fail()
        if type(current_embedding.get("dimensions")) is not int:
            return None
        current_binding = _binding(record, probes)
        if receipt["binding"] != current_binding or receipt["embedding_dimensions"] != current_embedding["dimensions"]:
            return None
        return copy.deepcopy(receipt)
    except VersioningError as exc:
        if exc.code == "invalid_probe_cache":
            raise
        raise VersioningError("invalid_probe_cache") from exc
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise VersioningError("invalid_probe_cache") from exc
