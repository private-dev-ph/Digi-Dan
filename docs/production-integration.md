# Production integration and rollout

Production source verification and read-only runtime wiring are implemented. The live chat mode defaults to `legacy`; publishing this code does not activate a new knowledge version.

## Publication order

1. Publish Digi-Dan's processor and runtime preparation first. The old upload workflow is now manual-only, preventing a code deployment from automatically re-embedding the legacy corpus.
2. Publish the portfolio shared resolver and `/api/knowledge` export. Its Production environment must contain `PORTFOLIO_PUBLIC_URL=https://zachcodes.vercel.app` as a public config variable.
3. Verify the actual deployed export and PDF, then prepare a new candidate using the production origin. The localhost test candidate is deliberately rejected for production.
4. Configure the production Redis registry and separate runtime/worker credentials, stage and verify a production namespace, then perform the controlled mode switch. Production writes remain disabled in the factory until the production worker step is ready.

## Source readiness

```powershell
Set-Location 'C:\Users\USER\Documents\GitHub\Digi-Dan'
.\.venv\Scripts\python.exe -m api.tools.knowledge_production_check --site-url https://zachcodes.vercel.app
```

The verifier requires the trusted HTTPS origin, a complete hash-validated manifest, a Git commit and deployment identity, and a same-origin PDF with matching bytes. It reads the manifest again after downloading the PDF to detect source movement during the check. Every activation attempt must run a fresh verification; it does not cache a previous pass. This bounds the observation window but does not guarantee the website cannot change immediately after verification.

To check a production candidate against the live sources:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_production_check --site-url https://zachcodes.vercel.app --candidate .knowledge-state/production/last-successful.json
```

The command checks sources only. It does not embed documents, write vectors, change the registry or activate a version. A matching receipt is not a substitute for reviewed source policy, candidate validation and retrieval probes.

## Runtime readiness

The application constructs the cloud reader only when both `KNOWLEDGE_MODE=versioned` and `KNOWLEDGE_ENVIRONMENT=production` are explicitly configured. Construction uses `writable=False`; it cannot build or delete namespaces. Explicit service injection remains available for isolated tests. Missing configuration or an unavailable active version fails closed, with no fallback to a potentially unrelated legacy namespace.

`GET /api/health` remains a liveness check. `GET /api/knowledge/status` reports mode and versioned readiness using the registry without embeddings or a Vector scan. Successful versioned readiness exposes only the knowledge version ID. Requests still pass through the existing allowed-origin/API-secret middleware.

While mode is legacy, status indicates the selected mode; it is not a probe of Gemini/Vector availability. In versioned mode, it checks the active registry record; semantic retrieval and generation require their own smoke checks.

## Remaining deployment work

Do not switch the runtime to versioned mode before a verified production version exists. Keep the original namespace available for controlled fallback during rollout. Configure background sync credentials in GitHub Actions, with read-only database tokens in Vercel where supported. Test Redis data is not a production registry, even though the test schemas match.

Automatic portfolio deployment triggers, showcase reconciliation and scheduled sync remain Stage 4. The resume-preparation workflow produces pending artifacts on PDF changes; those artifacts do not automatically approve new facts or activate knowledge.
