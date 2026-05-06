from time import monotonic
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from api.core.config import Settings, get_settings


_chat_rate_limit_buckets: dict[str, tuple[int, float]] = {}


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


def enforce_chat_rate_limit(request: Request, settings: Settings) -> None:
    if settings.chat_rate_limit <= 0:
        return

    client_id = _client_identifier(request)
    now = monotonic()
    count, reset_at = _chat_rate_limit_buckets.get(
        client_id,
        (0, now + settings.chat_rate_limit_window_seconds),
    )

    if now >= reset_at:
        count = 0
        reset_at = now + settings.chat_rate_limit_window_seconds

    if count >= settings.chat_rate_limit:
        raise HTTPException(
            status_code=429,
            detail=(
                "Digi-Dan is cooling the circuits for a moment. "
                f"You've reached the {settings.chat_rate_limit}-question limit, so please try again shortly."
            ),
        )

    _chat_rate_limit_buckets[client_id] = (count + 1, reset_at)


def _client_identifier(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()

    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()

    if request.client:
        return request.client.host

    return "unknown-client"
