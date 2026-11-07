"""Deterministic assembly of reviewed portfolio and supplemental knowledge."""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlsplit

from api.knowledge.manifest import KnowledgeValidationError, canonical_hash, validate_portfolio_manifest
from api.knowledge import resume_source

MAX_CHUNK_CHARS = 1200
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_FACT_ID = re.compile(r"^supplement:[A-Za-z0-9][A-Za-z0-9._:-]{0,247}$")
_CURATED_URL = "https://github.com/private-dev-ph/Digi-Dan/blob/main/daniel-zachary-rag-data.json"


def portfolio_review_hash(doc: dict) -> str:
    """Hash factual portfolio content while ignoring deployment-specific URLs."""
    if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not isinstance(doc.get("text"), str):
        _fail("invalid_portfolio_document")
    return canonical_hash({"id": doc["id"], "text": doc["text"]})


def _fail(code: str, message: str = "Knowledge candidate failed validation") -> None:
    raise KnowledgeValidationError(code, message)


def _string(value, maximum: int, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _fail(code)
    return value


def _serialized(value: dict) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise KnowledgeValidationError("invalid_input") from exc


def _split_text(text: str, prefix: str) -> list[str]:
    """Split while preserving every non-whitespace character from the source."""
    room = MAX_CHUNK_CHARS - len(prefix)
    if room <= 0:
        _fail("invalid_title")
    if len(text) <= room:
        return [text]
    chunks = []
    remaining = text
    while len(remaining) > room:
        floor = max(1, room // 2)
        sentence_ends = [match.end() for match in re.finditer(r"[.!?](?=\s)", remaining[:room]) if match.end() >= floor]
        newlines = [match.end() for match in re.finditer(r"\n", remaining[:room]) if match.end() >= floor]
        if sentence_ends or newlines:
            boundary = max(sentence_ends + newlines)
        else:
            whitespace = [match.end() for match in re.finditer(r"\s", remaining[:room]) if match.end() >= floor]
            boundary = max(whitespace) if whitespace else room
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _chunk(parent_id: str, text: str, metadata: dict) -> list[dict]:
    prefix = metadata["title"] + "\n"
    pieces = _split_text(text, prefix)
    count = len(pieces)
    result = []
    for index, piece in enumerate(pieces, 1):
        chunk_id = parent_id if count == 1 else f"{parent_id}:chunk:{index:04d}"
        meta = {**metadata, "parent_id": parent_id, "chunk_index": index, "chunk_count": count}
        chunk_text = prefix + piece
        result.append({"id": chunk_id, "text": chunk_text, "content_hash": canonical_hash({"id": chunk_id, "text": chunk_text, "metadata": meta}), "metadata": meta})
    return result


def _legacy_documents(legacy: dict) -> dict[str, dict]:
    if not isinstance(legacy, dict) or not isinstance(legacy.get("documents"), list):
        _fail("invalid_legacy")
    result = {}
    for doc in legacy["documents"]:
        if not isinstance(doc, dict) or set(doc) != {"id", "text", "metadata"}:
            _fail("invalid_legacy_document")
        doc_id = _string(doc["id"], 256, "invalid_legacy_id")
        if not _ID.fullmatch(doc_id) or doc_id in result or not isinstance(doc["text"], str) or not doc["text"].strip() or len(doc["text"]) > 30000 or not isinstance(doc["metadata"], dict):
            _fail("invalid_legacy_document")
        result[doc_id] = doc
    return result


def assemble_knowledge(portfolio: dict, resume: dict, legacy: dict, policy: dict, *, allow_local_http: bool = False) -> dict:
    """Validate every input and produce a stable, upload-safe candidate."""
    if not isinstance(portfolio, dict) or not isinstance(portfolio.get("source"), dict):
        _fail("invalid_portfolio")
    site_url = portfolio["source"].get("site_url")
    portfolio = validate_portfolio_manifest(_serialized(portfolio), site_url=site_url, allow_local_http=allow_local_http)

    try:
        from api.tools.prepare_resume import _validate_snapshot_obj
        _validate_snapshot_obj(resume)
    except Exception as exc:
        raise KnowledgeValidationError("invalid_resume") from exc
    resume_source_url = resume["source"]["url"]
    try:
        resume_source.validate_url(resume_source_url, allow_local_http=allow_local_http)
    except Exception as exc:
        raise KnowledgeValidationError("invalid_resume_source") from exc
    portfolio_origin = resume_source.origin(site_url)
    p = urlsplit(resume_source_url)
    is_repo = p.hostname and (p.hostname.lower() == "github.com" or p.hostname.lower().endswith(".githubusercontent.com"))
    if not is_repo and (resume_source.origin(resume_source_url) != portfolio_origin or not p.path.lower().endswith(".pdf")):
        _fail("invalid_resume_source")

    legacy_docs = _legacy_documents(legacy)
    if not isinstance(policy, dict) or set(policy) != {"schema_version", "policy_version", "resume_content_hash", "portfolio_review_hashes", "legacy_inventory", "supplemental_facts"} or type(policy.get("schema_version")) is not int or policy["schema_version"] != 1:
        _fail("invalid_policy")
    policy_version = _string(policy.get("policy_version"), 256, "invalid_policy")
    if policy.get("resume_content_hash") != resume.get("content_hash"):
        _fail("resume_review_required")

    inventory = policy.get("legacy_inventory")
    if not isinstance(inventory, list) or len(inventory) != len(legacy_docs):
        _fail("legacy_inventory_mismatch")
    inventory_by_id = {}
    allowed_dispositions = {"replaced", "omitted", "split", "retained"}
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"id", "content_hash", "disposition", "reason"}:
            _fail("invalid_legacy_inventory")
        doc_id = item["id"]
        if not isinstance(doc_id, str) or doc_id in inventory_by_id or doc_id not in legacy_docs:
            _fail("legacy_inventory_mismatch")
        if item["content_hash"] != canonical_hash(legacy_docs[doc_id]) or not isinstance(item["disposition"], str) or item["disposition"] not in allowed_dispositions:
            _fail("legacy_inventory_mismatch")
        _string(item["reason"], 1000, "invalid_legacy_inventory")
        inventory_by_id[doc_id] = item
    if set(inventory_by_id) != set(legacy_docs):
        _fail("legacy_inventory_mismatch")

    facts = policy.get("supplemental_facts")
    if not isinstance(facts, list) or len(facts) > 2000:
        _fail("invalid_supplemental_facts")
    page_map = {page["page_number"]: page["text"] for page in resume["pages"]}
    portfolio_by_id = {doc["id"]: doc for doc in portfolio["documents"]}
    review_hashes = policy["portfolio_review_hashes"]
    if not isinstance(review_hashes, dict):
        _fail("invalid_portfolio_review_hashes")
    for doc_id, digest in review_hashes.items():
        if not isinstance(doc_id, str) or not doc_id.startswith("portfolio:") or not _ID.fullmatch(doc_id) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("invalid_portfolio_review_hashes")
    identity_seen, ids_seen = set(), set()
    valid_portfolio_ids = set(portfolio_by_id)
    validated_facts = []
    for fact in facts:
        keys = {"id", "entity_id", "fact_key", "section", "title", "text", "owner", "sources", "requires_portfolio_ids", "portfolio_wins"}
        if not isinstance(fact, dict) or set(fact) != keys:
            _fail("invalid_supplemental_fact")
        fid = _string(fact["id"], 256, "invalid_supplemental_fact")
        if not _FACT_ID.fullmatch(fid) or fid in ids_seen:
            _fail("invalid_supplemental_id")
        ids_seen.add(fid)
        entity = _string(fact["entity_id"], 256, "invalid_supplemental_fact")
        fk = _string(fact["fact_key"], 256, "invalid_supplemental_fact")
        if not _ID.fullmatch(entity) or not _ID.fullmatch(fk) or (entity, fk) in identity_seen:
            _fail("duplicate_fact_identity")
        identity_seen.add((entity, fk))
        section = _string(fact["section"], 128, "invalid_supplemental_fact")
        title = _string(fact["title"], 512, "invalid_supplemental_fact")
        text = _string(fact["text"], 30000, "invalid_supplemental_fact")
        owner = fact["owner"]
        if not isinstance(owner, str) or owner not in {"resume", "curated"}:
            _fail("invalid_supplemental_fact")
        sources = fact["sources"]
        if not isinstance(sources, list) or not sources:
            _fail("invalid_supplemental_fact")
        source_kinds = set()
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("kind"), str) or source.get("kind") not in {"resume", "legacy"}:
                _fail("invalid_supplemental_source")
            kind = source["kind"]
            source_kinds.add(kind)
            if kind == "resume":
                if set(source) != {"kind", "page_number", "quote"} or type(source["page_number"]) is not int or source["page_number"] not in page_map:
                    _fail("invalid_supplemental_source")
                quote = _string(source["quote"], 2000, "invalid_supplemental_source")
                if quote not in page_map[source["page_number"]]:
                    _fail("source_quote_missing")
            else:
                if set(source) != {"kind", "document_id", "quote"} or not isinstance(source["document_id"], str) or source["document_id"] not in legacy_docs:
                    _fail("invalid_supplemental_source")
                quote = _string(source["quote"], 2000, "invalid_supplemental_source")
                if quote not in legacy_docs[source["document_id"]]["text"]:
                    _fail("source_quote_missing")
        if (owner == "resume" and "resume" not in source_kinds) or (owner == "curated" and "legacy" not in source_kinds):
            _fail("invalid_supplemental_source")
        for field in ("requires_portfolio_ids", "portfolio_wins"):
            refs = fact[field]
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not _ID.fullmatch(ref) for ref in refs) or len(set(refs)) != len(refs):
                _fail("invalid_supplemental_fact")
        if any(ref not in review_hashes for ref in fact["requires_portfolio_ids"]):
            _fail("unreviewed_portfolio_reference")
        if any(ref in portfolio_by_id and portfolio_review_hash(portfolio_by_id[ref]) != review_hashes[ref] for ref in fact["requires_portfolio_ids"]):
            _fail("portfolio_review_required")
        validated_facts.append(fact)

    chunks = []
    for doc in portfolio["documents"]:
        chunks.extend(_chunk(doc["id"], doc["text"], doc["metadata"]))
    supplemental_audit = []
    included = 0
    for fact in sorted(validated_facts, key=lambda f: f["id"]):
        absent = sorted(set(fact["requires_portfolio_ids"]) - valid_portfolio_ids)
        wins = sorted(set(fact["portfolio_wins"]) & valid_portfolio_ids)
        if absent or wins:
            why = "required_portfolio_id_absent" if absent else "portfolio_wins"
            supplemental_audit.append({"id": fact["id"], "status": "suppressed", "reason": why, "sources": fact["sources"]})
            continue
        src = portfolio["attachments"][0]["url"] if fact["owner"] == "resume" else _CURATED_URL
        metadata = {"section": fact["section"], "entity_id": fact["entity_id"], "source_url": src, "owner": fact["owner"], "title": fact["title"]}
        chunks.extend(_chunk(fact["id"], fact["text"], metadata))
        supplemental_audit.append({"id": fact["id"], "status": "included", "reason": "policy_validated", "sources": fact["sources"]})
        included += 1
    chunks.sort(key=lambda item: item["id"])
    if len({chunk["id"] for chunk in chunks}) != len(chunks):
        _fail("duplicate_chunk_id")
    chunk_payload = [{"id": c["id"], "text": c["text"], "metadata": c["metadata"]} for c in chunks]
    return {
        "schema_version": 1, "kind": "knowledge-candidate", "complete": True,
        "review_status": "policy-validated", "policy_version": policy_version,
        "content_hash": canonical_hash(chunk_payload), "chunks": chunks,
        "provenance": {"portfolio_source": copy.deepcopy(portfolio["source"]), "portfolio_content_hash": portfolio["content_hash"],
                       "resume_source": copy.deepcopy(resume["source"]), "resume_extractor": copy.deepcopy(resume["extractor"]),
                       "resume_content_hash": resume["content_hash"],
                       "legacy_content_hash": canonical_hash(sorted(legacy_docs.values(), key=lambda item: item["id"])),
                       "policy_hash": canonical_hash({**policy, "legacy_inventory": sorted(inventory, key=lambda item: item["id"]),
                                                       "supplemental_facts": sorted(facts, key=lambda item: item["id"])})},
        "audit": {"legacy_inventory": copy.deepcopy(sorted(inventory, key=lambda item: item["id"])), "supplemental": copy.deepcopy(supplemental_audit),
                  "counts": {"portfolio_documents": len(portfolio["documents"]), "legacy_documents": len(legacy_docs),
                             "supplemental_included": included, "supplemental_suppressed": len(supplemental_audit) - included,
                             "chunks": len(chunks)}}
    }
