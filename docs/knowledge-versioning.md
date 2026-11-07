# Stage 3: versioned knowledge, local phase

> Historical milestone notes: production adapters, deployed-source validation and controlled rollout commands are now implemented. Use [Production integration and rollout](production-integration.md) for current configuration and operating instructions. Original milestone status statements below describe their dates.

This implementation builds, verifies, activates and rolls back knowledge through injected storage and embedding interfaces. The isolated local adapters persist to SQLite and use deterministic token-hash embeddings. They do not load cloud credentials or contact external services.

The current production path remains in `legacy` mode by default. Do not enable `KNOWLEDGE_MODE=versioned` on Vercel yet: cloud adapters are implemented and covered by mocked tests; deployed-source verification and live cloud integration checks remain pending. See [cloud preparation](knowledge-cloud-tests.md). A versioned app without an injected knowledge service returns a controlled 503; it never silently chooses the legacy namespace.

## Lifecycle

`KnowledgeService` in `api/knowledge/versioning.py` validates a Stage 2 candidate, its chunk hashes and embedding profile before requesting a build. Each accepted intent advances a registry generation using compare-and-swap. Each changed build gets an isolated namespace beginning with `knowledge-`, and uploads bounded batches without changing the active version.

Verification checks finite vectors and dimensions, bounded indexing readiness, the complete stored ID set and record hashes, and representative retrieval questions. A required source-verifier callback checks the selected source before building and again before activation. The local CLI compares the explicitly supplied candidate receipt; this does not establish that Vercel deployed its content. Production must use a verifier that checks the desired production export and PDF digest.

Only a verified build that still matches the desired intent can become active. A late job cannot reclaim that intent. A source moving during preparation or any failed build leaves the current pointer unchanged. A matching active version can be reused without re-embedding its documents. Version identity includes candidate content, the embedding provider/model/dimensions/policy, and the pipeline policy version.

Registry generations represent accepted operator/synchronization intent, not Git commit time. Stage 4 must register production events in the intended order and recheck source freshness; the local state machine alone does not determine which Git commit is deployed.

## Active retrieval and old ingestion

Chat resolves the active record once per request and uses its recorded namespace and embedding profile throughout retrieval. It returns a `knowledge_version` identifier. A request already using version A can finish there while later requests use version B. Retrieved record hashes are checked against the captured version's receipts.

Public chat requests cannot choose a namespace. The legacy ingestion function rejects writes to every `knowledge-` namespace, and rejects all legacy ingestion when versioned mode is enabled. The legacy JSON loader rejects Stage 2 candidates and resume extraction artifacts. The existing upload workflow also skips its job when the repository variable `KNOWLEDGE_MODE` is `versioned`.

These are application-level protections, not new cloud credential permissions. Production integration must restrict write credentials and coordinate the mode switch in Vercel and GitHub. The existing default remains `legacy`; no deployed settings were changed.

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

The SQLite registry uses transactions and revision checks across separate connections, so tests exercise real persistence and atomic updates rather than only an in-memory dictionary. SQLite is a local adapter here, not the proposed shared store for Vercel.

## Isolated tests

```powershell
python tests/run_stage3_isolated.py
```

The runner discovers `test_stage3*.py` and blocks outbound socket connections, counting attempts even if an application catches the exception. Python 3.10 on Windows uses an internal loopback socket pair to wake its event loop; only that standard-library operation is exempted. TestClient HTTP requests execute through its in-process transport. No Upstash or Gemini access is required.

Coverage includes SQLite restart durability and concurrent compare-and-swap, stage/activation separation, invalid candidates and vectors, upload/indexing/retrieval failures, source changes, stale activation, unchanged builds, rollback, cleanup protection, fixed-version retrieval and legacy write guards. Earlier Stage 1/2 tests remain part of regression verification.

## Cloud integration still required

The shared registry, Upstash Vector and Gemini adapters are now implemented with mocked coverage. The next milestone is to configure and validate them against actual cloud services; supply production source verification; and test an isolated cloud namespace. The application factory already accepts a knowledge service, and the coordinator uses the same interfaces for these adapters. No local database or local-test embedding profile should be used as a production substitute.

That milestone must also verify actual index dimensions and namespace configuration, shared atomic activation semantics, production failure handling, credential scopes and the legacy uploader cutover. A manual production activation follows successful integration and deployment verification. Automatic deployment events and scheduled reconciliation remain Stage 4.
