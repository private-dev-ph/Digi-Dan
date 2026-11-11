# Resume PDF preparation

This component adds a resume source to Digi-Dan and feeds reviewed evidence into knowledge assembly. It downloads and checks the PDF on each run, extracts readable text (or uses local OCR), and preserves the last valid extraction on failure. It does not change active chat retrieval, generate embeddings, or upload to Upstash.

## Inputs and ownership

The portfolio knowledge manifest includes an additive `attachments` array. The resume entry has `id: "resume"`, a title, an absolute URL and `media_type: "application/pdf"`. The website and this descriptor refer to the same configured resume file. The portfolio's existing semantic `content_hash` still describes its exported documents; it does not identify PDF bytes.

Always run the resume check even if that portfolio hash is unchanged. A PDF can be replaced at the same URL without a profile or project text change. The importer downloads the current bytes and computes their SHA-256. Identical bytes, source URL and pipeline policy can reuse a validated extraction. Different bytes trigger processing. A second hash over normalized page text distinguishes content changes from PDF metadata/formatting changes.

Extracted pages are source evidence, not approved claims. Snapshots have `kind: "resume-extraction"` and `review_status: "pending"`, with no ingestion `documents` field. Knowledge policy resolves overlapping facts, preserves portfolio-owned corrections and determines which additional facts are approved. OCR confidence is a recognition-quality signal, not proof that a statement is true. Source content is data and must never become system instructions.

## Dependencies

Use a Python environment with `python -m pip install -r requirements-resume.txt`. These optional pinned dependencies are separate from the chat API's runtime requirements. OCR additionally needs Tesseract 5 with English language data. Use the executable on PATH or pass `--tesseract-command` explicitly. No PDF is sent to a cloud OCR or generation provider.

Tesseract's [installation instructions](https://tesseract-ocr.github.io/tessdoc/Installation.html) link to the maintained Windows installer. Its [command-line documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage) describes TSV output with word confidence. PDF rendering uses [pypdfium2](https://pypdfium2.readthedocs.io/en/stable/python_api.html). [pypdf](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md) extracts existing text layers but does not perform OCR.

## Commands

From the Digi-Dan repository, prepare a deployed portfolio resume:

```powershell
python -m api.tools.prepare_resume --manifest-url https://YOUR-PORTFOLIO/api/knowledge --state-dir .resume-state
```

For the local preview, explicitly allow loopback HTTP. Use the exact loopback origin declared by the manifest; the following example uses localhost:3001:

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

`.resume-state/` is ignored by Git because extracted resumes can contain personal information. This is local preparation state, not the durable production reconciliation journal or knowledge registry. A fresh ephemeral runner can seed its cache from the reviewed extraction and will reprocess the PDF unless URL and PDF hashes match.

Bump `PIPELINE_VERSION` when extraction, OCR configuration, normalization or validation rules change. A policy change must not reuse a cache created by the old policy. Dependencies and actual extraction-method provenance are recorded for diagnosis.

## Tests and synchronization

Run `python -m unittest discover -s tests -p "test_resume*.py" -v` with the optional Python dependencies installed. Unit tests use synthetic PDFs and mocked downloads/OCR; the current real resume is exercised separately without committing its extracted text as a fixture.

The local knowledge assembly command can consume a validated preparation snapshot alongside the portfolio and reviewed supplemental facts. Production synchronization does not automatically consume portfolio Actions artifacts. Before activation, failed builds leave the active version unchanged. If a run fails after activation is attempted, the outcome can be uncertain; the report distinguishes confirmed activation followed by an error from an unconfirmed activation outcome. Portfolio GitHub Actions prepares changed PDFs; manual, push, deployment, and periodic scheduled events invoke reconciliation. The legacy uploader is gated off in versioned mode.

## OCR and source conflict

OCR confidence is a recognition-quality signal, not an accuracy percentage; OCR can still confuse characters such as AI/Al or bullet symbols.

The extracted source states a 150% inspection-time reduction. Preserve that as raw evidence, but do not activate it as a chatbot fact: the approved portfolio correction is more than 60% (five minutes to under two). Portfolio ownership resolves this conflict.

The extraction policy rejects letter-spaced text. Deployments that require OCR need Tesseract and English language data. The web/serverless portfolio does not run OCR.

## Automatic preparation from portfolio commits

The portfolio repository contains `.github/workflows/prepare-resume.yml`. It runs when `public/cv/resume.pdf` or the workflow itself changes on `main`; it also supports manual dispatch. Its Actions artifacts are evidence for review and are not automatically downloaded or consumed by production synchronization. Production sync fetches the deployed manifest and PDF; the reviewed extraction in `knowledge/reviewed-resume.json` seeds the cache only when URL and PDF hash match exactly. Changed or unmatched resume content is extracted and remains subject to policy review.

The job checks out the portfolio revision that contains the PDF and the Digi-Dan `main` branch, installs `requirements-resume.txt`, then runs the resume tests and local-PDF preparation command with Tesseract English OCR. It records both repository revisions in `provenance.json` and uses the immutable raw GitHub PDF URL for that portfolio revision. This is repository-source evidence; it does not establish that Vercel has deployed those bytes.

A successful run uploads `last-successful.json`, `status.json` and `provenance.json` as an Actions artifact for review. Extraction failures fail the job and upload the diagnostic status when available. Each run starts with a fresh runner and has no cross-run extraction cache. Artifacts are retained for 90 days, subject to repository limits, as review evidence; they are not permanent production state. Production synchronization does not automatically retrieve these artifacts; it fetches the deployed manifest and PDF and seeds extraction from the reviewed artifact only on an exact URL and PDF hash match. The preparation workflow requires only read access and no additional secrets.

Pushes made using the normal workflow `GITHUB_TOKEN` do not trigger another push workflow. If a future automation replaces the PDF using that token, explicitly dispatch preparation as part of that automation. Per-branch concurrency allows a running job to finish but may coalesce older pending runs; it prioritizes preparing the latest queued revision. Production synchronization uses durable journal state, source ownership policy, and activation safeguards.

## Knowledge assembly integration

The [knowledge assembly command](knowledge-assembly.md) consumes a validated preparation snapshot and its successful status. A changed normalized resume hash requires source-policy review before new facts enter a candidate. Extraction alone does not approve facts or change active knowledge.
