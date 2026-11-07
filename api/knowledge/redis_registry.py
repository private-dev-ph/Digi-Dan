"""Bounded Upstash Redis REST adapter for the versioned knowledge registry."""

import hashlib
import json
import math
import re
import threading
from urllib.parse import urlsplit

import httpx

from .ports import VersioningError, initial_state
from .registry_state import _loads_strict, _validate_state

MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_REVISION = 2**53 - 1
_KEY = re.compile(r"digi-dan:knowledge:(?:test|production):registry\Z")

_CAS_SCRIPT = """
local function safe_revision(value)
  if not value or (value ~= '0' and not string.match(value, '^[1-9][0-9]*$')) then return nil end
  local n = tonumber(value)
  if not n or n > 9007199254740991 or string.format('%.0f', n) ~= value then return nil end
  return n
end
local count = redis.call('HLEN', KEYS[1])
local rev = redis.call('HGET', KEYS[1], 'revision')
local state = redis.call('HGET', KEYS[1], 'state')
local op = redis.call('HGET', KEYS[1], 'operation_id')
local expected = ARGV[1]
local expn = safe_revision(expected)
if count == 0 then
  if expected ~= '0' then return 0 end
elseif count ~= 3 or not state or string.len(state) > 4194304 or not op
    or #op ~= 64 or not string.match(op, '^[0-9a-f]+$') or not safe_revision(rev) then
  return redis.error_reply('corrupt registry hash')
end
if not expn or expn >= 9007199254740991 then return redis.error_reply('invalid expected revision') end
if rev == string.format('%.0f', expn + 1) and op == ARGV[3] then return 1 end
if (rev or '0') ~= expected then return 0 end
redis.call('HSET', KEYS[1], 'revision', string.format('%.0f', expn + 1), 'state', ARGV[2], 'operation_id', ARGV[3])
return 1
""".strip()


class UpstashRedisRegistry:
    def __init__(self, url, token, key, *, client=None, writable=False, timeout=15):
        if not isinstance(url, str):
            raise VersioningError("registry_config", "Registry configuration is invalid")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            valid_url = (parsed.scheme == "https" and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.upstash\.io", host) is not None
                         and parsed.port is None
                         and parsed.username is None and parsed.password is None
                         and not parsed.query and not parsed.fragment
                         and parsed.path in ("", "/"))
        except Exception:
            valid_url = False
        if not valid_url or not isinstance(token, str) or not token or not isinstance(key, str) or not _KEY.fullmatch(key):
            raise VersioningError("registry_config", "Registry configuration is invalid")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise VersioningError("registry_config", "Registry configuration is invalid")
        if type(writable) is not bool:
            raise VersioningError("registry_config", "Registry configuration is invalid")
        self.url = url.rstrip("/") + "/"
        self._token = token
        self.key = key
        self.writable = bool(writable)
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=False)
        self._lock = threading.RLock()
        self._sync_token = None

    def close(self):
        if self._owns_client:
            self._client.close()

    def _request(self, command):
        headers = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        if self._sync_token:
            headers["Upstash-Sync-Token"] = self._sync_token
        response = self._client.post(self.url, json=command, headers=headers,
                                     timeout=self.timeout, follow_redirects=False)
        sync_token = response.headers.get("upstash-sync-token")
        if sync_token:
            self._sync_token = sync_token
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError("upstream failure")
        try:
            result = response.json()
        except Exception as exc:
            raise ValueError("malformed upstream response") from exc
        if not isinstance(result, dict) or "result" not in result or result.get("error"):
            raise ValueError("malformed upstream response")
        return result["result"]

    def read(self):
        try:
            with self._lock:
                count = self._request(["HLEN", self.key])
                fields = self._request(["HMGET", self.key, "revision", "state", "operation_id"])
            if type(count) is not int or count < 0:
                raise ValueError("malformed hash length")
            if count == 0 and fields == [None, None, None]:
                return 0, _validate_state(initial_state())
            if count != 3:
                raise ValueError("malformed registry hash")
            if not isinstance(fields, list) or len(fields) != 3:
                raise ValueError("malformed hash response")
            revision, raw, operation_id = fields
            if revision is None or raw is None or operation_id is None:
                raise ValueError("partial registry hash")
            if (not isinstance(revision, str) or len(revision) > 16 or not revision.isascii()
                    or not re.fullmatch(r"(?:0|[1-9][0-9]*)", revision)
                    or not isinstance(operation_id, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", operation_id)):
                raise ValueError("invalid revision")
            rev = int(revision)
            if rev > MAX_REVISION or not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_STATE_BYTES:
                raise ValueError("invalid registry payload")
            return rev, _validate_state(_loads_strict(raw))
        except VersioningError:
            raise
        except Exception as exc:
            raise VersioningError("registry_corrupt", "Registry data is malformed or unavailable") from None

    def compare_and_swap(self, revision, state):
        if not self.writable:
            raise VersioningError("registry_read_only", "Registry is read-only")
        if type(revision) is not int or not 0 <= revision < MAX_REVISION:
            raise VersioningError("invalid_revision", "Revision must be a safe nonnegative integer")
        safe = _validate_state(state)
        payload = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
            raise VersioningError("invalid_state", "Registry state exceeds the size limit")
        operation_id = hashlib.sha256((str(revision) + "\n" + payload).encode("utf-8")).hexdigest()
        command = ["EVAL", _CAS_SCRIPT, "1", self.key, str(revision), payload, operation_id]
        with self._lock:
            timed_out = False
            for attempt in range(2):
                try:
                    result = self._request(command)
                    if type(result) is not int or result not in (0, 1):
                        raise ValueError("malformed CAS response")
                    if timed_out and result == 0:
                        raise VersioningError("registry_write_uncertain", "Registry write outcome is uncertain") from None
                    return result in (1, True)
                except httpx.TimeoutException:
                    timed_out = True
                    if attempt == 0:
                        continue
                except VersioningError:
                    raise
                except Exception:
                    raise VersioningError("registry_write_uncertain", "Registry write outcome is uncertain") from None
        raise VersioningError("registry_write_uncertain", "Registry write outcome is uncertain")
