# Conversation continuity

Stage 5's first increment adds recent conversation to chat requests. Knowledge synchronization, provider fallback and rate-limit policy remain separate concerns.

## Request contract

The portfolio sends the current `message` and optional `history`. History contains only completed, successful user/assistant exchanges, ordered oldest to newest. The current question is not duplicated in history. The greeting, failed requests and UI error messages are excluded.

Both the portfolio proxy and Digi-Dan enforce these limits:

| Field | Limit |
| --- | --- |
| Current question | 2,000 Unicode code points; nonblank |
| History messages | Eight, in complete alternating user/assistant pairs |
| Each history message | 2,000 Unicode code points; nonblank |
| Combined history content | 8,000 Unicode code points |
| History item keys | Only `role` and `content` |

The browser keeps the newest complete exchanges that fit and shortens long assistant replies for the outgoing history. The visible answer is not shortened. Direct callers must supply a valid bounded history; the servers reject malformed or oversized requests rather than silently rewriting them.

## How follow-ups use history

Recognized referential or elliptical questions use bounded recent subject hints alongside the current question for retrieval. This is deterministic query augmentation, not a model-generated rewrite. Standalone questions continue to retrieve using the question itself. Every request retrieves current knowledge, and a versioned request uses one captured active version throughout retrieval.

Generation receives conversation history separately from retrieved evidence and the current question. Server-controlled instructions treat history as untrusted conversational context: an earlier assistant claim cannot establish a fact or override newly retrieved knowledge. If a subject is ambiguous, the assistant should ask for clarification; if current evidence is missing, it should say so. Public callers cannot supply system instructions.

The response includes only complete, nonblank retrieved chunks actually supplied as generation context. The character budget counts chunk identifiers, text, and separators; a chunk that does not fit is skipped, so fewer sources may be returned. These source records show which evidence was supplied, not per-claim citations or proof that every answer statement follows from it. Conversation history remains separate and is never represented as a source.

When history is absent or empty, the system prompt explicitly says there is no earlier conversation available. Retrieved project records cannot establish what the user previously discussed. In that case, for questions asking which topic was discussed earlier, the assistant should say history is unavailable and ask the user to name the topic; this takes priority over answering from the closest retrieved project evidence.

The server renders the system prompt with the trusted `portfolio_owner_name` setting (default: `Daniel Zachary M. Mercurio`) and `app_name`. Set `PORTFOLIO_OWNER_NAME` to configure the owner; the value is trimmed and limited to 120 characters. Retrieved text and conversation history remain in the user prompt and are never interpolated into system instructions.

This first implementation does not guarantee resolution of every natural-language reference. Long conversations outside the retained window, unusual follow-up phrasing and ambiguous references can require clarification. Automated local tests verify payloads, retrieval inputs and prompt boundaries; they do not prove a live model will always follow the instructions.

## Browser lifecycle and privacy

History exists in the chat component's memory, with no new database or browser persistent storage. Clearing or leaving the mounted chat discards its context. Clear also aborts an in-flight request and invalidates any late reply, so an old answer cannot repopulate a cleared conversation or disrupt a newer request. There is no backend history-delete call.

On subsequent questions, retained conversation is sent through the portfolio proxy to Digi-Dan and included in the generation prompt. Bounded subject hints can also reach the embedding provider. This follows the existing model-service path; it is not local-only inference. Local automated verification uses fakes and blocks external calls.

## Local checks

From Digi-Dan, run `.\.venv\Scripts\python.exe tests/run_stage5_isolated.py` and `.\.venv\Scripts\python.exe tests/run_stage3_isolated.py`. From the portfolio, run `npm test`, `npx tsc --noEmit` and `npm run lint`.

Acceptance covers no-history compatibility, follow-up retrieval, a standalone topic change, role/size/sequence validation, Unicode length agreement, exclusion of failed exchanges, clearing during a request and prevention of stale replies. [Provider fallback](provider-fallback.md) is now centralized locally in Digi-Dan; [shared rate limiting and trusted client identity](shared-rate-limiting.md) are also implemented locally, with hosted acceptance still pending.

## Live local acceptance (2026-11-09)

Six acceptance samples passed against the local portfolio proxy and backend using Gemini 2.5 Flash and read-only production knowledge: AOI overview, technology follow-up, inspection-time follow-up, education topic change, clarification after Clear, and correcting a stale 150% history claim using the current five-minutes-to-under-two result (more than 60% reduction). The last two metric requests were exercised through browser fetch to the real proxy; the other cases used the chat UI. These are sampled model checks, not a guarantee for every phrasing.

The checks exposed two prompt issues corrected locally: unresolved identity placeholders could produce an invented owner name, and an empty history could lead the model to mistake retrieved project evidence for a previous discussion. Isolated verification passed 10 Stage 5 tests and 143 Stage 3 tests without network attempts. Intermittent embedding-provider errors and one generation server error also occurred; successful retries do not resolve those reliability concerns. At that time, proxy fallback masked those errors with the test harness's Gemini-only rejection. The subsequent local provider-fallback increment removes that proxy retry and classifies generation errors in Digi-Dan; retrieval failures still stop before generation.

The production active version, registry revision and generation were unchanged. No deployment was performed. The capped live test process was stopped and the localhost preview returned to its synthetic test fixture afterward.
