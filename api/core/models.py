from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Provider = Literal["gemini", "deepseek"]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    provider: Provider | None = None
    system: str | None = None
    top_k: int | None = Field(default=None, ge=0, le=20)
    namespace: str | None = None
    history: list["ConversationMessage"] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 2000:
            raise ValueError("message must be nonblank and at most 2000 characters")
        return value

    @model_validator(mode="after")
    def validate_history(self):
        if len(self.history) > 8:
            raise ValueError("history may contain at most 8 messages")
        total = sum(len(item.content) for item in self.history)
        if total > 8000:
            raise ValueError("history may contain at most 8000 characters total")
        if self.history:
            if len(self.history) % 2 or [item.role for item in self.history] != ["user", "assistant"] * (len(self.history) // 2):
                raise ValueError("history must contain complete user/assistant pairs")
        return self


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must be nonblank")
        return value


class Source(BaseModel):
    id: str
    score: float | None = None
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    answer: str
    provider: Provider
    sources: list[Source] = Field(default_factory=list)
    knowledge_version: str | None = None


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
