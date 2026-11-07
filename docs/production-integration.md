# Production integration and rollout

Production source verification and read-only runtime wiring are implemented. The live chat mode defaults to `legacy`; publishing this code does not activate a new knowledge version.

## Publication order

1. Publish Digi-Dan's processor and runtime preparation first. The old upload workflow is now manual-only, preventing a code deployment from automatically re-embedding the legacy corpus.
2. Publish the portfolio shared resolver and `/api/knowledge` export. Its Production environment must contain `PORTFOLIO_PUBLIC_URL=https://zachcodes.vercel.app` as a public config variable.
3. Verify the actual deployed export and PDF, then prepare a new candidate using the production origin. The localhost test candidate is deliberately rejected for production.
4. Configure the production Redis registry and separate runtime/worker credentials, stage and verify a production namespace, then perform the controlled mode switch. The general cloud factory rejects production writes. Dedicated staging and rollout paths perform the controlled writes; the deployed chatbot uses read-only adapters.

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

## Credential preflight

Create `.env.cloud-production` locally with the production `KNOWLEDGE_ENVIRONMENT`,
`UPSTASH_REDIS_REST_URL`, worker `UPSTASH_REDIS_REST_TOKEN`, and distinct
`UPSTASH_REDIS_REST_READ_ONLY_TOKEN`. Supply the production Vector URL/token and
Gemini embedding key as recognized settings (in the file or existing environment).
The file is gitignored. Then run:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_production_preflight
.\.venv\Scripts\python.exe -m api.tools.knowledge_production_preflight --online
```

The first command validates configuration without constructing cloud clients. The
second reads the registry with worker credentials and again with the runtime token,
then inspects the reserved Vector namespace name for dimensions. Both constructions
use `writable=False`; no write, embedding request, or live chat mode change occurs.
A blank registry is valid. Successful reads prove only that those reads succeeded;
they do not prove either credential's write permissions or establish Redis
read-only enforcement. This preflight does not activate production knowledge. The
`UPSTASH_REDIS_REST_READ_ONLY_TOKEN` name is for this local preflight file; when
configuring Vercel later, place that value in its `UPSTASH_REDIS_REST_TOKEN` setting.
The application does not automatically read the new `READ_ONLY_TOKEN` name.

## Production staging and status

The production CLI supports `status`, `stage`, `activate`, `rollback`, and
`verify`. These commands validate the production overlay and worker/runtime
separation. `status` uses the runtime token
with `writable=False` and prints only registry revision, generation, desired and
active selection, and selected version fields. It never scans vectors or embeds.

Prepare a policy-reviewed production candidate first. Then stage it with an
explicit generation read from the latest `status` output:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_production status
.\.venv\Scripts\python.exe -m api.tools.knowledge_production stage `
  --candidate .knowledge-state/production/last-successful.json `
  --site-url https://zachcodes.vercel.app `
  --expected-generation 0
```

The default resume input is `.resume-state/production/last-successful.json`; the
default retrieval fixture is `knowledge/production-retrieval-probes.json`. Override
them with `--resume` or `--probes` when needed. Staging rebuilds the candidate from
the current deployed manifest, resume snapshot, source policy, and legacy input,
then requires a full match with the supplied candidate before constructing the
writer. The expected generation is mandatory to prevent stale concurrent staging.

Staging records an intent, desired version, and incremented generation before
uploading vectors. After stored-vector checks and retrieval probes, the core
marks the record verified; the staging wrapper then performs a final
source verification before reporting success. It does not change the active
version or live chat mode. Failures retain the previous active version. A failure
of that final check can leave a verified inactive record or namespace behind.
When a reviewed sync has unchanged content, it refreshes the active source receipt
atomically while keeping the existing version ID and namespace.
Activation requires an explicitly reviewed ticket, candidate file, site URL, and
current generation. It revalidates the candidate and probes, rebuilds it from
the current deployed manifest and policy, and requires an exact match before
constructing the writer. The rollout core then checks the generation and freshly
verifies deployed sources as part of activation. Rollback takes the same explicit
inputs and current generation; the target must be retained and still match the
current source policy and deployed source state. Neither command edits the live
chat mode or retries automatically.

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_production activate 0123456789abcdef `
  --candidate .knowledge-state/production/last-successful.json `
  --site-url https://zachcodes.vercel.app `
  --expected-generation 1
```

Use `rollback <ticket>` with the same options when the target is an eligible
retained version. A first production version has no historical production
version to roll back to. In that case, emergency fallback is switching the
runtime configuration to legacy and redeploying. Keep the legacy 48-namespace
corpus available for that fallback. Later rollbacks still require the target to
pass current review policy and deployed-source checks; otherwise stage a fresh
candidate from the current sources.

The read-only runtime smoke command verifies the active ticket, deployed source
receipt, stored vectors, and retrieval probes. It uses the runtime token and
does not require the worker token or perform writes. Probe execution makes
single-attempt embedding requests, matching normal runtime behavior.

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_production verify `
  --ticket 0123456789abcdef `
  --site-url https://zachcodes.vercel.app
```

## Runtime readiness

The application constructs the cloud reader only when both `KNOWLEDGE_MODE=versioned` and `KNOWLEDGE_ENVIRONMENT=production` are explicitly configured. Construction uses `writable=False`; it cannot build or delete namespaces. Explicit service injection remains available for isolated tests. Missing configuration or an unavailable active version fails closed, with no fallback to a potentially unrelated legacy namespace.

`GET /api/health` remains a liveness check. `GET /api/knowledge/status` reports mode and versioned readiness using the registry without embeddings or a Vector scan. Successful versioned readiness exposes only the knowledge version ID. Requests still pass through the existing allowed-origin/API-secret middleware.

While mode is legacy, status indicates the selected mode; it is not a probe of Gemini/Vector availability. In versioned mode, it checks the active registry record; semantic retrieval and generation require their own smoke checks.

## Remaining deployment work

Do not switch the runtime to versioned mode before a verified production version exists. Keep the original namespace available for controlled fallback during rollout. Configure the Vercel runtime with the read-only Redis token and the explicit versioned production settings when ready; the CLI cannot publish that configuration or change the live chat mode. Configure background sync credentials in GitHub Actions, with read-only database tokens in Vercel where supported. Test Redis data is not a production registry, even though the test schemas match.

Automatic portfolio deployment triggers, showcase reconciliation and scheduled sync remain Stage 4. The resume-preparation workflow produces pending artifacts on PDF changes; those artifacts do not automatically approve new facts or activate knowledge.
