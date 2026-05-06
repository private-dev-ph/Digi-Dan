import httpx
from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import Provider


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


def generate_answer(
    provider: Provider,
    prompt: str,
    system: str,
    settings: Settings,
) -> str:
    if provider == "gemini":
        return _generate_with_gemini(prompt, system, settings)
    if provider == "deepseek":
        return _generate_with_deepseek(prompt, system, settings)
    raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")


def _generate_with_gemini(prompt: str, system: str, settings: Settings) -> str:
    if not settings.gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not configured")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(system_instruction=system),
    )
    return response.text or ""


def _generate_with_deepseek(prompt: str, system: str, settings: Settings) -> str:
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not configured")

    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

    try:
        response = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=45,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"DeepSeek request failed: {exc}") from exc

    data = response.json()
    return data["choices"][0]["message"]["content"] or ""
