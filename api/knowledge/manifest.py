"""Strict, read-only validation for portfolio knowledge manifests."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlsplit

from api.knowledge import resume_source

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_DOCUMENTS = 2000
MAX_TEXT_CHARS = 30000
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class KnowledgeValidationError(ValueError):
    def __init__(self, code: str, message: str = "Portfolio knowledge manifest is invalid"):
        super().__init__(message)
        self.code = code


def _fail(code: str) -> None:
    raise KnowledgeValidationError(code)


def canonical_hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise KnowledgeValidationError("invalid_hash_value") from exc
    return hashlib.sha256(encoded).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate_json_key")
        result[key] = value
    return result


def _constant(_: str) -> None:
    _fail("invalid_json_number")


def _obj(value: Any, keys: set[str], code: str = "invalid_schema") -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(code)
    return value


def _string(value: Any, maximum: int, code: str = "invalid_schema", *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value.strip()):
        _fail(code)
    return value


def _site(value: Any, allow_local_http: bool) -> tuple[str, tuple[str, str, int]]:
    try:
        checked = resume_source.validate_url(value, allow_local_http=allow_local_http)
        parts = urlsplit(checked)
        if parts.path not in ("", "/"):
            _fail("invalid_site_url")
        return checked, resume_source.origin(checked)
    except KnowledgeValidationError:
        raise
    except (resume_source.SourceError, TypeError, ValueError) as exc:
        raise KnowledgeValidationError("invalid_site_url") from exc


def _same_site_url(value: Any, expected_origin: tuple[str, str, int], allow_local_http: bool) -> str:
    try:
        checked = resume_source.validate_url(value, allow_local_http=allow_local_http)
        if resume_source.origin(checked) != expected_origin:
            _fail("origin_mismatch")
        return checked
    except KnowledgeValidationError:
        raise
    except (resume_source.SourceError, TypeError, ValueError) as exc:
        raise KnowledgeValidationError("invalid_source_url") from exc


def validate_portfolio_manifest(data: bytes, *, site_url: str, allow_local_http: bool = False) -> dict:
    """Parse and verify a complete export against the caller's trusted site origin."""
    if not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES:
        _fail("manifest_size_limit")
    try:
        raw = data.decode("utf-8", errors="strict")
        manifest = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except KnowledgeValidationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise KnowledgeValidationError("invalid_json") from exc

    _, expected_origin = _site(site_url, allow_local_http)
    root = _obj(manifest, {"schema_version", "source", "complete", "content_hash", "documents", "attachments"})
    if type(root["schema_version"]) is not int or root["schema_version"] != 1 or root["complete"] is not True:
        _fail("incomplete_manifest")
    source = _obj(root["source"], {"site_url", "deployment_id", "commit_sha"})
    source_site, source_origin = _site(source["site_url"], allow_local_http)
    if source_origin != expected_origin:
        _fail("origin_mismatch")
    # source must express the same canonical origin and cannot smuggle a path/query.
    if source_site.rstrip("/") != _origin_text(expected_origin):
        _fail("invalid_site_url")
    for field in ("deployment_id", "commit_sha"):
        item = source[field]
        if item is not None:
            _string(item, 512, "invalid_source")

    docs = root["documents"]
    if not isinstance(docs, list) or not docs or len(docs) > MAX_DOCUMENTS:
        _fail("invalid_documents")
    seen: set[str] = set()
    semantic: list[dict[str, Any]] = []
    for doc in docs:
        doc = _obj(doc, {"id", "text", "content_hash", "metadata"}, "invalid_document")
        doc_id = _string(doc["id"], 256, "invalid_id")
        if not doc_id.startswith("portfolio:") or not _ID.fullmatch(doc_id) or doc_id in seen:
            _fail("invalid_or_duplicate_id")
        seen.add(doc_id)
        text = _string(doc["text"], MAX_TEXT_CHARS, "invalid_document_text")
        metadata = _obj(doc["metadata"], {"section", "entity_id", "source_url", "owner", "title"}, "invalid_metadata")
        section = _string(metadata["section"], 128, "invalid_metadata")
        entity_id = _string(metadata["entity_id"], 256, "invalid_metadata")
        if not _ID.fullmatch(entity_id):
            _fail("invalid_metadata")
        source_url = _same_site_url(metadata["source_url"], expected_origin, allow_local_http)
        owner = _string(metadata["owner"], 64, "invalid_metadata")
        title = _string(metadata["title"], 512, "invalid_metadata")
        if owner != "portfolio":
            _fail("invalid_metadata")
        metadata = {"section": section, "entity_id": entity_id, "source_url": source_url, "owner": owner, "title": title}
        item = {"id": doc_id, "text": text, "metadata": metadata}
        content_hash = _string(doc["content_hash"], 64, "invalid_document_hash")
        if not _HASH.fullmatch(content_hash) or canonical_hash(item) != content_hash:
            _fail("document_hash_mismatch")
        semantic.append(item)

    declared_hash = _string(root["content_hash"], 64, "invalid_manifest_hash")
    if not _HASH.fullmatch(declared_hash) or canonical_hash(sorted(semantic, key=lambda item: item["id"])) != declared_hash:
        _fail("manifest_hash_mismatch")

    attachments = root["attachments"]
    if not isinstance(attachments, list) or len(attachments) != 1:
        _fail("invalid_attachments")
    attachment = _obj(attachments[0], {"id", "title", "url", "media_type"}, "invalid_attachment")
    if attachment["id"] != "resume" or attachment["media_type"] != "application/pdf":
        _fail("invalid_attachment")
    _string(attachment["title"], 512, "invalid_attachment")
    _same_site_url(attachment["url"], expected_origin, allow_local_http)
    return manifest


def _origin_text(origin: tuple[str, str, int]) -> str:
    scheme, host, port = origin
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{rendered_host}" + (f":{port}" if port != default_port else "")
