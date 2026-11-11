# Production knowledge synchronization

The CLI and GitHub Actions workflow reconcile the reviewed knowledge candidate with the active production version. Check Actions run history and the durable journal for current operational status; a successful local check alone does not prove production has synchronized.

## Local commands

Keep production credentials in the ignored `.env.cloud-production` file. It must contain the production worker configuration and a separate `UPSTASH_REDIS_REST_READ_ONLY_TOKEN` for runtime reads. Never commit this file. The local state directory holds extracted resume data, the candidate, and the latest command report; it is disposable and should not be committed.

From the repository root, use PowerShell:

```powershell
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode check --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation --trigger local
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode status --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation
```

`check` runs production configuration and read-only cloud checks, prepares the local resume extraction and candidate, validates retrieval probes, and verifies the currently deployed source. It reports `check_ready` and whether a change is needed. It does not request embeddings, build a version, or activate one. The `--tesseract-cmd` option can point to a non-default Tesseract executable when local OCR is needed; otherwise the CLI uses `tesseract` from `PATH`.

When a fresh local state directory has no resume snapshot, the coordinator seeds its local cache from `knowledge/reviewed-resume.json`. That reviewed extraction is tied to the current `resume_content_hash` in source policy and is included to keep OCR output reproducible across platforms. The normal preparation step still downloads and validates the live manifest and PDF on every run; only an exact URL and PDF hash match can reuse the cached text. A changed URL, PDF, or extraction pipeline runs extraction again and remains subject to the existing policy review. The reviewed artifact does not bypass source availability or approve new extraction output.

For local checks that need OCR, use Python 3.11 or newer with `requirements-resume.txt` installed and Tesseract available. An unchanged approved PDF can use the reviewed extraction cache without OCR dependencies.

`status` reads the safe reconciliation journal and active version using runtime read-only credentials. `stale` means no successful sync is recorded within the requested freshness window (3 hours by default); `running_timed_out` means the latest run has remained marked running for more than 30 minutes. These flags are journal observations, not an independent external monitor. If Actions never starts, the journal cannot send a proactive alert.

To run a production sync locally, first complete the hosted check and review the candidate and its source facts. Then enable the explicit guard in the current shell:

```powershell
$env:KNOWLEDGE_SYNC_ENABLED = "true"
.\.venv\Scripts\python.exe -m api.tools.knowledge_sync --mode sync --site-url https://zachcodes.vercel.app --env-file .env.cloud-production --state-dir .knowledge-state/reconciliation --trigger manual
Remove-Item Env:KNOWLEDGE_SYNC_ENABLED
```

The command prepares a verified version, checks that production generation has not moved, then activates and verifies it. An unchanged candidate skips activation. If a run fails after activation was attempted, the report records whether activation is confirmed and sets `activation_uncertain` when its outcome cannot be confirmed. A confirmed activation followed by a later error is reported as a post-activation failure. Rate limits, quota failures, and other errors do not trigger a destructive cleanup. Failed and test namespaces are retained; no automatic namespace deletion is part of reconciliation.

## Review and recovery

The sync stops with `review_required` for changes requiring a human decision, including resume or portfolio overlap, candidate policy changes, and retrieval-probe target removals. It never auto-approves those changes. Review the actual candidate facts and policy against the current source, update the authoritative reviewed source or probe targets as appropriate, then run `check` again. A matching content hash alone is not a review of changed facts or policy.

Existing production fallback behavior remains available through the legacy deployment path. Do not use the legacy upload workflow to replace versioned production knowledge. Rollback is limited to retained verified versions with a current matching receipt; it is not an unrestricted restore of arbitrary historical content.

Keep normal source review and commit practices. To freeze production knowledge synchronization, set `KNOWLEDGE_SYNC_ENABLED=false` in both Digi-Dan and the portfolio repository. This stops future sync dispatches while keeping chat available; a job already running may finish. Do not substitute a blind upload.

Portfolio GitHub content uses a one-hour cache revalidation interval, and knowledge reconciliation is scheduled hourly at minute 7. Cache revalidation is request-driven, not an independent hourly update job. The configured cron does not guarantee hourly delivery. Queue time, provider and platform delays, and portfolio cache refresh affect when changes appear; there is no fixed maximum freshness time.

## GitHub Actions configuration

The `Sync production knowledge` workflow is scheduled hourly at minute 7, can be dispatched for a check or sync, and reacts to pushes to the configured production knowledge, policy, assembly, resume-preparation, and workflow files on `main`. Its concurrency group serializes runs and it does not cancel an active run. The scheduled and push sync paths require the repository variable `KNOWLEDGE_SYNC_ENABLED` to equal `true`; a manual check is allowed while it is false.

For a new installation, set `KNOWLEDGE_SYNC_ENABLED=false` in both Digi-Dan and the portfolio repository initially. Enable it only after the hosted check and deployment configuration review are complete. Digi-Dan's workflow consumes these six repository secrets:

| Digi-Dan secret | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Embeddings for version builds and candidate validation. |
| `UPSTASH_VECTOR_REST_URL` | Production Vector endpoint. |
| `UPSTASH_VECTOR_REST_TOKEN` | Production Vector access. |
| `UPSTASH_REDIS_REST_URL` | Production registry endpoint. |
| `UPSTASH_REDIS_REST_TOKEN` | Worker credential for registry writes. |
| `UPSTASH_REDIS_REST_READ_ONLY_TOKEN` | Separate runtime credential for registry reads and verification. |

The application runtime must keep its separate Redis read-only credential; do not give it the worker credential. Set these Digi-Dan repository variables:

| Variable | Default or purpose |
| --- | --- |
| `KNOWLEDGE_SYNC_ENABLED` | Enabled for scheduled, push-triggered, and sync dispatches; only the exact value `true` enables those paths. |
| `PORTFOLIO_PUBLIC_URL` | `https://zachcodes.vercel.app` when unset. |
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` when unset. |
| `EMBEDDING_DIMENSIONS` | `768` when unset. |

The portfolio workflow `notify-knowledge-sync.yml` dispatches Digi-Dan after successful Production deployments using `DIGIDAN_WORKFLOW_TOKEN`. Keep the relay and Digi-Dan worker guards enabled.

Run the offline suite without network access with:

```powershell
.\.venv\Scripts\python.exe tests\run_stage4_isolated.py
```

This validates synchronization behavior and fails if a test attempts a network connection. It does not validate production credentials or perform a hosted rollout.
