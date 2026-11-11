# Provider fallback

The portfolio sends one chat request. Digi-Dan retrieves knowledge and assembles the evidence, history and trusted system instructions once, then owns generation-provider fallback. The response's `provider` names the provider that answered; `sources` and `knowledge_version` remain attached to the evidence captured for that request.

When `provider` is omitted or `null`, generation starts with `DEFAULT_PROVIDER` (Gemini by default). If fallback is enabled, only eligible temporary generation failures may try the other provider once. There are at most two generation attempts and no repeated-provider retries. An explicitly selected `gemini` or `deepseek` provider is pinned and never falls back to the other provider. The same assembled prompt is used for both attempts.

## Failure policy

| Failure | Behavior |
| --- | --- |
| Generation timeout, connection error, provider HTTP 408/429/5xx | Eligible for one attempt with the other provider when fallback is enabled. |
| Empty answer or malformed generation response without a detected refusal | Eligible for one attempt with the other provider when fallback is enabled. |
| Missing credentials/model or provider HTTP 401/403/404 | Stop with a safe configuration error. |
| Other provider rejection or unexpected internal error | Stop with a safe error. |
| Explicit provider safety/refusal signal | Stop; do not switch providers to bypass it. A normal text refusal is returned as the answer. |
| Invalid chat, forbidden origin, failed identity/secret check, rate-limit denial, or retrieval failure | No generation fallback. |

Failure logs use the provider, attempt number and fixed failure category. They omit keys, request text, raw provider responses and exception messages. Each failed attempt is recorded separately so a backup error does not erase the primary failure. Public errors use safe detail strings.

## Time limits and transport

`GENERATION_TIMEOUT_SECONDS` defaults to 20 seconds per attempt (positive, at most 120). `GENERATION_TOTAL_TIMEOUT_SECONDS` defaults to 40 seconds for generation as a whole (positive, at most 240). An attempt receives the smaller of its configured allowance and the remaining shared budget. An expired request cannot start a backup or return a late successful answer. These limits cover generation only. Version resolution, query embedding and vector retrieval happen before generation and have their own timeouts.

Generation uses non-streaming asynchronous HTTP with a cancellable timeout through the existing synchronous FastAPI worker path. HTTP transport retries and redirects are disabled. This avoids hidden SDK generation retries and the installed Gemini SDK's blocking async shim. Embeddings continue to use their existing implementation. The portfolio forwards its abort signal upstream; browser cancellation does not guarantee cancellation of work already running in Digi-Dan or at a provider.

Gemini embeddings remain a required retrieval dependency even when DeepSeek handles generation. An embedding-provider failure prevents retrieval and does not trigger generation fallback. Provider fallback cannot substitute a different embedding provider. A successful request to each provider individually does not establish failover behavior; exercise fallback with a controlled primary-provider failure.

Wire formats follow [Gemini generateContent](https://ai.google.dev/api/generate-content?hl=en) and [DeepSeek chat completions](https://api-docs.deepseek.com/api/create-chat-completion/). Model names, credentials, and enabled provider settings are deployment configuration.

See [shared rate limiting](shared-rate-limiting.md) for how a single admitted request is charged. Fallback does not consume a second allowance. Conversation and prompt boundaries are documented in [conversation continuity](conversation-continuity.md).
