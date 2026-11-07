# Resume PDF preparation

This prerequisite to Stage 2 adds a resume source to Digi-Dan. It downloads and checks the PDF on each run, extracts readable text (or uses local OCR), and preserves the last valid extraction on failure. It does not change active chat retrieval, generate embeddings, or upload to Upstash.

## Inputs and ownership

The portfolio knowledge manifest now includes an additive `attachments` array. The resume entry has `id: "resume"`, a title, an absolute URL and `media_type: "application/pdf"`. The website and this descriptor refer to the same configured resume file. The portfolio's existing semantic `content_hash` still describes its exported documents; it does not identify PDF bytes.

Always run the resume check even if that portfolio hash is unchanged. A PDF can be replaced at the same URL without a profile or project text change. The importer downloads the current bytes and computes their SHA-256. Identical bytes, source URL and pipeline policy can reuse a validated extraction. Different bytes trigger processing. A second hash over normalized page text distinguishes content changes from PDF metadata/formatting changes.

Extracted pages are source evidence, not approved claims. Snapshots have `kind: "resume-extraction"` and `review_status: "pending"`, with no ingestion `documents` field. Stage 2 must resolve overlapping facts, preserve portfolio-owned corrections and decide which additional facts are approved. OCR confidence is a recognition-quality signal, not proof that a statement is true. Source content is data and must never become system instructions.

## Dependencies

Use a Python environment with `python -m pip install -r requirements-resume.txt`. These optional pinned dependencies are separate from the chat API's runtime requirements. OCR additionally needs Tesseract 5 with English language data. Use the executable on PATH or pass `--tesseract-command` explicitly. No PDF is sent to a cloud OCR or generation provider.

Tesseract's [installation instructions](https://tesseract-ocr.github.io/tessdoc/Installation.html) link to the maintained Windows installer. Its [command-line documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage) describes TSV output with word confidence. PDF rendering uses [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html). [pypdf](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md) extracts existing text layers but does not perform OCR.

## Commands

From the Digi-Dan repository, prepare a deployed portfolio resume:

```powershell
python -m api.tools.prepare_resume --manifest-url https://YOUR-PORTFOLIO/api/knowledge --state-dir .resume-state
```

For the local preview, explicitly allow loopback HTTP. Use the exact origin declared by the manifest; this checkout currently declares localhost:3001:

```powershell
python -m api.tools.prepare_resume --manifest-url http://localhost:3001/api/knowledge --allow-local-http --state-dir .resume-state --tesseract-command C:\path\to\tesseract.exe
```

For a local PDF without running the website:

```powershell
python -m api.tools.prepare_resume --pdf ..\portfolio\public\cv\resume.pdf --source-url https://YOUR-PORTFOLIO/cv/resume.pdf --state-dir .resume-state
```

Use the actual canonical public origin. These are examples, not deployment settings. The source URL is recorded as provenance in local-file mode; that mode does not prove those bytes are deployed.

## Validation and failure handling

The configured manifest, its declared site and the attachment must share an origin. Production sources require HTTPS, credentials and URL queries/fragments are rejected, redirects are not followed, and only complete successful source responses are accepted. Missing resume attachments or HTTP failures are failed updates, not deletion requests. An intentionally removed resume requires an explicit source-policy change during later synchronization work.

The worker bounds download sizes, PDF bytes (5 MiB), page count (10), text volume and execution time. It rejects encrypted/corrupt PDFs. Pages with insufficient text, excessive isolated letters or replacement characters use local OCR; every page must satisfy quality checks. Missing OCR dependencies, low-confidence results, unreadable pages or timeouts fail the entire preparation rather than silently dropping content. The current policy targets English resumes; other languages need a deliberate OCR configuration and policy-version update.

`last-successful.json` holds the latest validated extraction. `status.json` describes the most recent attempt as `changed`, `unchanged` or `failed`. A failure leaves the previous snapshot bytes intact and returns a nonzero command exit code. Do not mistake a retained snapshot for a successful fresh check. Atomic file replacement and an exclusive state-directory lock prevent partial snapshots and concurrent writes. If a process crashes and leaves a lock, verify no preparation worker is running before removing only that lock file.

`.resume-state/` is ignored by Git because extracted resumes can contain personal information. This is local preparation state, not a durable shared production knowledge registry. Later synchronization jobs must persist their state in the chosen durable storage. A fresh ephemeral runner otherwise reprocesses the PDF and has no previous local snapshot to retain.

Bump `PIPELINE_VERSION` when extraction, OCR configuration, normalization or validation rules change. A policy change must not reuse a cache created by the old policy. Dependencies and actual extraction-method provenance are recorded for diagnosis.

## Tests and rollout

Run `python -m unittest discover -s tests -p "test_resume*.py" -v` with the optional Python dependencies installed. Unit tests use synthetic PDFs and mocked downloads/OCR; the current real resume is exercised separately without committing its extracted text as a fixture.

Stage 2 will consume this pending evidence alongside the portfolio and existing curated corpus. Stage 3 will build and activate a verified knowledge version while preserving the current active version on failure. A portfolio GitHub Actions workflow now provides the repository PDF-change trigger described below. Stage 4 will connect production events and scheduled reconciliation to the complete synchronization pipeline. The old upload workflow remains unchanged until that coordinated cutover.


## Validation on the current resume

The existing Canva PDF has a problematic text layer: layout extraction is empty, and plain extraction returns widely letter-spaced text. The quality gate routes this through OCR. The real local import produced one complete page, using Tesseract 5.5.3, with mean word confidence about 91.39. The rendered page and extracted text were compared; section coverage was checked. Confidence is not an accuracy percentage, and OCR can still confuse characters such as AI/Al or bullet symbols.

The extracted source still states a 150% inspection-time reduction. Preserve that as raw evidence in this pending snapshot, but do not activate it as a chatbot fact: the approved portfolio correction is more than 60% (five minutes to under two). Stage 2 must resolve that ownership conflict. The PDF itself has not been modified.

Current extraction policy is version 2, including rejection of letter-spaced text. The development machine has a task-local Tesseract installation; project deployments need their own OCR executable and English language data. The web/serverless portfolio never runs OCR.

## Automatic preparation from portfolio commits

The portfolio repository now contains `.github/workflows/prepare-resume.yml`. After publication to `main`, it runs when `public/cv/resume.pdf` or the workflow itself changes on `main`; it also supports manual dispatch. Publish this repository's processor and tests to Digi-Dan `main` before publishing the portfolio workflow. These changes are currently local; no hosted run has been verified yet.

The job uses Ubuntu 24.04, Python 3.12 and Tesseract English OCR. It checks out the exact portfolio commit and Digi-Dan `main`, installs only `requirements-resume.txt`, runs the resume tests, and invokes the local-PDF command. The source URL identifies the immutable raw GitHub PDF at that commit. Both repository revisions are recorded in `provenance.json`. This establishes repository-source evidence; it does not establish that Vercel has deployed those bytes.

A successful run uploads `last-successful.json`, `status.json` and `provenance.json` as an Actions artifact. Extraction failures fail the job and upload the diagnostic status when available. Each runner starts fresh: there is no cross-run cache or automatic restoration of earlier extraction state. Earlier successful artifacts remain available within their retention period (requested 90 days, subject to repository limits), and are not permanent production state. The workflow requires only read access and no additional secrets. It does not alter the legacy upload workflow or live knowledge collection.

Pushes made using the normal workflow `GITHUB_TOKEN` do not trigger another push workflow. If a future automation replaces the PDF using that token, explicitly dispatch preparation as part of that automation. Per-branch concurrency allows a running job to finish but may coalesce older pending runs; it prioritizes preparing the latest queued revision. Later synchronization stages still need durable state, source ownership and activation rules.

## Stage 2 integration

The [knowledge assembly command](knowledge-assembly.md) now consumes a validated preparation snapshot and its successful status. A changed normalized resume hash requires source-policy review before new facts enter a candidate. Pending extraction remains separate from active chatbot knowledge.
