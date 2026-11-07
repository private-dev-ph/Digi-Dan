"""Gemini embedding adapter for versioned knowledge."""

import math
import re
import time
from typing import Optional

from .ports import VersioningError

MODEL = "gemini-embedding-001"
DIMENSIONS = (768, 1536, 3072)
POLICY_VERSION = "normalized-v1"


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
                 max_attempts: int = 1, sleep=time.sleep):
        if type(timeout_ms) is not int or timeout_ms <= 0:
            raise VersioningError("invalid_embedding_timeout")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise VersioningError("invalid_embedding_attempts")
        if not callable(sleep):
            raise VersioningError("invalid_embedding_sleep")
        self._client = client
        self._owns_client = False
        self._api_key = api_key
        self._timeout_ms = timeout_ms
        self._max_attempts = max_attempts
        self._sleep = sleep

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
        response = None
        for attempt in range(self._max_attempts):
            try:
                response = client.models.embed_content(
                    model=MODEL, contents=texts, config=config,
                )
                break
            except Exception as exc:
                if type(getattr(exc, "code", None)) is not int or exc.code != 429:
                    raise VersioningError("embedding_provider_error") from None
                if attempt + 1 >= self._max_attempts:
                    raise VersioningError("embedding_rate_limited") from None
                delay, excessive = _retry_delay(exc)
                if excessive:
                    raise VersioningError("embedding_rate_limited") from None
                self._sleep(60.0 if delay is None else delay)

        embeddings = getattr(response, "embeddings", None)
        if not isinstance(embeddings, (list, tuple)) or len(embeddings) != len(texts):
            raise VersioningError("invalid_embedding_response")
        result = []
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


def _retry_delay(error) -> tuple[Optional[float], bool]:
    """Return a bounded retry delay and whether a supplied hint exceeds the cap."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    retry_after = None
    if headers is not None:
        try:
            retry_after = headers.get("Retry-After")
        except Exception:
            retry_after = None
    if retry_after is not None:
        parsed, excessive = _parse_delay(retry_after)
        if parsed is not None or excessive:
            return parsed, excessive

    details = getattr(error, "details", None)
    if isinstance(details, dict):
        nested_error = details.get("error")
        if isinstance(nested_error, dict):
            details = nested_error.get("details")
        else:
            details = details.get("details")
    if isinstance(details, (list, tuple)):
        for item in details:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type", "")
            if isinstance(kind, str) and kind.endswith("google.rpc.RetryInfo"):
                parsed, excessive = _parse_delay(item.get("retryDelay"))
                if parsed is not None or excessive:
                    return parsed, excessive
    return None, False


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
