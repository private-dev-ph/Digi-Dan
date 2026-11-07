from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.core.config import get_settings
from api.core.models import ChatRequest, ChatResponse, IngestRequest, IngestResponse
from api.query.rag import answer_question, ingest_documents
from api.tools.security import AllowedOriginMiddleware, enforce_chat_rate_limit, require_admin_token
from api.knowledge.ports import VersioningError


def create_app(*, settings=None, knowledge_service=None) -> FastAPI:
    settings = settings or get_settings()
    if knowledge_service is None and settings.knowledge_mode == "versioned" and settings.knowledge_environment == "production":
        try:
            from api.knowledge.cloud import create_cloud_service

            knowledge_service = create_cloud_service(settings, writable=False)
        except VersioningError:
            knowledge_service = None
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
    app.state.knowledge_service = knowledge_service

    @app.get("/")
    def root() -> dict[str, str]:
        return {"name": settings.app_name, "status": "ok"}

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/knowledge/status")
    def knowledge_status():
        if settings.knowledge_mode == "legacy":
            return {"mode": "legacy", "ready": True}
        if knowledge_service is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail={"code": "knowledge_unavailable", "message": "Knowledge service is unavailable"})
        try:
            active = knowledge_service.get_active()
        except VersioningError:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail={"code": "knowledge_unavailable", "message": "Knowledge service is unavailable"}) from None
        if not active:
            from fastapi import HTTPException

            raise HTTPException(status_code=503, detail={"code": "knowledge_unavailable", "message": "Knowledge service is unavailable"})
        return {"mode": "versioned", "ready": True, "knowledge_version": active["version_id"]}

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: Request, chat_request: ChatRequest) -> ChatResponse:
        enforce_chat_rate_limit(request, settings)
        return answer_question(chat_request, settings, knowledge_service=knowledge_service)

    @app.post(
        "/api/ingest",
        response_model=IngestResponse,
        dependencies=[Depends(require_admin_token)],
    )
    def ingest(request: IngestRequest) -> IngestResponse:
        return ingest_documents(request, settings)

    return app
