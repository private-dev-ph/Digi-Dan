"""Read-only verification of knowledge sources currently deployed at a site."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from api.knowledge import manifest, resume_source
from api.knowledge.ports import VersioningError

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$", re.I)


def _fail(code: str):
    raise VersioningError(code) from None


def _trusted_origin(url: str) -> tuple[str, str, int]:
    try:
        resume_source.validate_url(url)
        p = urlsplit(url)
        port = p.port
        host = p.hostname
        if (not isinstance(url, str) or p.scheme.lower() != "https" or not host or
                p.username is not None or p.password is not None or p.query or p.fragment or
                p.path not in ("", "/") or port not in (None, 443)):
            _fail("invalid_site_url")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _fail("invalid_site_url")
        if host.lower() == "localhost" or "." not in host:
            _fail("invalid_site_url")
        return "https", host.lower(), 443
    except (TypeError, ValueError):
        _fail("invalid_site_url")


def _safe_url(url: str) -> bool:
    try:
        resume_source.validate_url(url)
        p = urlsplit(url)
        if p.scheme != "https" or p.port not in (None, 443) or not p.hostname:
            return False
        try:
            return ipaddress.ip_address(p.hostname).is_global
        except ValueError:
            return p.hostname.lower() != "localhost"
    except (ValueError, TypeError):
        return False


class DeployedSourceVerifier:
    def __init__(self, site_url: str, *, manifest_url: str | None = None, fetch=None):
        self.site_url = site_url
        self.site_origin = _trusted_origin(site_url)
        self.manifest_url = manifest_url or site_url.rstrip("/") + "/api/knowledge"
        try:
            p = urlsplit(self.manifest_url)
            if (p.scheme != "https" or p.username is not None or p.password is not None or p.query or p.fragment or
                    p.port not in (None, 443) or not p.path or resume_source.origin(self.manifest_url) != self.site_origin):
                _fail("invalid_manifest_url")
        except (ValueError, TypeError):
            _fail("invalid_manifest_url")
        self.fetch = fetch or resume_source.fetch_bytes

    def _manifest(self):
        try:
            body, ctype = self.fetch(self.manifest_url, limit=manifest.MAX_MANIFEST_BYTES)
            if ctype != "application/json":
                _fail("invalid_manifest_content_type")
            parsed = manifest.validate_portfolio_manifest(body, site_url=self.site_url)
            source = parsed["source"]
            if not isinstance(source.get("deployment_id"), str) or not source["deployment_id"].strip() or not isinstance(source.get("commit_sha"), str) or not _COMMIT.fullmatch(source["commit_sha"]):
                _fail("missing_deployment_identity")
            return parsed
        except VersioningError:
            raise
        except Exception:
            _fail("manifest_fetch_failed")

    @staticmethod
    def _identity(parsed):
        return parsed["source"], parsed["content_hash"], parsed["attachments"][0]["url"]

    def inspect(self) -> dict:
        first = self._manifest()
        first_id = self._identity(first)
        attachment_url = first_id[2]
        if resume_source.origin(attachment_url) != self.site_origin:
            _fail("invalid_attachment_origin")
        try:
            pdf, ctype = self.fetch(attachment_url, limit=resume_source.MAX_PDF_BYTES)
            resume_source.validate_pdf(pdf, ctype)
        except VersioningError:
            raise
        except Exception:
            _fail("resume_fetch_failed")
        second = self._manifest()
        if self._identity(second) != first_id:
            _fail("source_changed")
        return {"portfolio_source": first["source"], "portfolio_content_hash": first["content_hash"],
                "resume_source": {"url": attachment_url, "pdf_sha256": hashlib.sha256(pdf).hexdigest()},
                "document_count": len(first["documents"])}

    def verify(self, record: dict) -> dict:
        candidate_hash = record.get("candidate_hash") if isinstance(record, dict) else None
        if not isinstance(candidate_hash, str) or not _HASH.fullmatch(candidate_hash):
            _fail("invalid_record")
        source = record.get("source")
        if not isinstance(source, dict) or source.get("assembly_input") != "fetched-manifest":
            _fail("source_stale")
        provenance = source
        ps = provenance.get("portfolio_source")
        rs = provenance.get("resume_source")
        if not isinstance(ps, dict) or not _safe_url(ps.get("site_url")) or not isinstance(rs, dict) or not _safe_url(rs.get("url")):
            _fail("source_stale")
        if _trusted_origin(ps["site_url"]) != self.site_origin or resume_source.origin(rs["url"]) != self.site_origin:
            _fail("source_stale")
        receipt = self.inspect()
        if (ps != receipt["portfolio_source"] or
                provenance.get("portfolio_content_hash") != receipt["portfolio_content_hash"] or
                rs.get("url") != receipt["resume_source"]["url"] or
                rs.get("pdf_sha256") != receipt["resume_source"]["pdf_sha256"]):
            _fail("source_stale")
        return receipt

    def __call__(self, record: dict) -> bool:
        self.verify(record)
        return True
