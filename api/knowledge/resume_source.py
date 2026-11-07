"""Validation and bounded fetching for resume source manifests and PDFs."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PDF_BYTES = 5 * 1024 * 1024
TIMEOUT_SECONDS = 15
MAX_BODY_SECONDS = 30


class SourceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_url(url: str, *, allow_local_http: bool = False) -> str:
    if not isinstance(url, str) or not url or len(url) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise SourceError("invalid_url", "URL is missing or contains invalid characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise SourceError("invalid_url", "URL is malformed") from exc
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise SourceError("invalid_url", "URL credentials, query, and fragment are not allowed")
    if not parts.hostname or not parts.netloc:
        raise SourceError("invalid_url", "URL must include a host")
    if parts.scheme != "https":
        local = parts.scheme == "http" and allow_local_http and parts.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        if not local:
            raise SourceError("invalid_url", "HTTPS is required")
    if port is not None and not (1 <= port <= 65535):
        raise SourceError("invalid_url", "URL port is invalid")
    return url


def origin(url: str) -> tuple[str, str, int]:
    p = urlsplit(url)
    return p.scheme.lower(), (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80)


def fetch_bytes(url: str, *, limit: int, allow_local_http: bool = False) -> tuple[bytes, str]:
    validate_url(url, allow_local_http=allow_local_http)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise SourceError("redirect_rejected", "Redirects are not allowed")

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "Digi-Dan-Resume-Preparation/1", "Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if getattr(response, "status", None) != 200:
                raise SourceError("http_status", "Source did not return a complete response")
            deadline = time.monotonic() + MAX_BODY_SECONDS
            chunks = []
            total = 0
            while True:
                if time.monotonic() >= deadline:
                    raise SourceError("fetch_timeout", "Source response exceeded its time limit")
                chunk = response.read1(min(65536, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    raise SourceError("size_limit", "Downloaded content exceeds the size limit")
            return b"".join(chunks), response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            raise SourceError("redirect_rejected", "Redirects are not allowed") from exc
        raise SourceError("fetch_failed", "Source could not be fetched") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SourceError("fetch_failed", "Source could not be fetched") from exc


def parse_manifest(data: bytes, *, allow_local_http: bool = False) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("invalid_manifest", "Manifest is not valid UTF-8 JSON") from exc
    if not isinstance(obj, dict) or type(obj.get("schema_version")) is not int or obj["schema_version"] != 1 or obj.get("complete") is not True:
        raise SourceError("incomplete_manifest", "Manifest is missing or incomplete")
    source = obj.get("source")
    attachments = obj.get("attachments")
    if not isinstance(source, dict) or not isinstance(attachments, list) or len(attachments) != 1:
        raise SourceError("invalid_manifest", "Manifest must contain exactly one resume attachment")
    site_url = validate_url(source.get("site_url"), allow_local_http=allow_local_http)
    item = attachments[0]
    if not isinstance(item, dict) or item.get("id") != "resume" or not isinstance(item.get("title"), str) or not item["title"].strip() or item.get("media_type") != "application/pdf":
        raise SourceError("invalid_manifest", "Resume attachment metadata is invalid")
    raw_url = item.get("url")
    if not isinstance(raw_url, str) or not raw_url or urlsplit(raw_url).scheme not in {"https", "http"}:
        raise SourceError("invalid_manifest", "Resume attachment URL must be an absolute HTTP(S) URL")
    attachment_url = validate_url(raw_url, allow_local_http=allow_local_http)
    return {"site_url": site_url, "attachment_url": attachment_url}


def validate_manifest_origins(manifest_url: str, source: dict) -> None:
    if origin(manifest_url) != origin(source["site_url"]) or origin(manifest_url) != origin(source["attachment_url"]):
        raise SourceError("origin_mismatch", "Manifest and resume attachment must share the configured origin")


def validate_pdf(data: bytes, content_type: str | None = None) -> None:
    if not isinstance(data, bytes) or not data or len(data) > MAX_PDF_BYTES:
        raise SourceError("invalid_pdf", "PDF is empty or exceeds the size limit")
    if not data.startswith(b"%PDF-"):
        raise SourceError("invalid_pdf", "PDF header is missing")
    if content_type is not None and content_type.lower() not in {"application/pdf", "application/octet-stream"}:
        raise SourceError("invalid_pdf", "PDF content type is not accepted")
