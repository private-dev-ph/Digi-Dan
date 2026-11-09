# Production knowledge synchronization

Stage 4 provides a CLI and a scheduled GitHub Actions workflow for reconciling the reviewed knowledge candidate with the active production version. The workflow is present, but Stage 4 has **not yet been enabled or activated in the hosted environment**. A successful local check does not mean production has been synchronized.

## Local commands

Keep production credentials in the ignored `.env.cloud-production` file. It must contain the production worker configuration and a separate `UPSTASH_REDIS_REST_READ_ONLY_TOKEN` for runtime reads. Never commit this file. The local state directory holds extracted resume data, the candidate, and the latest command report; it is disposable and should not be committed.

From the repository root, use PowerShell:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode check --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation --trigger local
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode status --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation
```

`check` runs production configuration and read-only cloud checks, prepares the local resume extraction and candidate, validates retrieval probes, and verifies the currently deployed source. It reports `check_ready` and whether a change is needed. It does not request embeddings, stage a version, or activate one. The `--tesseract-cmd` option can point to a non-default Tesseract executable when local OCR is needed; otherwise the CLI uses `tesseract` from `PATH`.

`status` reads the safe reconciliation journal and active version using runtime read-only credentials. `stale` means no successful sync is recorded within the requested freshness window (3 hours by default); `running_timed_out` means the latest run has remained marked running for more than 30 minutes. These flags are journal observations, not an independent external monitor. If Actions never starts, the journal cannot send a proactive alert.

To run a production sync locally, first complete the hosted check and review the candidate and its source facts. Then enable the explicit guard in the current shell:

```powershell
$env:KNOWLEDGE_SYNC_ENABLED = "true"
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode sync --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation --trigger manual
Remove-Item Env:KNOWLEDGE_SYNC_ENABLED
```

The command stages a verified version, checks that production generation has not moved, then activates and verifies it. An unchanged candidate skips activation. If a run fails after activation was attempted, the report records whether activation is confirmed and sets `activation_uncertain` when its outcome cannot be confirmed. A confirmed activation followed by a later error is reported as a post-activation failure. Rate limits, quota failures, and other errors do not trigger a destructive cleanup. Failed and test namespaces are retained; no automatic namespace deletion is part of reconciliation.

## Review and recovery

The sync stops with `review_required` for changes requiring a human decision, including resume or portfolio overlap, candidate policy changes, and retrieval-probe target removals. It never auto-approves those changes. Review the actual candidate facts and policy against the current source, update the authoritative reviewed source or probe targets as appropriate, then run `check` again. A matching content hash alone is not a review of changed facts or policy.

Existing production fallback behavior remains available through the legacy deployment path. Do not use the legacy upload workflow to replace versioned production knowledge. Rollback is limited to retained verified versions with a current matching receipt; it is not an unrestricted restore of arbitrary historical content.

Keep normal source review and commit practices. To freeze production knowledge synchronization, set `KNOWLEDGE_SYNC_ENABLED=false` in both Digi-Dan and the portfolio repository. This stops future sync dispatches while keeping chat available; a job already running may finish. Do not substitute a blind upload.

The portfolio source is revalidated hourly, and knowledge reconciliation also runs hourly at minute 17. Updates then depend on workflow queue time, provider and platform delays, and the portfolio cache refresh. There is no fixed maximum time for a source change to appear in chat.

## GitHub Actions configuration

The `Sync production knowledge` workflow runs hourly at minute 17, can be dispatched for a check or sync, and reacts to pushes to the configured production knowledge, policy, assembly, resume-preparation, and workflow files on `main`. Its concurrency group serializes runs and it does not cancel an active run. The scheduled and push sync paths require the repository variable `KNOWLEDGE_SYNC_ENABLED` to equal `true`; a manual check is allowed while it is false.

Set `KNOWLEDGE_SYNC_ENABLED=false` in both Digi-Dan and the portfolio repository initially. Enable it only after the hosted check and deployment configuration review are complete. Digi-Dan's workflow consumes these six repository secrets:

| Digi-Dan secret | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Embeddings for staging and candidate validation. |
| `UPSTASH_VECTOR_REST_URL` | Production Vector endpoint. |
| `UPSTASH_VECTOR_REST_TOKEN` | Production Vector access. |
| `UPSTASH_REDIS_REST_URL` | Production registry endpoint. |
| `UPSTASH_REDIS_REST_TOKEN` | Worker credential for registry writes. |
| `UPSTASH_REDIS_REST_READ_ONLY_TOKEN` | Separate runtime credential for registry reads and verification. |

The application runtime must keep its separate Redis read-only credential; do not give it the worker credential. Set these Digi-Dan repository variables:

| Variable | Default or purpose |
| --- | --- |
| `KNOWLEDGE_SYNC_ENABLED` | `false` initially; only the exact value `true` enables scheduled, push-triggered, and sync dispatches. |
| `PORTFOLIO_PUBLIC_URL` | `https://zachcodes.vercel.app` when unset. |
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` when unset. |
| `EMBEDDING_DIMENSIONS` | `768` when unset. |

The portfolio repository's relay needs `DIGIDAN_WORKFLOW_TOKEN` as a GitHub Actions secret. This is the seventh secret across the two repositories. Use a narrowly scoped token that can dispatch the Digi-Dan workflow and has Actions write access to the Digi-Dan repository. Keep the relay and worker guard disabled in both repositories until the hosted check has passed. The job uploads only a short safe report artifact; it does not upload source text or credentials.

Run the offline suite without network access with:

```powershell
.\.venv\Scripts\python.exe tests\run_stage4_isolated.py
```

This validates Stage 4 behavior and fails if a test attempts a network connection. It does not validate production credentials or perform a hosted rollout.
