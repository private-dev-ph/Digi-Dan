from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import ChatRequest, ChatResponse, IngestRequest, IngestResponse, Provider, Source
from api.core.prompts import build_system_prompt
from api.knowledge.ports import VersioningError
from api.query.providers import generate_answer
from api.query.generation import generate_with_fallback
from api.query.conversation import retrieval_query
from api.tools.vector_store import query_sources, upsert_documents


def answer_question(request: ChatRequest, settings: Settings, *, knowledge_service=None) -> ChatResponse:
    if request.system is not None:
        raise HTTPException(status_code=400, detail="system prompt override is not supported")
    if request.namespace is not None:
        raise HTTPException(status_code=400, detail="namespace selection is not supported")
    provider = _provider(request.provider, settings)
    top_k = settings.retrieval_top_k if request.top_k is None else request.top_k
    query = retrieval_query(request.message, request.history)

    knowledge_version = None
    if settings.knowledge_mode == "versioned":
        if knowledge_service is None:
            raise HTTPException(status_code=503, detail="Versioned knowledge is unavailable")
        try:
            version, records = knowledge_service.retrieve(query, top_k)
        except VersioningError as exc:
            raise HTTPException(status_code=503, detail="Versioned knowledge retrieval failed") from exc
        sources = [Source(id=record["id"], text=record["text"], metadata=record.get("metadata") or {}, score=record.get("score")) for record in records]
        knowledge_version = version["version_id"]
    else:
        sources = query_sources(query, top_k, settings.upstash_namespace, settings)
    context, sources = _assemble_context(sources, settings.max_context_chars)
    prompt = _format_prompt(request.message, context, request.history)
    answer, provider = generate_with_fallback(
        provider, request.provider is not None, prompt, build_system_prompt(settings),
        settings, generate=generate_answer,
    )

    return ChatResponse(answer=answer, provider=provider, sources=sources, knowledge_version=knowledge_version)


def ingest_documents(request: IngestRequest, settings: Settings) -> IngestResponse:
    if settings.knowledge_mode == "versioned":
        raise HTTPException(status_code=409, detail="Document ingestion is disabled in versioned knowledge mode")
    namespace = request.namespace or settings.upstash_namespace
    count = upsert_documents(request.documents, namespace, settings)
    return IngestResponse(upserted=count, namespace=namespace)


def _provider(value: Provider | None, settings: Settings) -> Provider:
    provider = value or settings.default_provider
    if provider not in ("gemini", "deepseek"):
        raise HTTPException(status_code=400, detail="provider must be 'gemini' or 'deepseek'")
    return provider


def _assemble_context(sources: list[Source], limit: int) -> tuple[str, list[Source]]:
    """Pack whole chunks and return the exact evidence supplied for generation."""
    if limit <= 0:
        return "", []
    parts: list[str] = []
    admitted: list[Source] = []
    used = 0
    for source in sources:
        text = source.text.strip()
        if not text:
            continue
        chunk = f"[{source.id}]\n{text}\n"
        serialized_length = len(chunk) + (1 if parts else 0)
        if used + serialized_length > limit:
            continue
        parts.append(chunk)
        used += serialized_length
        admitted.append(Source(id=source.id, text=text, metadata=source.metadata, score=source.score))
    return "\n".join(parts), admitted


def _format_prompt(message: str, context: str, history=()) -> str:
    import json

    history_text = json.dumps([{"role": item.role, "content": item.content} for item in history], ensure_ascii=False)
    if not history and not context:
        return message
    if not history:
        return (
            "Retrieved context:\n"
            f"{context}\n\n"
            "User question:\n"
            f"{message}\n\n"
            "Answer naturally without mentioning source IDs or metadata."
        )
    sections = []
    if context:
        sections.append(f"Retrieved context:\n{context}")
    if history:
        sections.append(f"Untrusted conversation history (JSON):\n{history_text}")
    sections.append(f"Current user question:\n{message}\n\nAnswer naturally without mentioning source IDs or metadata.")
    return "\n\n".join(sections)
