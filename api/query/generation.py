"""Bounded, privacy-safe provider fallback for answer generation."""

import logging
import time
from typing import Callable

from fastapi import HTTPException

from api.core.models import Provider
from api.query.providers import ProviderFailure

logger = logging.getLogger(__name__)

_RETRYABLE_CATEGORIES = {"timeout", "connection", "rate_limit", "server", "malformed"}
_KNOWN_CATEGORIES = _RETRYABLE_CATEGORIES | {"refusal", "configuration", "rejected", "unexpected"}
_TRANSIENT_DETAIL = "Digi-Dan is temporarily unable to answer. Please try again shortly."


def _http_failure(category: str) -> HTTPException:
    if category == "refusal":
        return HTTPException(status_code=422, detail="Digi-Dan cannot answer that request.")
    if category == "configuration":
        return HTTPException(status_code=503, detail="Digi-Dan has a provider configuration problem.")
    if category in _RETRYABLE_CATEGORIES:
        return HTTPException(status_code=503, detail=_TRANSIENT_DETAIL)
    return HTTPException(status_code=502, detail="Digi-Dan could not complete this request.")


def _log_failure(provider: Provider, category: str, attempt: int) -> None:
    logger.warning(
        "Generation provider attempt failed provider=%s category=%s attempt=%d",
        provider,
        category,
        attempt,
        extra={"provider": provider, "category": category, "attempt": attempt},
    )


def generate_with_fallback(
    primary: Provider,
    explicit: bool,
    prompt: str,
    system: str,
    settings,
    *,
    generate: Callable,
) -> tuple[str, Provider]:
    """Generate once, optionally using the other provider after a transient failure."""
    total_seconds = max(0.0, float(settings.generation_total_timeout_seconds))
    per_attempt_seconds = max(0.0, float(settings.generation_timeout_seconds))
    deadline = time.monotonic() + total_seconds
    providers: list[Provider] = [primary]
    if not explicit and settings.generation_fallback_enabled:
        providers.append("deepseek" if primary == "gemini" else "gemini")

    last_category = "timeout"
    for attempt, provider in enumerate(providers, start=1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_category = "timeout"
            _log_failure(provider, last_category, attempt)
            break
        attempt_settings = settings.model_copy(update={
            "generation_timeout_seconds": min(per_attempt_seconds, remaining),
        })
        try:
            answer = generate(provider, prompt, system, attempt_settings)
            if time.monotonic() >= deadline:
                raise ProviderFailure("timeout")
            if not isinstance(answer, str) or not answer.strip():
                raise ProviderFailure("malformed")
            return answer, provider
        except ProviderFailure as exc:
            raw_category = getattr(exc, "category", None)
            category = raw_category if isinstance(raw_category, str) and raw_category in _KNOWN_CATEGORIES else "unexpected"
            _log_failure(provider, category, attempt)
            last_category = category
            if category not in _RETRYABLE_CATEGORIES or not getattr(exc, "retryable", False):
                raise _http_failure(category) from None
            if attempt == len(providers):
                raise _http_failure(category) from None
        except Exception:
            _log_failure(provider, "unexpected", attempt)
            raise HTTPException(
                status_code=502,
                detail="Digi-Dan could not complete this request.",
            ) from None
    raise _http_failure(last_category)
