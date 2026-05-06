# Digi-Dan

FastAPI bot for Vercel serverless with Gemini, DeepSeek, Gemini embeddings, and Upstash Vector.

## Endpoints

- `GET /api/health`
- `POST /api/chat`
- `POST /api/ingest` with `Authorization: Bearer $BOT_ADMIN_TOKEN`

API routes accept browser requests only from `yashashm.dev` and subdomains by default. For local API testing, send `X-API-Secret: $API_BYPASS_SECRET`.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app:app --reload
```

## Chat request

```json
{
  "message": "What do you know?",
  "provider": "gemini",
  "top_k": 4
}
```

Use `"provider": "deepseek"` to route generation through DeepSeek. Retrieval and ingestion always use `gemini-embedding-001`.
