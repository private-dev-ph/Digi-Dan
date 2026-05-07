from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import ChatRequest, ChatResponse, IngestRequest, IngestResponse, Provider, Source
from api.core.prompts import DEFAULT_SYSTEM_PROMPT
from api.query.providers import generate_answer
from api.tools.vector_store import query_sources, upsert_documents


def answer_question(request: ChatRequest, settings: Settings) -> ChatResponse:
    provider = _provider(request.provider, settings)
    namespace = request.namespace or settings.upstash_namespace
    top_k = settings.retrieval_top_k if request.top_k is None else request.top_k

    sources = query_sources(request.message, top_k, namespace, settings)
    context = _format_context(sources, settings.max_context_chars)
    prompt = _format_prompt(request.message, context)
    answer = generate_answer(provider, prompt, request.system or DEFAULT_SYSTEM_PROMPT, settings)

    return ChatResponse(answer=answer, provider=provider, sources=sources)


def ingest_documents(request: IngestRequest, settings: Settings) -> IngestResponse:
    namespace = request.namespace or settings.upstash_namespace
    count = upsert_documents(request.documents, namespace, settings)
    return IngestResponse(upserted=count, namespace=namespace)


def _provider(value: Provider | None, settings: Settings) -> Provider:
    provider = value or settings.default_provider
    if provider not in ("gemini", "deepseek"):
        raise HTTPException(status_code=400, detail="provider must be 'gemini' or 'deepseek'")
    return provider


def _format_context(sources: list[Source], limit: int) -> str:
    parts = []
    used = 0
    for source in sources:
        chunk = f"[{source.id}]\n{source.text.strip()}\n"
        if used + len(chunk) > limit:
            chunk = chunk[: max(0, limit - used)]
        if chunk:
            parts.append(chunk)
            used += len(chunk)
        if used >= limit:
            break
    return "\n".join(parts)


def _format_prompt(message: str, context: str) -> str:
    if not context:
        return message

    return (
        "Retrieved context:\n"
        f"{context}\n\n"
        "User question:\n"
        f"{message}\n\n"
        "Answer naturally without mentioning source IDs or metadata."
    )
