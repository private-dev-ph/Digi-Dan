# Cloud adapter validation

Use these instructions to configure and validate isolated cloud adapters. Production runtime reads use a restricted credential; GitHub Actions retains writer credentials.

## Isolated test resources

Create an isolated Redis database, for example `digi-dan-knowledge-test`, and obtain its HTTPS REST URL and read/write token from the connection details. See [Upstash database creation](https://upstash.com/docs/redis/overall/getstarted) and [REST credentials](https://upstash.com/docs/redis/features/restapi).

Save only these test settings in the ignored `.env.cloud-test` file. Do not commit the file or include tokens in shell commands:

```dotenv
UPSTASH_REDIS_REST_URL=https://YOUR-TEST-DATABASE.upstash.io
UPSTASH_REDIS_REST_TOKEN=YOUR-TEST-DATABASE-REST-TOKEN
KNOWLEDGE_ENVIRONMENT=test
```

The preflight loader combines `.env`, process environment and the explicitly supplied test file in that order; the test file wins. It reads only the recognized cloud settings, disables dotenv interpolation, and never modifies the process environment or live chat mode. Existing Gemini and Vector settings can be reused. A malformed configuration produces a code, not credential values.

The test registry uses `digi-dan:knowledge:test:registry`. Logical coordinator namespaces such as `knowledge-HASH-TICKET` map to `digi-dan-test-knowledge-HASH-TICKET`. Production namespaces use `digi-dan-production-knowledge-`. Existing default Vector data remains separate. Namespace isolation is enforced by application code, not by a new scoped Vector credential; real tests will still consume the existing index's storage and request quotas. The index's dimensions must match the selected embedding profile before upload. This factory currently denies production writes.

## Commands

From the Digi-Dan repository root, run in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_cloud_preflight --env-file .env.cloud-test
```

The default check is entirely offline. `configuration_valid` confirms required settings and formats, not credentials or connectivity. After configuring the test database, run the separate read-only check:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_cloud_preflight --env-file .env.cloud-test --online
```

The online check reads the Redis registry and Vector index information, including dimension compatibility. It neither creates the registry nor writes vectors, requests embeddings, activates a version or deploys code. `read_only_checks_passed` does not verify write-token permissions or Gemini access. Those belong to the subsequent isolated lifecycle exercise. An empty test registry is expected on the first run.

## Adapter behavior

The registry stores the revision, exact JSON state and a retry identifier in one Redis hash. One server-side Lua operation checks the expected revision and conditionally writes all fields. State JSON is not decoded/re-encoded in Lua, preserving empty arrays. Repeating the same write after a lost response uses the same retry identifier. A write whose outcome cannot be established returns `registry_write_uncertain`; it may have succeeded. Re-read and reconcile before issuing another intent. Never assume a transport timeout proves the active pointer is unchanged.

The client carries Upstash's sync token into subsequent requests for read-your-writes behavior. Upstash documents eventual consistency, including the possibility that a different client temporarily reads an earlier value. Atomic conditional writes do not establish globally linearizable reads or a guarantee against all partition/failover anomalies. Retained verified namespaces allow a reader with an earlier pointer to retrieve that version; fail-closed validation still applies. Use isolated concurrent-client and propagation checks to validate deployment behavior; they cannot guarantee behavior under every outage or partition. See [consistency](https://upstash.com/docs/redis/features/consistency) and [read-your-writes](https://upstash.com/docs/redis/howto/readyourwrites).

The Vector adapter validates dimensions, finite nonzero vectors, full record responses and bounded pagination. All operations map to the selected environment. Legacy ingestion also refuses both physical cloud prefixes. Deletion addresses a single managed namespace, never an index-wide reset. The pinned `upstash-vector==0.8.0` client has no constructor timeout parameter, so its underlying HTTP client's timeout is set in the factory to 15 seconds (5 seconds to connect), with SDK retries disabled. This compatibility seam is tested and needs review when upgrading the SDK.

The Gemini profile is `gemini-embedding-001`, normally 768 dimensions, with policy `normalized-v1`. The adapter validates batch counts and vectors and applies L2 normalization. The profile participates in version identity so old and new embedding policies cannot silently mix. It uses a finite SDK timeout.

## Local validation

```powershell
.\.venv\Scripts\python.exe tests/run_stage3_isolated.py
```

The isolated suite runs the adapters with mocked HTTP/SDK responses and the coordinator through all three adapters with simulated services. It covers scoped namespaces, read-only restrictions, configuration errors, retries, malformed responses, version preparation, activation, retrieval and preservation of an active version after a failed successor. The runner blocks and counts outbound socket attempts. Mocked Redis responses do not execute Lua or prove real cloud atomicity.

## Rate-limit handling

The embedding adapter supports bounded retries for Gemini HTTP 429 responses only. Background writers use at most three attempts per batch. They honor numeric Retry-After headers or the installed SDK's nested google.rpc.RetryInfo retry delay, with a 60-second fallback if no usable hint exists. A requested delay above 60 seconds stops the build instead of retrying early. Exhaustion reports embedding_rate_limited; authentication errors, other provider errors and malformed vectors are not retried by this policy.

Read-only/chat construction retains one attempt and does not acquire these background waits. A rate-limited or interrupted build cannot become active. If a separate registry outage prevents recording its failure, the record can remain building and requires reconciliation after confirming the worker has stopped. Do not infer that a worker is running from that status alone.

The regression suite covers the exact SDK error shape, same-batch retry, bounded exhaustion, oversized delay hints and the difference between writer and reader configuration. Google documents rate limits as dependent on the project's usage tier; the [rate-limit guide](https://ai.google.dev/gemini-api/docs/rate-limits) explains where to inspect current limits.

Bulk embedding builds consume the configured Gemini project's quota. Account for visitor requests when scheduling builds. Captured test fixtures do not establish which content the production portfolio currently serves; use [production source verification](production-integration.md#source-readiness) before activation.
