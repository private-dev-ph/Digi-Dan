# Knowledge versioning and local validation

This implementation builds, verifies, activates and rolls back knowledge through injected storage and embedding interfaces. The isolated local adapters persist to SQLite and use deterministic token-hash embeddings. They do not load cloud credentials or contact external services.

Production uses `versioned` mode with read-only runtime knowledge access. Cloud adapters and deployed-source checks are required for this mode. See [cloud adapter operations](knowledge-cloud-tests.md) for cloud checks and adapter behavior. A versioned app without an available knowledge service returns a controlled 503; it never silently chooses the legacy namespace.

## Lifecycle

`KnowledgeService` in `api/knowledge/versioning.py` validates an assembled knowledge candidate, its chunk hashes and embedding profile before requesting a build. Each accepted intent advances a registry generation using compare-and-swap. Each changed build gets an isolated namespace beginning with `knowledge-`, and uploads bounded batches without changing the active version.

Verification checks finite vectors and dimensions, bounded indexing readiness, the complete stored ID set and record hashes, and representative retrieval questions. A required source-verifier callback checks the selected source before building and again before activation. The local CLI compares the explicitly supplied candidate receipt; this does not establish that Vercel deployed its content. Production must use a verifier that checks the desired production export and PDF digest.

Only a verified build that still matches the desired intent can become active. A late job cannot reclaim that intent. A source moving during preparation or any failed build leaves the current pointer unchanged. A matching active version can be reused without re-embedding its documents. Version identity includes candidate content, the embedding provider/model/dimensions/policy, and the pipeline policy version.

For a changed candidate, the coordinator validates every stored record and vector in the current verified active namespace. With an identical embedding profile, unchanged chunk IDs and hashes (covering text and metadata) reuse their vectors. New or changed chunks request embeddings. Both sets are copied into a fresh namespace and pass the same count, hash, retrieval-probe and source checks before activation. Removed chunks are omitted. Failed or inactive builds are never reuse sources; a corrupt compatible active namespace stops the build for investigation.

Registry generations represent accepted operator/synchronization intent, not Git commit time. Production synchronization registers events in the intended order and rechecks source freshness; the local state machine alone does not determine which Git commit is deployed.

## Active retrieval and old ingestion

Unchanged syncs retain all storage and source checks and query the vector store using validated cached probe embeddings. The receipt is stored with the version in the registry, so it survives process and runner restarts. It is bound to the full probe definitions and embedding/version identity. Cache misses require fresh successful verification; corrupt caches and failed retrieval stop the run. Changed builds and activation still verify with fresh embeddings. See [unchanged-content verification](knowledge-sync.md#unchanged-content-verification) for operation and reporting.

Chat resolves the active record once per request and uses its recorded namespace and embedding profile throughout retrieval. It returns a `knowledge_version` identifier. A request already using version A can finish there while later requests use version B. Retrieved record hashes are checked against the captured version's receipts.

Public chat requests cannot choose a namespace. The legacy ingestion function rejects writes to every `knowledge-` namespace, and rejects all legacy ingestion when versioned mode is enabled. The legacy JSON loader rejects assembled knowledge candidates and resume extraction artifacts. The existing upload workflow also skips its job when the repository variable `KNOWLEDGE_MODE` is `versioned`.

These are application-level protections, not cloud credential permissions. Production runtime reads use restricted credentials; writer credentials remain in GitHub Actions. Production is configured for versioned mode. The legacy uploader skips when `KNOWLEDGE_MODE=versioned`, and `/api/ingest` returns 409.

## Local commands

From the Digi-Dan repository, use the existing Python environment. The lifecycle/store modules and local CLI use only the standard library; HTTP/chat integration tests use the application's existing dependencies.

```powershell
python -m api.tools.knowledge_local stage --candidate .knowledge-state/last-successful.json
python -m api.tools.knowledge_local status
python -m api.tools.knowledge_local activate TICKET --candidate .knowledge-state/last-successful.json
python -m api.tools.knowledge_local query "references available upon request"
```

`stage` builds and verifies only; it does not activate. Its JSON result contains the ticket to use with `activate`. The default database is the ignored `.knowledge-local-state/knowledge.sqlite3`. For an explicit isolated directory, place `--state-dir PATH` before the command.

`knowledge/local-retrieval-probes.json` contains four checks against the current candidate: AOI inspection time, university study period, reference availability and AWS training track completion. These are local test fixtures. If the portfolio intentionally removes or rechunks one of these records, review and update the probes; do not bypass missing-record validation. Real Gemini retrieval quality still needs separate integration checks.

The query command retrieves source text; it does not ask Gemini or DeepSeek to generate an answer. Its token-hash embeddings test lifecycle and retrieval plumbing, not production semantic relevance.

## Rollback and cleanup

```powershell
python -m api.tools.knowledge_local rollback PREVIOUS_TICKET --candidate path/to/previous-candidate.json --expected-generation CURRENT_GENERATION
python -m api.tools.knowledge_local cleanup
```

Use `status` to inspect the generation. Rollback is explicit: it checks the retained target, verifies its records and probes, registers a new intent and activates it. It does not rebuild its document embeddings. The supplied candidate must match the intended rollback target; retain source candidates needed for local exercises.

Cleanup defaults to retaining the active version and two previous versions with a one-hour grace period. It rechecks protection when claiming a namespace for deletion, measures retired versions' grace from retirement, and can retry interrupted deletion. Running builds are not automatically deleted; a crashed build still marked `building` requires operator investigation before recovery or removal. Retained versions require temporary additional storage. Production grace must exceed the maximum supported in-flight retrieval lifetime.

The SQLite registry uses transactions and revision checks across separate connections, so tests exercise real persistence and atomic updates rather than only an in-memory dictionary. SQLite is a local adapter here; production uses the shared Redis registry. Local SQLite and deterministic test embeddings are not production substitutes.

## Local validation

```powershell
python tests/run_stage3_isolated.py
```

The runner discovers `test_stage3*.py` and blocks outbound socket connections, counting attempts even if an application catches the exception. Python 3.10 on Windows uses an internal loopback socket pair to wake its event loop; only that standard-library operation is exempted. TestClient HTTP requests execute through its in-process transport. No Upstash or Gemini access is required.

Coverage includes SQLite restart durability and concurrent compare-and-swap, build/activation separation, invalid candidates and vectors, upload/indexing/retrieval failures, source changes, stale activation, unchanged builds, rollback, cleanup protection, fixed-version retrieval and legacy write guards. Source preparation and assembly tests remain part of regression verification.
