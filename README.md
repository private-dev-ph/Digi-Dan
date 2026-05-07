# Digi-Dan

Digi-Dan is a FastAPI retrieval-augmented generation backend for a portfolio assistant. It stores profile, education, project, skill, and experience facts in Upstash Vector, retrieves the most relevant chunks for a question, and sends the grounded context to either Gemini or DeepSeek for the final answer.

The project is designed to run locally with Uvicorn and deploy as a Python/FastAPI serverless backend on Vercel. It can also be reused as a template for any personal portfolio, resume bot, documentation assistant, or small knowledge-base chatbot.

## Features

- FastAPI backend with Vercel-compatible ASGI entrypoint.
- RAG flow using Gemini embeddings and Upstash Vector.
- Generation provider switch between Gemini and DeepSeek.
- JSON, JSONL, and TXT ingestion helper for vector uploads.
- Browser origin allowlist for portfolio frontend access.
- `X-API-Secret` bypass for local testing and server-to-server calls.
- Bearer-token protected ingest endpoint.
- Per-client in-memory chat rate limiting.
- GitHub Actions workflow that uploads updated RAG data automatically.
- Source citations returned with each answer.

## Architecture

```txt
Portfolio frontend
  -> POST /api/chat
  -> FastAPI app
  -> Gemini embedding model embeds the question
  -> Upstash Vector returns relevant source chunks
  -> Gemini or DeepSeek generates an answer from the retrieved context
  -> API returns answer, provider, and source records
```

Important files:

```txt
app.py                                  Vercel/Uvicorn ASGI entrypoint
api/endpoint/application.py             FastAPI app, middleware, routes
api/core/config.py                      Environment variable loading
api/core/models.py                      Request and response models
api/core/prompts.py                     Default system prompt
api/query/rag.py                        Retrieval and prompt assembly
api/query/providers.py                  Gemini, DeepSeek, and embedding calls
api/tools/vector_store.py               Upstash Vector query/upsert helpers
api/tools/upload_vectorstore.py         CLI uploader for RAG data
daniel-zachary-rag-data.json            Template knowledge base
.github/workflows/upload-rag-data.yml   Automatic vector upload workflow
```

## Endpoints

### `GET /`

Basic root check.

Response:

```json
{
  "name": "Digi-Dan",
  "status": "ok"
}
```

### `GET /api/health`

Health check for local, Vercel, or uptime monitoring.

Response:

```json
{
  "status": "ok"
}
```

Protected by the same origin/bypass middleware as other `/api/*` routes.

### `POST /api/chat`

Ask the model a question.

Request body:

```json
{
  "message": "Where did Daniel get his college education?",
  "provider": "gemini",
  "top_k": 4,
  "namespace": "digi-dan-rag"
}
```

Fields:

```txt
message     Required question.
provider    Optional. "gemini" or "deepseek". Defaults to DEFAULT_PROVIDER.
system      Optional per-request system prompt override.
top_k       Optional number of retrieved chunks. Defaults to RETRIEVAL_TOP_K.
namespace   Optional Upstash namespace. Defaults to UPSTASH_NAMESPACE.
```

Response:

```json
{
  "answer": "Daniel studied at President Ramon Magsaysay State University...",
  "provider": "gemini",
  "sources": [
    {
      "id": "education-college",
      "score": 0.8238814,
      "text": "Daniel's college education...",
      "metadata": {
        "section": "education",
        "topic": "college"
      }
    }
  ]
}
```

### `POST /api/ingest`

Upload documents into Upstash Vector through the API.

Headers:

```txt
Authorization: Bearer BOT_ADMIN_TOKEN
```

Request body:

```json
{
  "namespace": "digi-dan-rag",
  "documents": [
    {
      "id": "education-college",
      "text": "Daniel studied Computer Engineering...",
      "metadata": {
        "section": "education"
      }
    }
  ]
}
```

Response:

```json
{
  "upserted": 1,
  "namespace": "digi-dan-rag"
}
```

## Environment Variables

Copy `.env.example` to `.env` for local development.

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Variables:

```txt
APP_NAME
Display name used by the FastAPI app and root endpoint.

CORS_ORIGINS
Comma-separated full browser origins allowed by CORS.
Example: https://your-portfolio.vercel.app,http://localhost:3000

ALLOWED_ORIGIN_DOMAINS
Comma-separated hostnames allowed by the custom origin middleware.
Do not include protocol.
Example: your-portfolio.vercel.app,yourdomain.com,localhost

API_BYPASS_SECRET
Secret sent as X-API-Secret for terminal tests or server-to-server calls.
Never expose this in browser code.

GEMINI_API_KEY
Required for embeddings and Gemini generation.

GEMINI_MODEL
Gemini generation model. Default: gemini-2.5-flash.

GEMINI_EMBEDDING_MODEL
Gemini embedding model used for retrieval and ingestion.
Default: gemini-embedding-001.

EMBEDDING_DIMENSIONS
Embedding vector size. Must match your Upstash Vector index dimension.
Default: 768.

DEEPSEEK_API_KEY
Required only when using provider "deepseek".

DEEPSEEK_BASE_URL
DeepSeek API base URL. Default: https://api.deepseek.com.

DEEPSEEK_MODEL
DeepSeek chat model. Default: deepseek-chat.

UPSTASH_VECTOR_REST_URL
Upstash Vector REST URL.

UPSTASH_VECTOR_REST_TOKEN
Upstash Vector REST token.

UPSTASH_NAMESPACE
Default namespace for retrieval and ingestion.
Example: digi-dan-rag.

BOT_ADMIN_TOKEN
Bearer token required by POST /api/ingest.

DEFAULT_PROVIDER
Default generation provider when a chat request omits provider.
Allowed values: gemini, deepseek.

RETRIEVAL_TOP_K
Default number of source chunks to retrieve.

MAX_CONTEXT_CHARS
Maximum retrieved context characters passed into the generation prompt.

CHAT_RATE_LIMIT
Maximum chat requests per client per window. Set 0 to disable.

CHAT_RATE_LIMIT_WINDOW_SECONDS
Rate limit window size in seconds.
```

Keep `.env` as plain `KEY=value` lines only. Do not paste terminal commands into it.

## Local Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
```

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run the API:

```bash
uvicorn app:app --reload --port 8001
```

Test health from PowerShell:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8001/api/health" `
  -Headers @{ "X-API-Secret" = "YOUR_API_BYPASS_SECRET" }
```

Ask a question:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8001/api/chat" `
  -Method Post `
  -ContentType "application/json" `
  -Headers @{ "X-API-Secret" = "YOUR_API_BYPASS_SECRET" } `
  -Body '{"message":"Where did Daniel get his college education?","provider":"gemini","top_k":4,"namespace":"digi-dan-rag"}'
```

## RAG Data Format

The included `daniel-zachary-rag-data.json` uses this structure:

```json
{
  "documents": [
    {
      "id": "education-college",
      "text": "Daniel's college education...",
      "metadata": {
        "section": "education",
        "topic": "college"
      }
    }
  ]
}
```

Guidelines:

- Keep each document focused on one topic.
- Put likely user wording near the beginning of `text`.
- Use stable, readable IDs such as `education-college` or `project-rice-leaf-detection`.
- Use metadata for filtering, debugging, and source organization.
- Add summary chunks for broad questions such as education, projects, or experience.

Upload the RAG file manually:

```bash
python -m api.tools.upload_vectorstore daniel-zachary-rag-data.json --namespace digi-dan-rag
```

Supported upload file types:

```txt
.json   Either {"documents": [...]} or a raw array of documents
.jsonl  One document object per line
.txt    Uploaded as one document
```

## GitHub Actions RAG Upload

The workflow in `.github/workflows/upload-rag-data.yml` runs when RAG data or ingestion code changes on `main`. It validates the JSON and uploads it to Upstash Vector.

Add these GitHub repository secrets:

```txt
GEMINI_API_KEY
UPSTASH_VECTOR_REST_URL
UPSTASH_VECTOR_REST_TOKEN
```

Optional GitHub repository variables:

```txt
UPSTASH_NAMESPACE=digi-dan-rag
GEMINI_EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMENSIONS=768
```

You can also run the workflow manually from the GitHub Actions tab.

## Vercel Deployment

This repo deploys to Vercel as a FastAPI backend. The root `app.py` exposes the ASGI app:

```python
from api.endpoint.application import create_app

app = create_app()
```

Vercel settings:

```txt
Framework Preset: FastAPI
Root Directory: repository root
Build Command: default/empty
Install Command: default/empty
Output Directory: default/empty
```

Add the same production environment variables in Vercel Project Settings. At minimum:

```txt
GEMINI_API_KEY
UPSTASH_VECTOR_REST_URL
UPSTASH_VECTOR_REST_TOKEN
UPSTASH_NAMESPACE
API_BYPASS_SECRET
BOT_ADMIN_TOKEN
DEFAULT_PROVIDER
ALLOWED_ORIGIN_DOMAINS
CORS_ORIGINS
```

Add `DEEPSEEK_API_KEY` only if you want DeepSeek generation.

## Using It From A Portfolio

Recommended pattern: keep secrets on the portfolio server and proxy requests through your portfolio backend.

Example Next.js route:

```ts
export async function POST(req: Request) {
  const body = await req.json();

  const response = await fetch(`${process.env.DIGI_DAN_API_URL}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Secret": process.env.DIGI_DAN_API_SECRET!,
    },
    body: JSON.stringify({
      message: body.message,
      provider: body.provider ?? "gemini",
      top_k: body.top_k ?? 4,
    }),
  });

  return Response.json(await response.json(), { status: response.status });
}
```

Portfolio environment variables:

```txt
DIGI_DAN_API_URL=https://your-digi-dan-backend.vercel.app
DIGI_DAN_API_SECRET=your API_BYPASS_SECRET
```

Browser code calls your portfolio route:

```ts
const response = await fetch("/api/digi-dan", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ message }),
});

const data = await response.json();
```

If your portfolio is fully static, you may call `/api/chat` directly from the browser, but only if your domain is configured in `ALLOWED_ORIGIN_DOMAINS` and `CORS_ORIGINS`. Do not send `X-API-Secret` from browser code.

## Using This As A Template

To adapt this project for your own assistant:

1. Fork or copy the repository.
2. Rename `APP_NAME`.
3. Replace `daniel-zachary-rag-data.json` with your own knowledge base.
4. Update `api/core/prompts.py` with your assistant personality and response rules.
5. Create an Upstash Vector index with the same dimension as `EMBEDDING_DIMENSIONS`.
6. Set your local, Vercel, and GitHub Actions environment variables.
7. Run the uploader once to populate the vector database.
8. Connect your frontend to `POST /api/chat`.

Template ideas:

```txt
Portfolio assistant
Resume chatbot
Project documentation bot
Small company FAQ bot
Course or seminar knowledge base
Personal second-brain search assistant
```

## Security Notes

- Do not commit `.env`.
- Do not expose `API_BYPASS_SECRET` or `BOT_ADMIN_TOKEN` in browser JavaScript.
- Keep `/api/ingest` server-only or admin-only.
- Use narrow `ALLOWED_ORIGIN_DOMAINS` values in production.
- Rotate secrets if they are ever pasted into public logs or frontend code.

## Troubleshooting

`Forbidden origin`

The request did not come from an allowed browser origin and did not include a matching `X-API-Secret`. For terminal tests, send `X-API-Secret`. For browser tests, configure `ALLOWED_ORIGIN_DOMAINS` and `CORS_ORIGINS`.

`GEMINI_API_KEY is not configured`

The app needs Gemini for embeddings even when generation uses DeepSeek.

Unexpected or missing answers

Check the returned `sources`. If the right source is missing, improve the relevant RAG chunk wording, add a summary chunk, increase `top_k`, and re-upload the RAG data.

Wrong namespace

Set `UPSTASH_NAMESPACE` or pass `"namespace"` in the chat request.

Vercel unmatched function pattern

Use the FastAPI preset with the root `app.py` entrypoint. Do not add a stale `functions` pattern for `app.py` or `api/index.py`.
