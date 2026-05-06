from typing import Any, Literal

from pydantic import BaseModel, Field


Provider = Literal["gemini", "deepseek"]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    provider: Provider | None = None
    system: str | None = None
    top_k: int | None = Field(default=None, ge=0, le=20)
    namespace: str | None = None


class Source(BaseModel):
    id: str
    score: float | None = None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    provider: Provider
    sources: list[Source] = Field(default_factory=list)


class Document(BaseModel):
    id: str | None = None
    text: str = Field(..., min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    documents: list[Document] = Field(..., min_length=1)
    namespace: str | None = None


class IngestResponse(BaseModel):
    upserted: int
    namespace: str
