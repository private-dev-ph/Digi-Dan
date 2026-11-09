"""Local and Upstash REST rate limiters for chat requests."""

from dataclasses import dataclass
import math
import re
import threading
import time
from urllib.parse import urlsplit

import httpx


_SAFE_ERROR = "Rate limit service is unavailable"
_CLIENT_ID = re.compile(r"[0-9a-f]{64}\Z")
_UPSTASH_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.upstash\.io\Z")


class RateLimitUnavailable(Exception):
    """Raised when the limiter cannot safely determine a decision."""

    def __init__(self):
        super().__init__(_SAFE_ERROR)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after: int


def _validate_policy(client_id, limit, window_seconds):
    if (not isinstance(client_id, str) or _CLIENT_ID.fullmatch(client_id) is None
            or type(limit) is not int or not 1 <= limit <= 1_000_000
            or type(window_seconds) is not int or not 1 <= window_seconds <= 604_800):
        raise ValueError("Invalid rate limit policy")


RATE_LIMIT_SCRIPT = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
if not limit or limit < 1 or limit > 1000000 or limit ~= math.floor(limit)
    or not window or window < 1 or window > 604800000 or window ~= math.floor(window) then
  return redis.error_reply('invalid rate limit policy')
end
local value = redis.call('GET', KEYS[1])
if not value then
  redis.call('SET', KEYS[1], '1', 'PX', window)
  return {1, limit - 1, window}
end
if #value > 7 or not string.match(value, '^[1-9][0-9]*$') then
  return redis.error_reply('corrupt rate limit counter')
end
local count = tonumber(value)
if not count or count > 1000000 or string.format('%.0f', count) ~= value then
  return redis.error_reply('corrupt rate limit counter')
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 or ttl > 604800000 then return redis.error_reply('corrupt rate limit expiry') end
if count >= limit then return {0, 0, ttl} end
count = redis.call('INCR', KEYS[1])
return {1, limit - count, ttl}
""".strip()


class LocalMemoryRateLimiter:
    def __init__(self, environment="test", clock=time.monotonic):
        if environment not in ("test", "production") or not callable(clock):
            raise ValueError("Invalid rate limiter configuration")
        self.environment = environment
        self._clock = clock
        self._entries = {}
        self._lock = threading.Lock()

    def consume(self, client_id, limit, window_seconds):
        _validate_policy(client_id, limit, window_seconds)
        with self._lock:
            now = self._clock()
            for key, (_count, reset) in list(self._entries.items()):
                if reset <= now:
                    del self._entries[key]
            entry = self._entries.get(client_id)
            if entry is None:
                reset = now + window_seconds
                self._entries[client_id] = (1, reset)
                return RateLimitDecision(True, limit - 1, window_seconds)
            count, reset = entry
            retry = max(1, math.ceil(reset - now))
            if count >= limit:
                return RateLimitDecision(False, 0, retry)
            count += 1
            self._entries[client_id] = (count, reset)
            return RateLimitDecision(True, limit - count, retry)


class RedisRateLimiter:
    def __init__(self, url, token, environment, *, client=None, timeout=2.0):
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            host = parsed.hostname or "" if parsed is not None else ""
            valid_url = (parsed is not None and parsed.scheme == "https"
                         and _UPSTASH_HOST.fullmatch(host) is not None and parsed.port is None
                         and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment and parsed.path in ("", "/"))
        except Exception:
            valid_url = False
        if (not valid_url or not isinstance(token, str) or not token.strip()
                or environment not in ("test", "production")
                or type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise RateLimitUnavailable() from None
        self.url = url.rstrip("/") + "/"
        self._token = token
        self.environment = environment
        self.timeout = timeout
        self._prefix = f"digi-dan:ratelimit:{environment}:chat:v1:"
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(follow_redirects=False)

    def close(self):
        if self._owns_client:
            self._client.close()

    def consume(self, client_id, limit, window_seconds):
        _validate_policy(client_id, limit, window_seconds)
        key = self._prefix + client_id
        command = ["EVAL", RATE_LIMIT_SCRIPT, "1", key, str(limit), str(window_seconds * 1000)]
        try:
            response = self._client.post(
                self.url, json=command,
                headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
                timeout=self.timeout, follow_redirects=False)
            if response.status_code < 200 or response.status_code >= 300:
                raise ValueError
            payload = response.json()
            if not isinstance(payload, dict) or set(payload) != {"result"}:
                raise ValueError
            result = payload["result"]
            if not isinstance(result, list) or len(result) != 3:
                raise ValueError
            allowed, remaining, ttl_ms = result
            if (type(allowed) is not int or allowed not in (0, 1)
                    or type(remaining) is not int or not 0 <= remaining < limit
                    or type(ttl_ms) is not int or not 1 <= ttl_ms <= 604_800_000
                    or (allowed == 0 and remaining != 0)):
                raise ValueError
            return RateLimitDecision(bool(allowed), remaining, max(1, math.ceil(ttl_ms / 1000)))
        except Exception:
            raise RateLimitUnavailable() from None
