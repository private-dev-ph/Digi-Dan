# Shared chat limits and trusted client identity

Implemented and verified locally. Temporary and permanent restricted credentials passed real Upstash REST/script/isolation/expiry checks on November 9, 2026. The user confirmed saving the permanent limiter URL/token to Digi-Dan Production. Eleven protected Vercel Preview identity checks passed; a comparison from a second real network remains deferred. Production publication and activation remain pending. Defaults retain a temporary `memory`/`legacy` compatibility mode; those defaults do not provide shared production enforcement.

## Request contract

The portfolio server derives a 64-character lowercase hexadecimal HMAC-SHA256 identifier from its trusted ingress IP. It sends `X-DigiDan-Client-Id` together with the existing `X-API-Secret`. In `trusted_proxy` mode Digi-Dan requires exactly one valid copy of each header, regardless of the request's Origin. Secret comparison is constant-time. Browser-supplied internal headers are discarded by the portfolio route. Direct diagnostics must use the same authenticated contract.

On Vercel the portfolio uses only a single valid `x-vercel-forwarded-for` IP, normalized across equivalent IPv6 spellings and IPv4-mapped IPv6. It rejects missing/invalid addresses and forwarded chains; it does not fall back to `x-forwarded-for`, `x-real-ip`, or browser client IDs. The portfolio must receive traffic directly through the expected Vercel ingress. An additional reverse proxy requires a new trust review. The protected Preview passed hosted spoof-resistance checks. Verify the production path during cutover before enabling enforcement.

Identity represents a network address, not a person or device. People behind a shared public IP share an allowance; VPN/network changes and different IPv6 addresses can change it. The dedicated HMAC secret is portfolio-server-only. The limiter stores opaque IDs, counts and expiry, not raw addresses or chat text. Application code does not log these values; hosting/access logs have their own policies. Rotating the HMAC key changes all identities and effectively resets allowances.

## Charging and failure behavior

The default is ten admitted requests per one-hour fixed window, beginning with the first admitted request. One valid request consumes one allowance before retrieval or generation. Provider fallback consumes no extra allowance; failed generation is not refunded. Malformed bodies, public system/namespace overrides and invalid authentication/identity are rejected before charging.

One atomic Redis Lua operation initializes a count with expiry, increments below the limit, or denies without changing count/expiry. Redis TTL determines `Retry-After`. Keys are `digi-dan:ratelimit:{test|production}:chat:v1:{opaque-id}`, using `KNOWLEDGE_ENVIRONMENT`. Window-boundary bursts remain possible. Mid-window policy changes do not rewrite existing expiry; use consistent settings across instances and allow the old window to expire.

Exhaustion returns 429 with a safe message and numeric `Retry-After`, preserved by the portfolio. Store errors, invalid state or missing Redis configuration return a generic 503 before retrieval/generation. Production Redis mode never silently switches to memory. The REST adapter has a two-second timeout by default, no redirects and no mutation retries. A lost response can leave a charged request even when the client sees 503. Health/readiness endpoints do not consume allowance and do not prove limiter health.

## Digi-Dan settings

| Variable | Meaning |
| --- | --- |
| `CHAT_RATE_LIMIT_BACKEND` | `memory` (default, local/compatibility only) or `redis`. |
| `CHAT_CLIENT_IDENTITY_MODE` | `legacy` (default), `trusted_proxy`, or `local`. |
| `CHAT_RATE_LIMIT` | Default 10; maximum 1,000,000. Zero disables charging only with memory, and does not disable trusted identity authentication. |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS` | Default 3600; range 1–604800. |
| `RATE_LIMIT_REDIS_REST_URL` | HTTPS root REST URL on `*.upstash.io`. Separate configuration from knowledge Redis. |
| `RATE_LIMIT_REDIS_REST_TOKEN` | Dedicated restricted limiter credential, never the synchronization worker's broad token. |
| `RATE_LIMIT_REDIS_TIMEOUT_SECONDS` | Default 2; positive and at most 10. Account for this plus retrieval/generation in hosting timeouts. |
| `CHAT_LOCAL_CLIENT_ID` | Fixed synthetic 64-character lowercase hex ID for explicit backend `local` mode. Defaults to 64 `a` characters. |

Redis mode requires `trusted_proxy` and a positive allowance. Backend `local` mode is forbidden with `KNOWLEDGE_ENVIRONMENT=production`. Transitional `legacy` mode hashes the socket peer address and ignores all forwarding headers; behind a proxy that can group visitors together. It is not the final production contract.

The production knowledge settings stay `KNOWLEDGE_MODE=versioned` and `KNOWLEDGE_ENVIRONMENT=production`; do not replace its read-only Redis credential. Reuse the existing database only after verifying a custom limiter credential can access the production rate-limit prefix with `EVAL`, `GET`, `SET`, `INCR` and `PTTL`, but cannot access knowledge keys. Local Redis ACL behavior and a temporary restricted Upstash REST token were verified. The hosted test admitted/denied requests with the real script, restored allowance after expiry, rejected knowledge access and out-of-prefix writes (including through EVAL), and preserved the knowledge registry. The temporary user was removed. A permanent production limiter credential subsequently passed the same access-boundary checks, was saved locally, and the user confirmed adding its URL/token to Vercel Production. If equivalent restrictions are unavailable, stop the rollout for a decision instead of using broad write access.

## Local validation

From Digi-Dan, run the isolated suites (they block outbound network):

```powershell
.\.venv\Scripts\python.exe tests/run_stage5_isolated.py
.\.venv\Scripts\python.exe tests/run_stage3_isolated.py
```

Run actual Lua/expiry/concurrency acceptance against a new disposable local Redis, using Docker Desktop. The runner refuses a nonempty database or an existing test ACL user, uses only loopback, and makes no provider/cloud calls. Port 16379 must be free. Do not point it at a shared database.

```powershell
docker run --detach --rm --name digidan-stage5-rate-local --publish 127.0.0.1:16379:6379 redis:7-alpine redis-server --save '' --appendonly no
try {
    .\.venv\Scripts\python.exe tests/run_stage5_redis_local.py --port 16379
    if ($LASTEXITCODE -ne 0) { throw 'Redis acceptance failed' }
} finally {
    docker stop digidan-stage5-rate-local
}
```

The runner bridges synthetic Upstash HTTP responses to real local Redis commands. It proves script atomicity, expiry, cross-instance sharing and local ACL isolation, not hosted Upstash REST compatibility. It tests 40 concurrent attempts, separate identities/environments, corrupt state, two independent FastAPI applications and preservation of a synthetic knowledge sentinel. Portfolio tests cover canonical identity, forged headers, missing settings and response/header propagation: run `npm test`, `npx tsc --noEmit`, and `npm run lint` in that repository.

## Later production cutover

1. Verify/provision the restricted limiter credential in the existing Redis database and confirm knowledge-key denial with that exact credential.
2. Deploy backend support with transitional settings first. Configure limiter settings separately from the knowledge read-only token.
3. Configure the portfolio with `DIGIDAN_CLIENT_IDENTITY_MODE=vercel` and a random server-only `CHAT_CLIENT_HASH_KEY` (at least 32 random bytes, encoded for storage), retaining `DIGI_DAN_API_SECRET`. Deploy and verify the actual Vercel ingress contract, including spoof attempts. Never use `NEXT_PUBLIC_` for secrets or manually emulate `VERCEL=1` in deployment.
4. Enable Digi-Dan `CHAT_CLIENT_IDENTITY_MODE=trusted_proxy` and `CHAT_RATE_LIMIT_BACKEND=redis`, then redeploy. The temporary compatibility phase is not acceptance.
5. Verify real client separation, shared counters across invocations, direct-call rejection, 429 recovery, Redis-failure behavior, knowledge isolation and existing conversation/source/fallback behavior. Confirm hosting timeouts and actual provider availability separately.

Do not enable enforcement before the portfolio sends the authenticated identity. If identity integration fails, restore the known-good deployment/configuration together; disabling shared enforcement restores the old limitations and must be recorded as a rollback, not a completed rollout.
