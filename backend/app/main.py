import base64
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .models import (
    ChatRequest,
    ChatResponse,
    ConfigResponse,
    HealthResponse,
    LoginRequest,
    SessionResponse,
)

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


def _session_signature(username: str, expires_at: int) -> str:
    payload = f"{username}:{expires_at}".encode("utf-8")
    secret = settings.auth_session_secret.encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _create_session_token(username: str) -> str:
    expires_at = int(time.time()) + settings.auth_session_ttl_hours * 3600
    raw = f"{username}:{expires_at}:{_session_signature(username, expires_at)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def _decode_session_token(session_token: str | None) -> str | None:
    if not session_token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(session_token.encode("utf-8")).decode("utf-8")
        username, expires_at_raw, signature = decoded.split(":", 2)
        expires_at = int(expires_at_raw)
    except (ValueError, UnicodeDecodeError):
        return None

    if expires_at < int(time.time()):
        return None

    expected_signature = _session_signature(username, expires_at)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    return username


def _set_session_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=_create_session_token(username),
        max_age=settings.auth_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.auth_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.auth_cookie_secure,
        path="/",
    )


def _require_auth(
    session_token: str | None = Cookie(default=None, alias=settings.auth_cookie_name),
) -> str:
    username = _decode_session_token(session_token)
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    return username


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


@app.get("/api/auth/session", response_model=SessionResponse)
async def auth_session(
    session_token: str | None = Cookie(default=None, alias=settings.auth_cookie_name),
) -> SessionResponse:
    username = _decode_session_token(session_token)
    return SessionResponse(authenticated=bool(username), username=username)


@app.post("/api/auth/login", response_model=SessionResponse)
async def auth_login(payload: LoginRequest, response: Response) -> SessionResponse:
    valid_username = hmac.compare_digest(payload.username, settings.auth_username)
    valid_password = hmac.compare_digest(payload.password, settings.auth_password)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )
    _set_session_cookie(response, payload.username)
    return SessionResponse(authenticated=True, username=payload.username)


@app.post("/api/auth/logout", response_model=SessionResponse)
async def auth_logout(response: Response) -> SessionResponse:
    _clear_session_cookie(response)
    return SessionResponse(authenticated=False, username=None)


@app.get("/api/health", response_model=HealthResponse, dependencies=[Depends(_require_auth)])
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


@app.get("/api/config", response_model=ConfigResponse, dependencies=[Depends(_require_auth)])
async def config() -> ConfigResponse:
    models = await _load_models()
    default_model = settings.vllm_model or (models[0] if models else None)
    return ConfigResponse(
        app_name=settings.app_name,
        default_model=default_model,
        available_models=models,
        vllm_base_url=settings.vllm_base_url,
    )


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(_require_auth)])
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


@app.post("/api/chat/stream", dependencies=[Depends(_require_auth)])
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

    def _index_response() -> FileResponse:
        return FileResponse(
            frontend_dist_dir / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return _index_response()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str) -> FileResponse:
        requested = frontend_dist_dir / full_path
        if full_path and requested.exists() and requested.is_file():
            return FileResponse(requested)
        return _index_response()
