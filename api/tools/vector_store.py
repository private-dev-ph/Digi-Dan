from fastapi import HTTPException

from api.core.config import Settings
from api.core.models import Document, Source
from api.query.providers import embed_texts


def _index(settings: Settings):
    if not settings.upstash_vector_rest_url or not settings.upstash_vector_rest_token:
        raise HTTPException(
            status_code=500,
            detail="UPSTASH_VECTOR_REST_URL or UPSTASH_VECTOR_REST_TOKEN is not configured",
        )
    from upstash_vector import Index

    return Index(url=settings.upstash_vector_rest_url, token=settings.upstash_vector_rest_token)


def query_sources(
    question: str,
    top_k: int,
    namespace: str,
    settings: Settings,
) -> list[Source]:
    if top_k == 0:
        return []

    [vector] = embed_texts([question], settings)
    results = _index(settings).query(
        vector=vector,
        top_k=top_k,
        namespace=namespace,
        include_metadata=True,
        include_data=True,
    )

    sources: list[Source] = []
    for result in results:
        text = result.data if isinstance(result.data, str) else str(result.data or "")
        sources.append(
            Source(
                id=result.id,
                score=result.score,
                text=text,
                metadata=result.metadata or {},
            )
        )
    return sources


def upsert_documents(documents: list[Document], namespace: str, settings: Settings) -> int:
    vectors = embed_texts([document.text for document in documents], settings)
    payload = []

    for document, vector in zip(documents, vectors, strict=True):
        document_id = document.id or _stable_id(document.text)
        payload.append(
            {
                "id": document_id,
                "vector": vector,
                "metadata": document.metadata,
                "data": document.text,
            }
        )

    _index(settings).upsert(vectors=payload, namespace=namespace)
    return len(payload)


def _stable_id(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
