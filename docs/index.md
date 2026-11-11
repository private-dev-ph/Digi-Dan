# Digi-Dan documentation

Digi-Dan is a versioned retrieval-augmented generation (RAG) service for a portfolio assistant. It assembles reviewed portfolio, resume, and supplemental material into validated knowledge versions, then retrieves relevant records for each question. Gemini or DeepSeek generates the response, with eligible fallback between providers. Production uses shared Upstash Redis rate limiting through a trusted portfolio proxy.

![Digi-Dan chat interface](assets/digidan-chat.png)

## Start here

- Read the [project README](../README.md) for setup, endpoints, deployment, template use, security guidance, and troubleshooting.
- Read [Architecture](architecture.md) to understand the request path, components, and trust boundaries.
- Read [Production knowledge synchronization](knowledge-sync.md) for source assembly, validation, version preparation, activation, and periodic reconciliation.
- Read [Knowledge versioning](knowledge-versioning.md) for version selection and safe activation.
- Read [Shared rate limiting](shared-rate-limiting.md) for the production Redis limiter and trusted client identity.
- Read [Conversation continuity](conversation-continuity.md) for bounded follow-up history and request behavior.
- Read [Provider fallback](provider-fallback.md) for eligible Gemini/DeepSeek fallback behavior.
- Use [`daniel-zachary-rag-data.json`](../daniel-zachary-rag-data.json) as a legacy knowledge-base shape reference; production knowledge uses reviewed versioned synchronization.

## What the repository contains

| Area | Purpose |
| --- | --- |
| `app.py` | ASGI entrypoint for Uvicorn and Vercel. |
| `api/endpoint/` | FastAPI application, routes, and middleware registration. |
| `api/query/` | RAG orchestration and generation-provider calls. |
| `api/knowledge/` | Versioned knowledge stores, registry, and active-version resolution. |
| `api/tools/` | Knowledge synchronization, production CLI, vector access, and legacy ingestion compatibility. |
| `api/core/` | Settings, API models, and default prompt configuration. |
| `docs/assets/` | Repository-owned logo and screenshot used by the README and portfolio showcase. |

## Documentation maintenance

Keep product-specific details close to their source of truth. Update the README when endpoint behavior, required configuration, deployment, or security boundaries change. Update [`portfolio-showcase.json`](../portfolio-showcase.json) and its referenced assets when changing the public project story.

## Resume preparation

[Resume PDF preparation](resume-import.md) documents change detection, local text/OCR extraction, quality checks and preservation of the last valid extraction before reviewed knowledge assembly.

## Knowledge assembly

[Knowledge assembly](knowledge-assembly.md) describes source ownership, reviewed supplements, bounded chunks, candidate validation and the local command. [Migration inventory](knowledge-migration.md) records the disposition of all legacy documents.

## Versioned knowledge

[Knowledge versioning](knowledge-versioning.md) covers isolated version preparation, verification, activation, rollback, cleanup, and chat integration.

[Cloud validation notes](knowledge-cloud-tests.md) document cloud adapter validation and test setup.

[Production integration](production-integration.md) describes source verification and runtime readiness.
