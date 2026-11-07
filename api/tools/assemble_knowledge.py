"""Build a reviewable knowledge candidate without contacting embedding/vector services."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from api.knowledge.assembly import assemble_knowledge
from api.knowledge.manifest import KnowledgeValidationError, canonical_hash, validate_portfolio_manifest
from api.knowledge import resume_source
from api.tools.prepare_resume import _atomic_write, _json_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT_BYTES = 2 * 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _read(path: Path, limit: int = MAX_INPUT_BYTES) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise KnowledgeValidationError("size_limit", "Input exceeds its size limit")
    return data


def _read_json(path: Path, limit: int = MAX_INPUT_BYTES) -> dict:
    try:
        value = json.loads(_read(path, limit).decode("utf-8"), object_pairs_hook=_object, parse_constant=_reject_constant)
    except KnowledgeValidationError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise KnowledgeValidationError("invalid_json", "Input must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise KnowledgeValidationError("invalid_input", "Input must be a JSON object")
    return value


def _previous(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = _read_json(path, 16 * 1024 * 1024)
    chunks = value.get("chunks")
    if (value.get("kind") != "knowledge-candidate" or value.get("complete") is not True
            or value.get("review_status") != "policy-validated" or not isinstance(chunks, list) or not chunks):
        raise KnowledgeValidationError("state_conflict", "Output contains invalid or unrelated state")
    semantic = [{"id": x["id"], "text": x["text"], "metadata": x["metadata"]} for x in chunks]
    semantic.sort(key=lambda x: x["id"])
    if value.get("content_hash") != canonical_hash(semantic):
        raise KnowledgeValidationError("state_conflict", "Previous candidate hash is invalid")
    return value


def build_candidate(*, output_dir: Path, site_url: str, resume_path: Path,
                    policy_path: Path, legacy_path: Path, manifest_path: Path | None = None,
                    manifest_url: str | None = None, resume_status_path: Path | None = None,
                    allow_local_http: bool = False) -> dict:
    """Validate all inputs, then atomically replace a local candidate; failures retain it."""
    output_dir = Path(output_dir)
    snapshot_path = output_dir / "last-successful.json"
    status_path = output_dir / "status.json"
    lock_path = output_dir / ".assemble.lock"
    resume_status_path = Path(resume_status_path) if resume_status_path else Path(resume_path).with_name("status.json")
    inputs = [Path(resume_path), Path(policy_path), Path(legacy_path), resume_status_path]
    if manifest_path is not None:
        inputs.append(Path(manifest_path))
    output_paths = {p.resolve() for p in (snapshot_path, status_path, lock_path)}
    if any(p.resolve() in output_paths for p in inputs):
        raise KnowledgeValidationError("output_conflict", "Output must not overwrite any input")
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    previous = None
    attempted_at = datetime.now(timezone.utc).isoformat()
    try:
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise KnowledgeValidationError("state_locked", "Another assembly is using this output directory") from exc
        previous = _previous(snapshot_path)
        if bool(manifest_path) == bool(manifest_url):
            raise KnowledgeValidationError("invalid_arguments", "Provide exactly one manifest input")
        resume_source.validate_url(site_url, allow_local_http=allow_local_http)
        if manifest_url:
            resume_source.validate_url(manifest_url, allow_local_http=allow_local_http)
            if resume_source.origin(manifest_url) != resume_source.origin(site_url):
                raise KnowledgeValidationError("origin_mismatch", "Manifest URL must match the configured site")
            raw_manifest, media_type = resume_source.fetch_bytes(
                manifest_url, limit=resume_source.MAX_MANIFEST_BYTES, allow_local_http=allow_local_http)
            if media_type not in {"application/json", "text/json", "application/octet-stream"}:
                raise KnowledgeValidationError("invalid_manifest", "Manifest response is not JSON")
        else:
            raw_manifest = _read(Path(manifest_path), resume_source.MAX_MANIFEST_BYTES)
        portfolio = validate_portfolio_manifest(raw_manifest, site_url=site_url, allow_local_http=allow_local_http)
        resume_status = _read_json(resume_status_path)
        if (resume_status.get("status") not in {"changed", "unchanged"}
                or resume_status.get("last_successful_available") is not True):
            raise KnowledgeValidationError("resume_preparation_failed", "The latest resume preparation must have succeeded")
        candidate = assemble_knowledge(portfolio, _read_json(Path(resume_path)),
                                       _read_json(Path(legacy_path)), _read_json(Path(policy_path)),
                                       allow_local_http=allow_local_http)
        candidate["provenance"]["assembly_input"] = "fetched-manifest" if manifest_url else "local-manifest"
        # Retain the newest validated provenance even when semantic content is unchanged.
        _atomic_write(snapshot_path, _json_bytes(candidate))
        status = {
            "status": "unchanged" if previous and previous["content_hash"] == candidate["content_hash"] else "changed",
            "last_attempt_at": attempted_at,
            "last_success_at": datetime.now(timezone.utc).isoformat(),
            "last_successful_available": True,
            "content_hash": candidate["content_hash"],
            "chunk_count": len(candidate["chunks"]),
            "active_knowledge_changed": False,
        }
        _atomic_write(status_path, _json_bytes(status))
        return status
    except Exception as exc:
        code = str(getattr(exc, "code", "assembly_failed"))[:64]
        if lock_fd is not None:
            _atomic_write(status_path, _json_bytes({
                "status": "failed", "code": code,
                "diagnostic": "Knowledge assembly failed validation. Review inputs and source policy.",
                "last_attempt_at": attempted_at,
                "last_successful_available": previous is not None,
                "active_knowledge_changed": False,
            }))
        raise
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--manifest-url")
    parser.add_argument("--site-url", required=True, help="Trusted canonical portfolio origin")
    parser.add_argument("--resume", required=True, type=Path, help="Validated resume last-successful.json")
    parser.add_argument("--resume-status", type=Path, help="Defaults to status.json beside the resume snapshot")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "knowledge" / "source-policy.json")
    parser.add_argument("--legacy", type=Path, default=REPO_ROOT / "daniel-zachary-rag-data.json")
    parser.add_argument("--output-dir", type=Path, default=Path(".knowledge-state"))
    parser.add_argument("--allow-local-http", action="store_true")
    args = parser.parse_args()
    try:
        result = build_candidate(output_dir=args.output_dir, site_url=args.site_url,
                                 resume_path=args.resume, resume_status_path=args.resume_status,
                                 policy_path=args.policy, legacy_path=args.legacy,
                                 manifest_path=args.manifest, manifest_url=args.manifest_url,
                                 allow_local_http=args.allow_local_http)
        print(f"{result['status']}: {result['chunk_count']} chunks; {result['content_hash']}")
        return 0
    except Exception as exc:
        code = getattr(exc, "code", "assembly_failed")
        print(f"Knowledge assembly failed ({code}); no live knowledge was changed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
