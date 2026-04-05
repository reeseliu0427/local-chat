from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .models import ChatRequest, ChatResponse, ConfigResponse, HealthResponse

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        _.state.http_client = client
        yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _vllm_url(path: str) -> str:
    return f"{settings.vllm_base_url.rstrip('/')}/{path.lstrip('/')}"


async def _load_models() -> list[str]:
    client: httpx.AsyncClient = app.state.http_client
    try:
        response = await client.get(_vllm_url("/v1/models"))
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    payload = response.json()
    return [item.get("id", "") for item in payload.get("data", []) if item.get("id")]


def _resolve_model(requested_model: str | None, available_models: list[str]) -> str:
    if requested_model:
        return requested_model
    if settings.vllm_model:
        return settings.vllm_model
    if available_models:
        return available_models[0]
    raise HTTPException(status_code=503, detail="No vLLM model is currently available.")


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    models = await _load_models()
    return HealthResponse(
        ok=bool(models),
        app_name=settings.app_name,
        vllm_base_url=settings.vllm_base_url,
        vllm_available=bool(models),
        models=models,
        detail=None if models else "vLLM backend is unreachable or has no loaded model.",
    )


@app.get("/api/config", response_model=ConfigResponse)
async def config() -> ConfigResponse:
    models = await _load_models()
    default_model = settings.vllm_model or (models[0] if models else None)
    return ConfigResponse(
        app_name=settings.app_name,
        default_model=default_model,
        available_models=models,
        vllm_base_url=settings.vllm_base_url,
    )


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    client: httpx.AsyncClient = app.state.http_client
    models = await _load_models()
    payload = {
        "model": _resolve_model(request.model, models),
        "messages": [message.model_dump() for message in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "top_p": request.top_p,
        "stream": False,
    }
    try:
        response = await client.post(_vllm_url("/v1/chat/completions"), json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to reach vLLM backend: {exc}") from exc

    raw = response.json()
    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Unexpected vLLM response payload.") from exc
    return ChatResponse(content=content, raw=raw)


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    models = await _load_models()
    payload = {
        "model": _resolve_model(request.model, models),
        "messages": [message.model_dump() for message in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "top_p": request.top_p,
        "stream": True,
    }
    client: httpx.AsyncClient = app.state.http_client

    async def event_stream() -> AsyncIterator[bytes]:
        try:
            async with client.stream(
                "POST",
                _vllm_url("/v1/chat/completions"),
                json=payload,
                headers={"Accept": "text/event-stream"},
                timeout=None,
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    if chunk:
                        yield chunk
        except httpx.HTTPStatusError as exc:
            error = JSONResponse(
                status_code=exc.response.status_code,
                content={"detail": exc.response.text},
            )
            yield error.body
        except httpx.HTTPError as exc:
            error = JSONResponse(
                status_code=502,
                content={"detail": f"Failed to reach vLLM backend: {exc}"},
            )
            yield error.body

    return StreamingResponse(event_stream(), media_type="text/event-stream")


frontend_dist_dir = settings.frontend_dist_dir
if frontend_dist_dir.exists():
    app.mount("/assets", StaticFiles(directory=frontend_dist_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(frontend_dist_dir / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        requested = frontend_dist_dir / full_path
        if full_path and requested.exists() and requested.is_file():
            return FileResponse(requested)
        return FileResponse(frontend_dist_dir / "index.html")

