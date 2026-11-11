import os
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator


class Settings(BaseModel):
    knowledge_mode: Literal["legacy", "versioned"] = "legacy"
    knowledge_environment: Literal["test", "production"] = "test"
    upstash_redis_rest_url: str | None = None
    upstash_redis_rest_token: str | None = None
    app_name: str = "Digi-Dan"
    portfolio_owner_name: str = Field(default="Daniel Zachary M. Mercurio", max_length=120)
    cors_origins: list[str] = Field(default_factory=list)
    allowed_origin_domains: list[str] = Field(default_factory=lambda: ["zachcodes.vercel.app"])
    api_bypass_secret: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    upstash_vector_rest_url: str | None = None
    upstash_vector_rest_token: str | None = None
    upstash_namespace: str = "default"

    bot_admin_token: str | None = None
    default_provider: str = "gemini"
    generation_fallback_enabled: bool = True
    generation_timeout_seconds: float = Field(default=20.0, gt=0, le=120, allow_inf_nan=False)
    generation_total_timeout_seconds: float = Field(default=40.0, gt=0, le=240, allow_inf_nan=False)
    retrieval_top_k: int = 4
    max_context_chars: int = 6000
    chat_rate_limit: int = Field(default=10, ge=0, le=1000000)
    chat_rate_limit_window_seconds: int = Field(default=3600, ge=1, le=604800)
    chat_rate_limit_backend: Literal["memory", "redis"] = "memory"
    chat_client_identity_mode: Literal["legacy", "trusted_proxy", "local"] = "legacy"
    chat_local_client_id: str = Field(default="a" * 64, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    rate_limit_redis_rest_url: str | None = None
    rate_limit_redis_rest_token: str | None = None
    rate_limit_redis_timeout_seconds: float = Field(default=2.0, gt=0, le=10, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_chat_enforcement(self):
        if self.chat_client_identity_mode == "local" and self.knowledge_environment == "production":
            raise ValueError("Local chat identity is not allowed in production")
        if self.chat_rate_limit_backend == "redis":
            if self.chat_client_identity_mode != "trusted_proxy" or self.chat_rate_limit <= 0:
                raise ValueError("Redis chat limits require trusted proxy identity and a positive allowance")
        return self

    @field_validator("portfolio_owner_name")
    @classmethod
    def validate_portfolio_owner_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("portfolio_owner_name must be nonblank")
        return value


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        knowledge_mode=os.getenv("KNOWLEDGE_MODE", "legacy"),
        knowledge_environment=os.getenv("KNOWLEDGE_ENVIRONMENT", "test"),
        upstash_redis_rest_url=os.getenv("UPSTASH_REDIS_REST_URL") or None,
        upstash_redis_rest_token=os.getenv("UPSTASH_REDIS_REST_TOKEN") or None,
        app_name=os.getenv("APP_NAME", "Digi-Dan"),
        portfolio_owner_name=os.getenv("PORTFOLIO_OWNER_NAME", "Daniel Zachary M. Mercurio"),
        cors_origins=_csv(os.getenv("CORS_ORIGINS")),
        allowed_origin_domains=_csv(os.getenv("ALLOWED_ORIGIN_DOMAINS")) or ["zachcodes.vercel.app"],
        api_bypass_secret=os.getenv("API_BYPASS_SECRET") or None,
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        gemini_embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "768")),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        upstash_vector_rest_url=os.getenv("UPSTASH_VECTOR_REST_URL") or None,
        upstash_vector_rest_token=os.getenv("UPSTASH_VECTOR_REST_TOKEN") or None,
        upstash_namespace=os.getenv("UPSTASH_NAMESPACE", "default"),
        bot_admin_token=os.getenv("BOT_ADMIN_TOKEN") or None,
        default_provider=os.getenv("DEFAULT_PROVIDER", "gemini"),
        generation_fallback_enabled=os.getenv("GENERATION_FALLBACK_ENABLED", "true"),
        generation_timeout_seconds=os.getenv("GENERATION_TIMEOUT_SECONDS", "20"),
        generation_total_timeout_seconds=os.getenv("GENERATION_TOTAL_TIMEOUT_SECONDS", "40"),
        retrieval_top_k=int(os.getenv("RETRIEVAL_TOP_K", "4")),
        max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "6000")),
        chat_rate_limit=int(os.getenv("CHAT_RATE_LIMIT", "10")),
        chat_rate_limit_window_seconds=int(os.getenv("CHAT_RATE_LIMIT_WINDOW_SECONDS", "3600")),
        chat_rate_limit_backend=os.getenv("CHAT_RATE_LIMIT_BACKEND", "memory"),
        chat_client_identity_mode=os.getenv("CHAT_CLIENT_IDENTITY_MODE", "legacy"),
        chat_local_client_id=os.getenv("CHAT_LOCAL_CLIENT_ID", "a" * 64),
        rate_limit_redis_rest_url=os.getenv("RATE_LIMIT_REDIS_REST_URL") or None,
        rate_limit_redis_rest_token=os.getenv("RATE_LIMIT_REDIS_REST_TOKEN") or None,
        rate_limit_redis_timeout_seconds=os.getenv("RATE_LIMIT_REDIS_TIMEOUT_SECONDS", "2"),
    )
