import asyncio
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import Provider


class ProviderFailure(Exception):
    _CATEGORIES = {"timeout", "connection", "rate_limit", "server", "malformed", "configuration", "rejected", "refusal", "unexpected"}
    _RETRYABLE = {"timeout", "connection", "rate_limit", "server", "malformed"}

    def __init__(self, category: str):
        if category not in self._CATEGORIES:
            category = "unexpected"
        self.category = category
        super().__init__(category)

    @property
    def retryable(self) -> bool:
        return self.category in self._RETRYABLE


def embed_texts(texts: list[str], settings: Settings) -> list[list[float]]:
    if not settings.gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=texts,
        config=types.EmbedContentConfig(output_dimensionality=settings.embedding_dimensions),
    )
    return [embedding.values for embedding in response.embeddings]


def generate_answer(provider: Provider, prompt: str, system: str, settings: Settings) -> str:
    if provider == "gemini":
        key = settings.gemini_api_key
        model = settings.gemini_model
        if not isinstance(key, str) or not key.strip() or not isinstance(model, str) or not model.strip():
            raise ProviderFailure("configuration")
        model = model.strip()
        if model.startswith("models/"):
            model = model[len("models/"):]
        if not model.strip():
            raise ProviderFailure("configuration")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model, safe='')}:generateContent"
        headers = {"x-goog-api-key": key.strip()}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        return _post_and_parse(url, headers, payload, settings, "gemini")
    if provider == "deepseek":
        key = settings.deepseek_api_key
        model = settings.deepseek_model
        base_url = settings.deepseek_base_url
        if not isinstance(key, str) or not key.strip() or not isinstance(model, str) or not model.strip() or not isinstance(base_url, str) or not base_url.strip():
            raise ProviderFailure("configuration")
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {key.strip()}"}
        payload = {
            "model": model.strip(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
        }
        return _post_and_parse(url, headers, payload, settings, "deepseek")
    raise ProviderFailure("configuration")


def _post_and_parse(url: str, headers: dict[str, str], payload: dict, settings: Settings, provider: str) -> str:
    try:
        timeout_seconds = float(getattr(settings, "generation_timeout_seconds", 20))
        if timeout_seconds <= 0:
            raise ProviderFailure("configuration")
        async def request_once():
            timeout = httpx.Timeout(timeout_seconds)
            transport = httpx.AsyncHTTPTransport(retries=0)
            async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False) as client:
                response = await client.post(url, headers=headers, json=payload)
                body = await response.aread()
                return response.status_code, body
        status, body = asyncio.run(asyncio.wait_for(request_once(), timeout=timeout_seconds))
    except ProviderFailure:
        raise
    except (asyncio.TimeoutError, httpx.TimeoutException):
        raise ProviderFailure("timeout") from None
    except httpx.RequestError:
        raise ProviderFailure("connection") from None
    except Exception:
        raise ProviderFailure("unexpected") from None

    if status < 200 or status >= 300:
        if status == 408:
            category = "timeout"
        elif status == 429:
            category = "rate_limit"
        elif status >= 500:
            category = "server"
        elif status in (401, 403, 404):
            category = "configuration"
        else:
            category = "rejected"
        raise ProviderFailure(category)
    import json
    try:
        data = json.loads(body)
    except Exception:
        raise ProviderFailure("malformed") from None
    if not isinstance(data, dict):
        raise ProviderFailure("malformed")
    try:
        answer = _parse_gemini(data) if provider == "gemini" else _parse_deepseek(data)
    except ProviderFailure:
        raise
    except Exception:
        raise ProviderFailure("unexpected") from None
    if not isinstance(answer, str) or not answer.strip():
        raise ProviderFailure("malformed")
    return answer.strip()


def _parse_gemini(data: dict) -> str:
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict):
        reason = feedback.get("blockReason")
        if reason and reason not in ("BLOCK_REASON_UNSPECIFIED", "BLOCKED_REASON_UNSPECIFIED"):
            raise ProviderFailure("refusal")
        feedback_ratings = feedback.get("safetyRatings")
        if isinstance(feedback_ratings, list) and any(isinstance(rating, dict) and rating.get("blocked") is True for rating in feedback_ratings):
            raise ProviderFailure("refusal")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise ProviderFailure("malformed")
    candidate = candidates[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ProviderFailure("malformed")
    if finish_reason in {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}:
        raise ProviderFailure("refusal")
    ratings = candidate.get("safetyRatings")
    if isinstance(ratings, list) and any(isinstance(rating, dict) and rating.get("blocked") is True for rating in ratings):
        raise ProviderFailure("refusal")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ProviderFailure("malformed")
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ratings = part.get("safetyRatings")
        if isinstance(ratings, list) and any(isinstance(rating, dict) and rating.get("blocked") is True for rating in ratings):
            raise ProviderFailure("refusal")
        if part.get("thought") is True:
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
    return "".join(texts)


def _parse_deepseek(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderFailure("malformed")
    choice = choices[0]
    if choice.get("finish_reason") == "content_filter":
        raise ProviderFailure("refusal")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderFailure("malformed")
    refusal = message.get("refusal")
    if isinstance(refusal, str) and refusal.strip():
        raise ProviderFailure("refusal")
    content = message.get("content")
    return content if isinstance(content, str) else ""
