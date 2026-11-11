# Shared chat limits and trusted client identity

The production Digi-Dan backend uses `CHAT_CLIENT_IDENTITY_MODE=trusted_proxy` and `CHAT_RATE_LIMIT_BACKEND=redis`. The portfolio proxy uses Vercel ingress identity mode and a server-only HMAC key. The limiter uses Redis with a separate restricted credential; it does not reuse the knowledge registry's read-only credential. Code defaults remain `memory`/`legacy` for local and compatibility use and do not describe Production.

## Authenticated request and identity

The browser calls the portfolio server route. The route removes any browser-supplied internal identity headers, derives a 64-character lowercase hexadecimal HMAC-SHA256 identifier from the trusted Vercel ingress IP, then forwards the request with `X-DigiDan-Client-Id` and `X-API-Secret`. Digi-Dan requires exactly one valid copy of both headers in `trusted_proxy` mode, regardless of Origin. An allowed Origin alone is insufficient. Secret comparison is constant-time.

On Vercel the portfolio accepts only a single valid `x-vercel-forwarded-for` IP, normalized across equivalent IPv6 and IPv4-mapped IPv6 forms. It rejects missing/invalid values and forwarded chains; it does not fall back to `x-forwarded-for`, `x-real-ip`, or browser client IDs. The proxy path must remain directly behind the expected Vercel ingress; a new reverse proxy requires a fresh trust review.

Identity represents a network address, not a person or device. People behind a shared public IP share the same allowance. VPN changes and different IPv6 addresses can produce a different identity. The dedicated HMAC key is portfolio-server-only. Redis stores opaque IDs, counts and expiries, not raw addresses or chat text. Rotating the HMAC key changes identities and effectively starts new allowances.

## Charging and failure behavior

The configured allowance is ten admitted chat requests per one-hour fixed window, beginning with the first admitted request. One valid request consumes one allowance before retrieval or generation. Provider fallback does not consume another allowance, and a failed generation is not refunded. Invalid payloads and invalid authentication/identity are rejected before charging.

An atomic Redis Lua operation initializes a count with expiry, increments below the limit, or denies without changing count or expiry. Redis TTL determines `Retry-After`. Keys use the `digi-dan:ratelimit:{test|production}:chat:v1:{opaque-id}` prefix, selected by `KNOWLEDGE_ENVIRONMENT`; limiter data and credentials remain separate from knowledge data/credentials. Fixed windows can allow bursts across boundaries. Changing policy mid-window does not rewrite existing expiry, so settings should stay consistent across instances.

Exhaustion returns 429 with a safe message and numeric `Retry-After`; the portfolio preserves the status and header. Store errors, corrupt state or missing Redis configuration return a generic 503 before retrieval or generation. Production Redis mode does not silently fall back to memory. The REST adapter uses a two-second timeout by default, no redirects and no mutation retries. A lost response can leave a request charged even if the client sees 503. Health/readiness endpoints do not consume allowance and do not prove limiter health.

## Settings and setup guidance

| Variable | Meaning |
| --- | --- |
| `CHAT_RATE_LIMIT_BACKEND` | `redis` in Production; `memory` is a local/compatibility implementation. |
| `CHAT_CLIENT_IDENTITY_MODE` | `trusted_proxy` in Production; `legacy` and `local` support explicit compatibility/local modes. |
| `CHAT_RATE_LIMIT` | Default 10; maximum 1,000,000. Production uses a positive value. |
| `CHAT_RATE_LIMIT_WINDOW_SECONDS` | Default 3600; range 1–604800 seconds. Production uses one hour. |
| `RATE_LIMIT_REDIS_REST_URL` | HTTPS Upstash REST root, configured separately from knowledge Redis. |
| `RATE_LIMIT_REDIS_REST_TOKEN` | Dedicated restricted limiter credential. Never substitute the synchronization worker's broad token or knowledge read-only token. |
| `RATE_LIMIT_REDIS_TIMEOUT_SECONDS` | Default 2; positive and at most 10 seconds. |
| `CHAT_LOCAL_CLIENT_ID` | Synthetic 64-character lowercase hex identity for explicit backend `local` mode. |

The production knowledge settings remain versioned and use the production environment. Keep the knowledge read-only Redis credential separate. The limiter credential must be scoped to the limiter prefix and cannot access the knowledge registry. Reusable environment and credential setup guidance remains in the repository's production setup documentation.

## Operational verification guidance

Test shared-counter behavior with synthetic identities and controlled state; do not exhaust or modify a real visitor's quota. Synthetic identities validate counter behavior but do not prove real-network separation or separate proxy instances. A successful request does not establish behavior during a Redis outage; verify fail-closed behavior using controlled failure conditions.

See [architecture](architecture.md) for request flow and [conversation continuity](conversation-continuity.md) for the bounded payload contract.
