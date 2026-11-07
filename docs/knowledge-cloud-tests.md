# Stage 3 cloud preparation

> Historical milestone notes: production adapters, deployed-source validation and controlled rollout commands are now implemented. Use [Production integration and rollout](production-integration.md) for current configuration and operating instructions. Original milestone status statements below describe their dates.

Cloud adapters are available for the shared Redis registry, Upstash Vector and Gemini embeddings. The application remains in legacy mode and still requires explicit service injection for versioned chat. This is preparation for isolated cloud tests, not a production cutover. The repository's existing Python environment and pinned dependencies are sufficient.

## Test resources

Use the existing Upstash account. Create a separate Redis database named `digi-dan-knowledge-test` within that account. The console's Redis tab provides **+ Create Database**, a name/region form and plan selection. Review the offered pricing before creation. In its connection details copy the HTTPS REST URL and read/write **Token**. A new account is unnecessary. See [Upstash database creation](https://upstash.com/docs/redis/overall/getstarted) and [REST credentials](https://upstash.com/docs/redis/features/restapi).

Save only these test settings in the ignored `.env.cloud-test` file. Do not paste tokens into chat, commit the file or put tokens into shell commands:

```dotenv
UPSTASH_REDIS_REST_URL=https://YOUR-TEST-DATABASE.upstash.io
UPSTASH_REDIS_REST_TOKEN=YOUR-TEST-DATABASE-REST-TOKEN
KNOWLEDGE_ENVIRONMENT=test
```

The preflight loader combines `.env`, process environment and the explicitly supplied test file in that order; the test file wins. It reads only the recognized cloud settings, disables dotenv interpolation, and never modifies the process environment or live chat mode. Existing Gemini and Vector settings can be reused. A malformed configuration produces a code, not credential values. Extra unrelated prose in the existing `.env` is ignored where dotenv can parse the recognized settings; this setup does not rewrite that file.

The test registry uses `digi-dan:knowledge:test:registry`. Logical coordinator namespaces such as `knowledge-HASH-TICKET` map to `digi-dan-test-knowledge-HASH-TICKET`. Production namespaces would use `digi-dan-production-knowledge-`. Existing default Vector data remains separate. Namespace isolation is enforced by application code, not by a new scoped Vector credential; real tests will still consume the existing index's storage and request quotas. The index's dimensions must match the selected embedding profile before upload. This factory currently denies production writes.

## Commands

Run from PowerShell:

```powershell
Set-Location 'C:\Users\USER\Documents\GitHub\Digi-Dan'
.\.venv\Scripts\python.exe -m api.tools.knowledge_cloud_preflight --env-file .env.cloud-test
```

The default check is entirely offline. `configuration_valid` confirms required settings and formats, not credentials or connectivity. After configuring the test database, run the separate read-only check:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_cloud_preflight --env-file .env.cloud-test --online
```

The online check reads the Redis registry and Vector index information, including dimension compatibility. It neither creates the registry nor writes vectors, requests embeddings, activates a version or deploys code. `read_only_checks_passed` does not verify write-token permissions or Gemini access. Those belong to the subsequent isolated lifecycle exercise. An empty test registry is expected on the first run.

## Adapter behavior

The registry stores the revision, exact JSON state and a retry identifier in one Redis hash. One server-side Lua operation checks the expected revision and conditionally writes all fields. State JSON is not decoded/re-encoded in Lua, preserving empty arrays. Repeating the same write after a lost response uses the same retry identifier. A write whose outcome cannot be established returns `registry_write_uncertain`; it may have succeeded. Re-read and reconcile before issuing another intent. Never assume a transport timeout proves the active pointer is unchanged.

The client carries Upstash's sync token into subsequent requests for read-your-writes behavior. Upstash documents eventual consistency, including the possibility that a different client temporarily reads an earlier value. Atomic conditional writes do not establish globally linearizable reads or a guarantee against all partition/failover anomalies. Retained verified namespaces allow a reader with an earlier pointer to retrieve that version; fail-closed validation still applies. Real concurrent-client and propagation checks remain necessary before production. See [consistency](https://upstash.com/docs/redis/features/consistency) and [read-your-writes](https://upstash.com/docs/redis/howto/readyourwrites).

The Vector adapter validates dimensions, finite nonzero vectors, full record responses and bounded pagination. All operations map to the selected environment. Legacy ingestion also refuses both physical cloud prefixes. Deletion addresses a single managed namespace, never an index-wide reset. The pinned `upstash-vector==0.8.0` client has no constructor timeout parameter, so its underlying HTTP client's timeout is set in the factory to 15 seconds (5 seconds to connect), with SDK retries disabled. This compatibility seam is tested and needs review when upgrading the SDK.

The Gemini profile is `gemini-embedding-001`, normally 768 dimensions, with policy `normalized-v1`. The adapter validates batch counts and vectors and applies L2 normalization. The profile participates in version identity so old and new embedding policies cannot silently mix. It uses a finite SDK timeout. No model migration is included in this change.

## Validation and remaining work

```powershell
.\.venv\Scripts\python.exe tests/run_stage3_isolated.py
```

The expanded isolated suite runs the adapters with mocked HTTP/SDK responses and the coordinator through all three adapters with simulated services. It covers scoped namespaces, read-only restrictions, configuration errors, retries, malformed responses, staging/activation/retrieval and preservation of an active version after a failed successor. The runner blocks and counts outbound socket attempts. Mocked Redis responses do not execute Lua or prove real cloud atomicity.

Next, after the test database is configured: read-only connectivity; then actual test-only staging, indexing, semantic retrieval, activation, a failed successor, concurrent writers and rollback. Confirm cloud propagation and source-verification behavior before wiring the production factory into Vercel. Production source verification must compare the deployed portfolio export and resume digest with the candidate. Publishing the portfolio export, production credential configuration, uploader cutover and manual activation remain separate steps. Automatic events and scheduled reconciliation remain Stage 4.

## Rate-limit handling verified during cloud tests

The first live full-candidate builds encountered Gemini HTTP 429 responses after three batches of 32 documents. The embedding adapter now supports bounded retries for that response only. Background writers use at most three attempts per batch. They honor numeric Retry-After headers or the installed SDK's nested google.rpc.RetryInfo retry delay, with a 60-second fallback if no usable hint exists. A requested delay above 60 seconds stops the build instead of retrying early. Exhaustion reports embedding_rate_limited; authentication errors, other provider errors and malformed vectors are not retried by this policy.

Read-only/chat construction retains one attempt and does not acquire these background waits. A rate-limited or interrupted build cannot become active. If a separate registry outage prevents recording its failure, the record can remain building and requires reconciliation after confirming the worker has stopped. This happened in the first test attempt and was explicitly recovered; it is not safe to infer a worker is running from that status alone.

The regression suite covers the exact SDK error shape, same-batch retry, bounded exhaustion, oversized delay hints and the difference between writer and reader configuration. Google documents rate limits as dependent on the project's usage tier; the [rate-limit guide](https://ai.google.dev/gemini-api/docs/rate-limits) explains where to inspect current limits. No quota, billing plan or model was changed for this correction.

## Live isolated validation completed — November 7, 2026

The isolated cloud exercise passed using the approved 139-chunk captured candidate. Real Redis Lua updates, stale-write rejection, two competing clients and a simulated lost response were verified. Real Gemini/Vector staging, all four retrieval probes, activation, unchanged sync without document re-embedding, a temporary project-removal candidate, stale activation rejection, rollback, partial-upload failure preservation and final recovery passed. The original candidate is active in the test registry; incomplete attempts remain inactive and retained for inspection.

Before/after fingerprints confirm the existing 48 legacy records (text, metadata and vectors) and default empty namespace are unchanged. The updated Stage 3 suite also passed 90 isolated tests with zero outbound socket attempts. No production mode, deployment, commit or push was changed.

Testing exposed the 429 behavior described above and transient registry/Vector access failures. One abandoned building record was explicitly reconciled after its worker exited. A failed Vector batch succeeded on replay; the exact original lower-level cause was not captured. The runner safely resumed from the verified active version. These results establish the tested behaviors, not guarantees under every outage or partition.

Production deployed-source verification, runtime wiring, credential setup and uploader cutover are still required. These checks used a captured source receipt and do not prove the production portfolio is serving that content. Bulk builds also consume the configured Gemini project's quota; production scheduling must account for visitor requests. Automatic triggers and scheduled reconciliation remain Stage 4.
