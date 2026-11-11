"""Gemini embedding adapter for versioned knowledge."""

import math
import re
import time
import threading
from collections import deque
from typing import Optional

from .ports import VersioningError

MODEL = "gemini-embedding-001"
DIMENSIONS = (768, 1536, 3072)
POLICY_VERSION = "normalized-v1"
_BACKGROUND_WINDOW = 61.0
_BACKGROUND_TEXTS = 64
_BACKGROUND_BYTES = 24000


def gemini_profile(dimensions: int = 768, model: str = MODEL) -> dict:
    """Return the canonical profile recorded with vectors from this adapter."""
    if model != MODEL:
        raise VersioningError("unsupported_embedding_model")
    if type(dimensions) is not int or dimensions not in DIMENSIONS:
        raise VersioningError("unsupported_embedding_dimensions")
    return {"provider": "gemini", "model": MODEL, "dimensions": dimensions,
            "policy_version": POLICY_VERSION}


class GeminiEmbedder:
    def __init__(self, api_key: Optional[str] = None, *, client=None, timeout_ms: int = 15000,
                 max_attempts: int = 1, sleep=time.sleep, background: bool = False,
                 monotonic=time.monotonic):
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise VersioningError("invalid_embedding_timeout")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise VersioningError("invalid_embedding_attempts")
        if not callable(sleep):
            raise VersioningError("invalid_embedding_sleep")
        if type(background) is not bool or not callable(monotonic):
            raise VersioningError("invalid_embedding_pacing")
        self._client = client
        self._owns_client = False
        self._api_key = api_key
        self._timeout_ms = timeout_ms
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._background = background
        self._clock = monotonic
        self._pace_lock = threading.Lock()
        self._attempts = deque()

    def embed(self, texts: list[str], profile: dict) -> list[list[float]]:
        if (not isinstance(texts, list) or not texts or len(texts) > 32 or
                any(not isinstance(text, str) or not text.strip() for text in texts)):
            raise VersioningError("invalid_embedding_input")
        try:
            expected = gemini_profile(profile.get("dimensions"), profile.get("model"))
        except (AttributeError, VersioningError):
            raise VersioningError("invalid_embedding_profile") from None
        if profile != expected:
            raise VersioningError("invalid_embedding_profile")

        batches = _byte_batches(texts) if self._background else [texts]

        client = self._client
        if client is None:
            if not self._api_key:
                raise VersioningError("embedding_configuration_error")
            try:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=self._api_key,
                                      http_options=types.HttpOptions(timeout=self._timeout_ms))
                self._client = client
                self._owns_client = True
            except Exception:
                raise VersioningError("embedding_configuration_error") from None
        else:
            from google.genai import types

        config = types.EmbedContentConfig(output_dimensionality=profile["dimensions"])
        result = []
        for batch in batches:
            response = None
            for attempt in range(self._max_attempts):
                if self._background:
                    self._pace(batch)
                try:
                    response = client.models.embed_content(
                        model=MODEL, contents=batch, config=config,
                    )
                    break
                except Exception as exc:
                    if type(getattr(exc, "code", None)) is not int or exc.code != 429:
                        raise VersioningError("embedding_provider_error") from None
                    quota_code = _quota_error_code(exc)
                    if quota_code != "embedding_rate_limited":
                        raise VersioningError(quota_code) from None
                    if attempt + 1 >= self._max_attempts:
                        raise VersioningError("embedding_rate_limited") from None
                    delay, excessive = _retry_delay(exc)
                    if excessive:
                        raise VersioningError("embedding_rate_limited") from None
                    self._sleep(61.0 if delay is None else delay + 1.0)

            embeddings = getattr(response, "embeddings", None)
            if not isinstance(embeddings, (list, tuple)) or len(embeddings) != len(batch):
                raise VersioningError("invalid_embedding_response")
            for embedding in embeddings:
                values = getattr(embedding, "values", None)
                if not isinstance(values, (list, tuple)) or len(values) != profile["dimensions"]:
                    raise VersioningError("invalid_embedding_response")
                if any(type(value) not in (int, float) for value in values):
                    raise VersioningError("invalid_embedding_response")
                try:
                    if any(not math.isfinite(value) for value in values):
                        raise VersioningError("invalid_embedding_response")
                    norm = math.hypot(*values)
                except (OverflowError, TypeError, ValueError):
                    raise VersioningError("invalid_embedding_response") from None
                if not math.isfinite(norm) or norm == 0:
                    raise VersioningError("invalid_embedding_response")
                result.append([value / norm for value in values])
        return result

    def _pace(self, batch):
        sizes = [len(text.encode("utf-8")) for text in batch]
        with self._pace_lock:
            while True:
                now = self._clock()
                while self._attempts and now - self._attempts[0][0] >= _BACKGROUND_WINDOW:
                    self._attempts.popleft()
                if (len(self._attempts) + len(sizes) <= _BACKGROUND_TEXTS and
                        sum(item[1] for item in self._attempts) + sum(sizes) <= _BACKGROUND_BYTES):
                    self._attempts.extend((now, size) for size in sizes)
                    return
                self._sleep(max(0.0, _BACKGROUND_WINDOW - (now - self._attempts[0][0])))


def _byte_batches(texts):
    batches, batch, size = [], [], 0
    for text in texts:
        try:
            length = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise VersioningError("invalid_embedding_input") from None
        if length > _BACKGROUND_BYTES:
            raise VersioningError("invalid_embedding_input")
        if batch and size + length > _BACKGROUND_BYTES:
            batches.append(batch)
            batch, size = [], 0
        batch.append(text)
        size += length
    if batch:
        batches.append(batch)
    return batches


def _error_details(error):
    """Handle both the installed SDK's nested body and direct RPC details."""
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        nested = details.get("error")
        details = nested.get("details") if isinstance(nested, dict) else details.get("details")
    return details if isinstance(details, (list, tuple)) else []


def _quota_error_code(error):
    daily = False
    for item in _error_details(error):
        if not isinstance(item, dict) or item.get("@type") != "type.googleapis.com/google.rpc.QuotaFailure":
            continue
        violations = item.get("violations")
        if not isinstance(violations, (list, tuple)):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            value = violation.get("quotaValue")
            if (type(value) in (int, float) and value == 0) or value == "0":
                return "embedding_quota_unavailable"
            for field in ("quotaId", "quotaMetric"):
                identifier = violation.get(field)
                if isinstance(identifier, str):
                    daily |= "perday" in re.sub(r"[^a-z]", "", identifier.lower())
    return "embedding_daily_quota_exceeded" if daily else "embedding_rate_limited"


def _retry_delay(error) -> tuple[Optional[float], bool]:
    """Honor the longest usable hint; never shorten an excessive server wait."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    hints = []
    if headers is not None:
        try:
            value = headers.get("Retry-After")
            hints.append(value if value is not None else headers.get("retry-after"))
        except Exception:
            pass
    for item in _error_details(error):
        if isinstance(item, dict) and item.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
            hints.append(item.get("retryDelay"))
    delays = []
    for hint in hints:
        parsed, excessive = _parse_delay(hint)
        if excessive:
            return None, True
        if parsed is not None:
            delays.append(parsed)
    return (max(delays) if delays else None), False


def _parse_delay(value) -> tuple[Optional[float], bool]:
    if isinstance(value, (int, float)) and type(value) is not bool:
        try:
            seconds = float(value)
        except (OverflowError, TypeError, ValueError):
            return None, False
    elif isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s)?\s*", value)
        if not match:
            return None, False
        try:
            seconds = float(match.group(1))
        except (OverflowError, ValueError):
            return None, False
    else:
        return None, False
    if not math.isfinite(seconds) or seconds < 0:
        return None, False
    if seconds > 60:
        return None, True
    return seconds, False
