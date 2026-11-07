# Stage 2: assembling knowledge

Stage 2 builds a local knowledge candidate from the portfolio export, prepared resume evidence and explicitly reviewed supplemental facts. It does not generate embeddings, upload to Upstash, switch the active chatbot, or publish a deployment.

## Source ownership

The portfolio owns profile, skills, public contact information, employment and current project facts. Old copies in `daniel-zachary-rag-data.json` are inventoried but are not imported wholesale. The user chose to omit older projects absent from the current portfolio, retain supported supplemental details, and replace named references with “available upon request.”

`knowledge/source-policy.json` records the decision for every legacy document, its full-document hash, selected supplemental facts and exact supporting quotes. The current policy includes education details, awards, certificates, seminars, leadership tenure, spoken languages and reference availability. Read [the migration inventory](knowledge-migration.md) for the disposition of each source record. The legacy file remains unchanged because the active chatbot still uses its existing ingestion path until Stage 3.

Each supplemental fact has an explicit owner, stable identity, source receipts and optional portfolio dependencies. Missing required portfolio documents suppress linked supplemental facts. A changed required portfolio fact requires review. These guards hash only document identity and text, so changing the site URL from local development to production does not invalidate ownership review. `portfolio_wins` can suppress a supplement when a specific authoritative portfolio document is present.

This is an explicit policy, not semantic conflict detection by an LLM. If a future export adds a new fact domain, review its ownership mapping before declaring corresponding supplements independently authoritative. Do not bypass a guard by merely copying a new hash: compare the underlying facts and revise the retained text and evidence.

## Resume updates

The GitHub Actions preparation job can continue processing changed PDFs automatically. Extraction produces pending evidence. Assembly checks that the latest preparation status succeeded, validates the snapshot and requires its normalized content hash to match the reviewed policy. Raw PDF formatting changes with unchanged extracted text can reuse the review; new or changed extracted content stops assembly with `resume_review_required`.

To review a changed resume, compare the prepared page text with the PDF, check OCR, select or revise supplemental facts and exact evidence quotes, preserve portfolio-owned corrections, then update the reviewed resume hash. Commit those policy changes with the usual code review. The current resume's 150% inspection claim remains in raw evidence but is excluded from the candidate; the corrected portfolio fact prevails. Different OCR versions can produce text differences that also need review.

An intentionally removed fact must be removed from the source that owns it. Do not add a duplicate in supplemental data to work around an absent portfolio fact. Unchanged legacy records remain explicit curated sources until deliberately migrated or removed.

## Running locally

From the Digi-Dan repository, with a valid prepared resume and adjacent successful `status.json`:

```powershell
python -m api.tools.assemble_knowledge `
  --manifest-url http://localhost:3001/api/knowledge `
  --site-url http://localhost:3001 `
  --allow-local-http `
  --resume .resume-state/last-successful.json
```

Use the actual canonical HTTPS origin for a deployed source and omit `--allow-local-http`. The configured origin must match both the manifest endpoint and its declared source. Network reads are bounded and redirects are rejected. You can instead provide a captured export with `--manifest path/to/export.json`; `--site-url` is still required to validate its expected origin. Local files do not establish that the content has deployed to production.

The optional `--resume-status` selects an explicit preparation status file. The default is `status.json` beside the resume snapshot. `--policy` and `--legacy` default to the repository policy and existing corpus. `--output-dir` defaults to the Git-ignored `.knowledge-state` directory. Use a separate directory from resume preparation; the command rejects input/output collisions.

Assembly uses the Python standard library. PDF/OCR dependencies are required for the earlier preparation step, not for combining already prepared evidence.

## Candidate contract and failure behavior

`last-successful.json` has `kind: "knowledge-candidate"`, schema version 1, `complete: true`, `review_status: "policy-validated"`, a semantic `content_hash`, and `chunks`. It intentionally has no top-level `documents` property. It is a Stage 3 input, not a file for the old uploader.

Each chunk includes a stable ID, text, hash, source owner, entity identity, source URL, title, section, parent ID and chunk position. Chunks are at most 1,200 characters, include title context and split deterministically without dropping source facts. Parent identities survive input reordering and renames; editing a long parent can change its internal chunk boundaries. This is a conservative character bound, not a model-specific token count.

The semantic hash covers sorted chunk IDs, text and metadata. Deployment information, PDF byte hashes and OCR engine versions are recorded separately as provenance. Replacing a PDF with equivalent extracted facts does not itself alter chunk content. Source URL changes are still reflected in chunk metadata and may change the candidate hash. The audit contains all legacy dispositions and source receipts for included or suppressed supplemental facts.

`status.json` reports `changed`, `unchanged` or `failed`, with a bounded failure code. A validation failure retains the previous candidate unchanged; its presence does not mean the latest attempt succeeded. An exclusive local lock prevents concurrent writers. Atomic snapshot replacement prevents partial JSON. These files are local preparation state; Stage 3 must provide durable shared state and activation semantics. A stale lock after a crash should only be removed after confirming no assembly process is running.

Important failure codes include `resume_review_required`, `portfolio_review_required`, `legacy_inventory_mismatch`, `source_quote_missing`, `resume_preparation_failed` and `origin_mismatch`. A newly added or edited legacy record requires an explicit inventory update. Invalid or incomplete manifests and mismatched document hashes never produce replacement candidates.

## Verification and next stage

```powershell
python -m unittest discover -s tests -p 'test_knowledge*.py' -v
python -m unittest discover -s tests -p 'test_assemble*.py' -v
```

Run the full test suite with the optional PDF dependencies installed to include preparation tests. Stage 3 will embed only `chunks` (never raw extraction pages or audit records), verify a separate namespace and atomically activate it. Before activation, verify the desired production deployment and that its resume bytes match the recorded PDF digest; a repository-commit extraction alone does not establish that match. The old uploader remains in service until that coordinated cutover; it must not continue writing into the replacement active collection afterward. Stage 4 will connect complete production synchronization and scheduled reconciliation. The existing portfolio workflow currently automates resume preparation only.
