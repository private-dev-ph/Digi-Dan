"""Prepare and preserve a validated resume extraction snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from api.knowledge import resume_source

PIPELINE_VERSION = "2"  # Bump when parsing, normalization, or OCR policy changes.


class PreparationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_hash(pages: list[dict]) -> str:
    normalized = [{"page_number": p["page_number"], "text": " ".join(p["text"].split())} for p in pages]
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _json_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _validate_snapshot(raw: bytes) -> dict:
    try:
        snapshot = json.loads(raw.decode("utf-8"))
        _validate_snapshot_obj(snapshot)
        return snapshot
    except Exception as exc:
        raise PreparationError("cache_corrupt", "Cached successful snapshot is invalid") from exc


def _validate_snapshot_obj(snapshot: dict) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValueError(message)

    require(isinstance(snapshot, dict), "snapshot must be an object")
    require(type(snapshot.get("schema_version")) is int and snapshot["schema_version"] == 1 and snapshot.get("kind") == "resume-extraction", "snapshot schema")
    require(snapshot.get("review_status") == "pending" and snapshot.get("pipeline_version") == PIPELINE_VERSION, "snapshot state")
    source = snapshot.get("source")
    require(isinstance(source, dict) and isinstance(source.get("url"), str), "snapshot source")
    digest = source.get("pdf_sha256")
    require(isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), "PDF digest")
    length = source.get("byte_length")
    require(type(length) is int and 0 < length <= resume_source.MAX_PDF_BYTES, "PDF byte length")
    pages = snapshot.get("pages")
    require(isinstance(pages, list) and 1 <= len(pages) <= 10, "page count")
    require(type(snapshot.get("page_count")) is int and snapshot["page_count"] == len(pages), "page count mismatch")
    total_chars = 0
    has_ocr = False
    for index, page in enumerate(pages, 1):
        require(isinstance(page, dict) and type(page.get("page_number")) is int and page["page_number"] == index, "page number")
        page_text = page.get("text")
        require(isinstance(page_text, str), "page text")
        require(len(page_text) <= 30000, "page text limit")
        words = re.findall(r"\b\w+\b", page_text, flags=re.UNICODE)
        short_ratio = sum(len(word) == 1 for word in words) / max(1, len(words))
        require(sum(char.isalnum() for char in page_text) >= 40 and len([w for w in words if len(w) >= 2]) >= 8, "page text quality")
        require(short_ratio <= 0.35 and "\ufffd" not in page_text, "page text quality")
        total_chars += len(page_text)
        method = page.get("method")
        confidence = page.get("ocr_mean_confidence")
        require(method in {"text", "ocr"}, "page method")
        if method == "text":
            require(confidence is None, "text confidence")
        else:
            import math
            require(type(confidence) in (int, float) and math.isfinite(confidence) and 75 <= confidence <= 100, "OCR confidence")
            has_ocr = True
    require(total_chars <= 100000, "document text limit")
    extractor = snapshot.get("extractor")
    require(isinstance(extractor, dict), "extractor metadata")
    require(extractor.get("policy_version") == "2", "extractor policy")
    require(isinstance(extractor.get("pypdf_version"), str) and bool(extractor["pypdf_version"].strip()), "pypdf version")
    require(extractor.get("ocr_engine") is None or isinstance(extractor.get("ocr_engine"), str) and bool(extractor["ocr_engine"].strip()), "OCR engine")
    require(not has_ocr or isinstance(extractor.get("ocr_engine"), str), "OCR engine missing")
    require(snapshot.get("content_hash") == _canonical_hash(pages), "content digest")

def _acquire_lock(path: Path) -> int:
    try:
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PreparationError("state_locked", "Another preparation is using this state directory") from exc


def _get_manifest_and_pdf(manifest_url: str, allow_local_http: bool, downloader: Callable | None):
    fetch = downloader or resume_source.fetch_bytes
    manifest_url = resume_source.validate_url(manifest_url, allow_local_http=allow_local_http)
    manifest_data, content_type = fetch(manifest_url, limit=resume_source.MAX_MANIFEST_BYTES, allow_local_http=allow_local_http)
    if len(manifest_data) > resume_source.MAX_MANIFEST_BYTES:
        raise PreparationError("size_limit", "Manifest exceeds the size limit")
    if content_type not in ("application/json", "text/json", "application/octet-stream", ""):
        raise PreparationError("invalid_manifest", "Manifest content type is not accepted")
    parsed = resume_source.parse_manifest(manifest_data, allow_local_http=allow_local_http)
    resume_source.validate_manifest_origins(manifest_url, parsed)
    pdf_data, pdf_type = fetch(parsed["attachment_url"], limit=resume_source.MAX_PDF_BYTES, allow_local_http=allow_local_http)
    resume_source.validate_pdf(pdf_data, pdf_type)
    return parsed["attachment_url"], pdf_data


def prepare_resume(*, state_dir: str | Path, manifest_url: str | None = None, pdf_path: str | Path | None = None,
                   source_url: str | None = None, tesseract_cmd: str = "tesseract", allow_local_http: bool = False,
                   downloader: Callable | None = None, extractor: Callable | None = None) -> dict:
    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    snapshot_path = state / "last-successful.json"
    status_path = state / "status.json"
    lock_path = state / ".prepare.lock"
    last_attempt = _now()
    lock_fd = None
    try:
        lock_fd = _acquire_lock(lock_path)
        if bool(manifest_url) == bool(pdf_path):
            raise PreparationError("invalid_arguments", "Provide exactly one of manifest_url or pdf_path")
        if manifest_url:
            source, pdf = _get_manifest_and_pdf(manifest_url, allow_local_http, downloader)
        else:
            if not source_url:
                raise PreparationError("invalid_arguments", "source_url is required with pdf_path")
            source = resume_source.validate_url(source_url, allow_local_http=allow_local_http)
            try:
                with open(pdf_path, "rb") as stream:
                    pdf = stream.read(resume_source.MAX_PDF_BYTES + 1)
            except OSError as exc:
                raise PreparationError("fetch_failed", "Local PDF could not be read") from exc
            resume_source.validate_pdf(pdf)
        pdf_sha = hashlib.sha256(pdf).hexdigest()
        previous_raw = snapshot_path.read_bytes() if snapshot_path.exists() else None
        try:
            previous = _validate_snapshot(previous_raw) if previous_raw is not None else None
        except PreparationError:
            previous = None
        if previous and previous["source"]["url"] == source and previous["source"]["pdf_sha256"] == pdf_sha and previous["pipeline_version"] == PIPELINE_VERSION:
            result = "unchanged"
        else:
            if extractor is None:
                from api.knowledge.resume_pdf import extract_pdf
                extractor = extract_pdf
            try:
                extracted = extractor(pdf, tesseract_cmd=tesseract_cmd)
            except Exception as exc:
                raise PreparationError(getattr(exc, "code", "extraction_failed"), "Resume PDF extraction failed") from exc
            pages = extracted.get("pages") if isinstance(extracted, dict) else None
            if not isinstance(pages, list) or not pages:
                raise PreparationError("invalid_extraction", "Extraction returned no valid pages")
            for index, page in enumerate(pages, 1):
                if not isinstance(page, dict) or page.get("page_number") != index or not isinstance(page.get("text"), str) or page.get("method") not in {"text", "ocr"}:
                    raise PreparationError("invalid_extraction", "Extraction page data is invalid")
            page_count = extracted.get("page_count")
            if page_count != len(pages):
                raise PreparationError("invalid_extraction", "Extraction page count is invalid")
            snapshot = {"schema_version": 1, "kind": "resume-extraction", "review_status": "pending",
                        "pipeline_version": PIPELINE_VERSION, "source": {"url": source, "pdf_sha256": pdf_sha, "byte_length": len(pdf)},
                        "content_hash": _canonical_hash(pages), "pages": pages, "page_count": page_count,
                        "extractor": extracted.get("extractor")}
            try:
                _validate_snapshot_obj(snapshot)
            except Exception as exc:
                raise PreparationError("invalid_extraction", "Extraction data failed validation") from exc
            _atomic_write(snapshot_path, _json_bytes(snapshot))
            result = "changed"
        status = {"status": result, "last_attempt_at": last_attempt, "last_success_at": _now(), "last_successful_available": True}
        _atomic_write(status_path, _json_bytes(status))
        return status
    except Exception as exc:
        code = getattr(exc, "code", "preparation_failed")
        available = False
        if snapshot_path.exists():
            try:
                _validate_snapshot(snapshot_path.read_bytes())
                available = True
            except PreparationError:
                pass
        status = {"status": "failed", "code": str(code)[:64], "diagnostic": "Resume preparation failed validation or extraction.",
                  "last_attempt_at": last_attempt, "last_successful_available": available}
        try:
            if code != "state_locked":
                _atomic_write(status_path, _json_bytes(status))
        except OSError:
            pass
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError(str(code), "Resume preparation failed") from exc
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and extract a resume PDF into a reviewable snapshot.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest-url")
    group.add_argument("--pdf")
    parser.add_argument("--source-url")
    parser.add_argument("--state-dir", default=".resume-state")
    parser.add_argument("--tesseract-command", default="tesseract")
    parser.add_argument("--allow-local-http", action="store_true")
    args = parser.parse_args()
    try:
        status = prepare_resume(state_dir=args.state_dir, manifest_url=args.manifest_url, pdf_path=args.pdf,
                                source_url=args.source_url, tesseract_cmd=args.tesseract_command,
                                allow_local_http=args.allow_local_http)
        print(status["status"])
        return 0
    except PreparationError as exc:
        print(f"Resume preparation failed ({exc.code}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
