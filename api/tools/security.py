from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from api.core.config import Settings, get_settings


class AllowedOriginMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.allowed_domains = tuple(domain.lower() for domain in settings.allowed_origin_domains)
        self.bypass_secret = settings.api_bypass_secret

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not request.url.path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)

        if self._has_bypass_secret(request) or self._has_allowed_origin(request):
            return await call_next(request)

        return Response("Forbidden origin", status_code=403)

    def _has_bypass_secret(self, request: Request) -> bool:
        if not self.bypass_secret:
            return False
        supplied = request.headers.get("x-api-secret")
        return supplied == self.bypass_secret

    def _has_allowed_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        if not origin:
            return False

        host = urlparse(origin).hostname
        if not host:
            return False

        host = host.lower()
        return any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)


def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().bot_admin_token
    if not expected:
        raise HTTPException(status_code=500, detail="BOT_ADMIN_TOKEN is not configured")

    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Unauthorized")
