import hashlib
import hmac
import re
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from api.core.config import Settings, get_settings


_CLIENT_ID = re.compile(r"[0-9a-f]{64}\Z")
_UNAVAILABLE_DETAIL = "Chat limits are temporarily unavailable. Please try again shortly."


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
        values = _header_values(request, b"x-api-secret")
        if len(values) != 1:
            return False
        try:
            supplied = values[0].decode("utf-8")
            expected = self.bypass_secret.encode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), expected)

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
    """Authenticate the configured client identity and charge one chat attempt."""
    mode = settings.chat_client_identity_mode
    if mode == "trusted_proxy":
        secret = settings.api_bypass_secret
        if not isinstance(secret, str) or not secret.strip():
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)
        secret_values = _header_values(request, b"x-api-secret")
        if len(secret_values) != 1 or not _constant_time_header_match(secret_values[0], secret):
            raise HTTPException(status_code=403, detail="Forbidden")
        identity_values = _header_values(request, b"x-digidan-client-id")
        if len(identity_values) != 1:
            raise HTTPException(status_code=403, detail="Forbidden")
        try:
            client_id = identity_values[0].decode("ascii")
        except UnicodeDecodeError:
            raise HTTPException(status_code=403, detail="Forbidden") from None
        if _CLIENT_ID.fullmatch(client_id) is None:
            raise HTTPException(status_code=403, detail="Forbidden")
    elif mode == "local":
        client_id = settings.chat_local_client_id
        if not isinstance(client_id, str) or _CLIENT_ID.fullmatch(client_id) is None:
            raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)
    elif mode == "legacy":
        host = request.client.host if request.client and request.client.host else "local-unknown"
        client_id = hashlib.sha256(host.encode("utf-8")).hexdigest()
    else:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)

    # Identity is deliberately checked before the quota-disabled fast path.
    if settings.chat_rate_limit <= 0:
        return

    limiter = getattr(request.app.state, "chat_rate_limiter", None)
    if limiter is None:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL)
    try:
        decision = limiter.consume(
            client_id,
            settings.chat_rate_limit,
            settings.chat_rate_limit_window_seconds,
        )
    except Exception:
        # Keep store errors and identifiers out of API responses and logs.
        raise HTTPException(status_code=503, detail=_UNAVAILABLE_DETAIL) from None
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                "Digi-Dan is cooling the circuits for a moment. "
                f"You've reached the {settings.chat_rate_limit}-question limit, "
                f"so please try again in {decision.retry_after} seconds."
            ),
            headers={"Retry-After": str(decision.retry_after)},
        )


def _header_values(request: Request, name: bytes) -> list[bytes]:
    lowered = name.lower()
    return [value for key, value in request.scope.get("headers", ()) if key.lower() == lowered]


def _constant_time_header_match(value: bytes, expected: str) -> bool:
    try:
        supplied = value.decode("utf-8").encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return False
    return hmac.compare_digest(supplied, expected_bytes)
