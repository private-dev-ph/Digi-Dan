# Conversation continuity

The portfolio chat retains recent successful exchanges in component memory and sends them through the portfolio server proxy to Digi-Dan. History is not stored in a new database or browser persistent storage. It is context for follow-up and is not authoritative factual evidence.

## Request contract

The portfolio sends the current `message` and optional `history`. History contains only completed successful user/assistant exchanges, ordered oldest to newest. The current question is not duplicated in history. The initial UI greeting, failed requests and UI error messages are excluded. A later successful user/assistant exchange that happens to be a greeting can be retained as ordinary conversation history.

Both the portfolio proxy and Digi-Dan validate the bounded contract:

| Field | Limit |
| --- | --- |
| Current question | 2,000 Unicode code points; nonblank |
| History messages | Eight, forming at most four complete alternating user/assistant pairs |
| Each history message | 2,000 Unicode code points; nonblank |
| Combined history content | 8,000 Unicode code points |
| History item keys | Only `role` and `content` |

The browser retains the newest complete exchanges that fit and may shorten long assistant replies in the outgoing history. The visible reply is unchanged. Direct callers must supply valid bounded history; servers reject malformed or oversized payloads rather than silently rewriting them.

## Retrieval and generation boundaries

For recognized referential or elliptical questions, Digi-Dan augments the retrieval query with bounded recent subject hints. This is deterministic query augmentation, not a model-generated rewrite. Standalone questions retrieve from the current question alone. Each request resolves one active version and uses that captured version for its retrieval.

Generation receives the history separately from retrieved evidence and the current question. History is untrusted conversational context: an earlier assistant response does not establish a fact or override current retrieved knowledge. If the current evidence is missing, the assistant should say so; ambiguous references may require clarification. Public callers cannot supply system instructions.

The current prompt renderer substitutes trusted server configuration such as the configured portfolio owner name and application name into the system-instruction template. Values such as `PORTFOLIO_OWNER_NAME` are validated/normalized before rendering. Prompt slots are rendered from trusted server configuration. Retrieved chunks and conversation history are not interpolated into the system instructions; they remain in the user/context portion of the prompt.

The response's source records contain only complete, nonblank retrieved chunks that were actually admitted to the generation context. The context budget includes identifiers, text and separators; chunks that do not fit are skipped. Source records identify supplied evidence; they are not per-claim citations and do not establish that every generated statement follows from a source. History is never reported as a source.

If history is empty or absent, the system instructions say earlier conversation is unavailable. Retrieved project records cannot establish what the user discussed previously; when asked about a prior topic in that state, the assistant should ask the user to name it.

The bounded history window and subject hints cannot resolve every natural-language reference. Long or ambiguous follow-ups may need clarification.

## Browser lifecycle and privacy

History remains in chat component memory. Clearing or leaving the mounted chat discards it. Clear aborts an in-flight browser request and invalidates a late reply so an old answer cannot repopulate the cleared conversation or disrupt a newer request. There is no backend history-delete call.

On subsequent questions, retained history reaches Digi-Dan through the portfolio proxy and is included in the generation request. Bounded follow-up subject hints can also reach Gemini embeddings. This is a hosted model-service path, not local-only inference.

See [provider fallback](provider-fallback.md) for generation failure handling and [shared rate limiting](shared-rate-limiting.md) for the authenticated proxy request contract.
