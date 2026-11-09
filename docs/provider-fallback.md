# Provider fallback

The portfolio sends one chat request. Digi-Dan retrieves knowledge and assembles evidence, history and system instructions once, then owns generation fallback. The response's `provider` names the provider that answered; `sources` and `knowledge_version` remain attached to the evidence captured for that request.

Omitting `provider`, or setting it to `null`, starts with `DEFAULT_PROVIDER` (Gemini by default). An eligible failure can try the other provider once when `GENERATION_FALLBACK_ENABLED=true`. Explicit `gemini` or `deepseek` selects only that provider. There are at most two generation attempts, with no repeated-provider retries. This applies to legacy and versioned retrieval.

## Failure policy

| Failure | Behavior |
| --- | --- |
| Generation timeout, connection error, provider HTTP 408/429/5xx | Eligible for the other provider. |
| Empty answer or malformed generation response without a detected refusal | Eligible for the other provider. |
| Missing credentials/model or provider HTTP 401/403/404 | Stop with a safe configuration error. |
| Other provider request rejection or unexpected internal error | Stop with a safe error. |
| Explicit provider safety/refusal signal | Stop; never switch to bypass it. A normal text refusal is returned as the answer. |
| Invalid chat, forbidden origin, Digi-Dan rate limit, or retrieval failure | No generation fallback. |

Failure logs contain only the provider, attempt number and fixed failure category. They do not include keys, request text, raw provider responses or exception messages. Each failed attempt is recorded separately, so a backup error does not erase the primary failure. Public errors use safe `detail` strings, which the portfolio displays.

## Time limits and transport

`GENERATION_TIMEOUT_SECONDS` defaults to 20 seconds per attempt (positive, at most 120). `GENERATION_TOTAL_TIMEOUT_SECONDS` defaults to 40 seconds for generation as a whole (positive, at most 240). An attempt gets the smaller of its configured allowance and the remaining shared budget. Expired requests cannot start a backup or return a late successful answer. Retrieval happens before this deadline and has its own service timeouts.

Generation uses non-streaming async HTTP wrapped in a cancellable timeout, through the existing synchronous FastAPI worker path. HTTP transport retries and redirects are disabled. This avoids hidden SDK generation retries and the installed Gemini SDK's blocking async shim. Embeddings still use their existing implementation. The portfolio forwards its abort signal upstream; browser cancellation does not guarantee cancellation of work already running in the backend or at a provider.

Wire formats follow [Gemini generateContent](https://ai.google.dev/api/generate-content?hl=en) and [DeepSeek chat completions](https://api-docs.deepseek.com/api/create-chat-completion/). Model names and credentials remain server configuration. No model migration is part of this change.

## Local validation and deployment

Run `.\.venv\Scripts\python.exe tests/run_stage5_isolated.py` and `tests/run_stage3_isolated.py` in Digi-Dan, plus `npm test`, `npx tsc --noEmit` and `npm run lint` in the portfolio. Tests replace provider HTTP with synthetic responses and verify classification, cancellation, bounded attempts, identical fallback prompts, one retrieval, one rate-limit charge, evidence provenance and the single-request proxy. They do not contact model providers.

Production deployment remains a separate step. Validate the enabled providers' credentials/model availability and the hosting timeout before publishing. Publish the backend before the single-request portfolio proxy so automatic fallback is already available when the caller switches. DeepSeek generation does not replace Gemini embeddings: a Gemini embedding outage still prevents retrieval. Shared rate limiting and trusted client identity are also implemented locally; see [shared rate limiting](shared-rate-limiting.md) for the separate hosted setup and cutover.
