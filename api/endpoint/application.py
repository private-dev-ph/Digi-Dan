from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.core.config import get_settings
from api.core.models import ChatRequest, ChatResponse, IngestRequest, IngestResponse
from api.query.rag import answer_question, ingest_documents
from api.tools.security import AllowedOriginMiddleware, enforce_chat_rate_limit, require_admin_token


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(AllowedOriginMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Secret"],
    )

    @app.get("/")
    def root() -> dict[str, str]:
        return {"name": settings.app_name, "status": "ok"}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: Request, chat_request: ChatRequest) -> ChatResponse:
        enforce_chat_rate_limit(request, settings)
        return answer_question(chat_request, settings)

    @app.post(
        "/api/ingest",
        response_model=IngestResponse,
        dependencies=[Depends(require_admin_token)],
    )
    def ingest(request: IngestRequest) -> IngestResponse:
        return ingest_documents(request, settings)

    return app
