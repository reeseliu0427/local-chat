# Local Chat

Local chat UI for a local vLLM backend.

## Stack

- Backend: FastAPI
- Frontend: Vue 3 + Vite
- Target model endpoint: local vLLM OpenAI-compatible API

## Project layout

```text
backend/   FastAPI app
frontend/  Vue app
```

## Backend setup

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Optional environment variables:

```bash
export VLLM_BASE_URL=http://127.0.0.1:9006
export VLLM_MODEL='/media/leis/研三数据盘/models/gemma-4-E4B-it/models/google/gemma-4-E4B-it'
export AUTH_USERNAME=admin
export AUTH_PASSWORD='replace-with-a-strong-password'
export AUTH_SESSION_SECRET='replace-with-a-random-secret'
export AUTH_COOKIE_SECURE=false
export OPENAI_API_KEY='replace-with-a-long-random-token'
```

## Frontend setup

```bash
cd frontend
npm install
npm run dev
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`.

## Production-ish flow

Build the frontend:

```bash
cd frontend
npm install
npm run build
```

Then run the backend. If `frontend/dist` exists, FastAPI also serves the compiled SPA.

## Notes

- The backend proxies chat requests to the vLLM server instead of exposing the model endpoint directly to the browser.
- Streaming is enabled through `/api/chat/stream`.
- OpenAI-compatible endpoints are available at `/v1/models` and `/v1/chat/completions` with `Authorization: Bearer $OPENAI_API_KEY`.
- The default UI assumes the vLLM backend is already running on port `9006`.
- The chat UI supports a single uploaded image per prompt for multimodal models such as Gemma 4.
- Login uses a simple FastAPI-issued HttpOnly cookie. Change the default auth values before exposing the app publicly.

## Agent Access

Point OpenAI-compatible clients at this backend instead of the raw vLLM port:

```bash
export OPENAI_BASE_URL=https://ai.leisgroup.online/v1
export OPENAI_API_KEY='replace-with-your-token'
```

Example:

```bash
curl https://ai.leisgroup.online/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma-4-E4B",
    "messages": [{"role": "user", "content": "Say only: hello"}],
    "temperature": 0,
    "max_tokens": 16
  }'
```
