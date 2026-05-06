import os
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "Digi-Dan"
    cors_origins: list[str] = Field(default_factory=list)
    allowed_origin_domains: list[str] = Field(default_factory=lambda: ["yashashm.dev"])
    api_bypass_secret: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
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
    retrieval_top_k: int = 4
    max_context_chars: int = 6000


def _csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        app_name=os.getenv("APP_NAME", "Digi-Dan"),
        cors_origins=_csv(os.getenv("CORS_ORIGINS")),
        allowed_origin_domains=_csv(os.getenv("ALLOWED_ORIGIN_DOMAINS")) or ["yashashm.dev"],
        api_bypass_secret=os.getenv("API_BYPASS_SECRET") or None,
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
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
        retrieval_top_k=int(os.getenv("RETRIEVAL_TOP_K", "4")),
        max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "6000")),
    )
