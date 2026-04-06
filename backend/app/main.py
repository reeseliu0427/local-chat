import base64
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response, status
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


def _openai_error_response(
    message: str,
    *,
    status_code: int,
    error_type: str,
    error_code: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": error_code,
            }
        },
    )


def _require_openai_api_key(authorization: str | None = Header(default=None)) -> None:
    expected = settings.openai_api_key
    if not expected:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
        )

    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )


async def _load_models() -> list[str]:
    client: httpx.AsyncClient = app.state.http_client
    try:
        response = await client.get(_vllm_url("/v1/models"))
        response.raise_for_status()
    except httpx.HTTPError:
        return []
    payload = response.json()
    return [item.get("id", "") for item in payload.get("data", []) if item.get("id")]


def _public_model_name() -> str:
    return settings.openai_model_name


def _to_public_model_name(model_name: str, available_models: list[str] | None = None) -> str:
    canonical = settings.vllm_model or ((available_models or [None])[0])
    if canonical and model_name == canonical:
        return _public_model_name()
    return model_name


def _to_canonical_model_name(
    model_name: str | None,
    available_models: list[str] | None = None,
) -> str | None:
    if model_name is None:
        return None
    canonical = settings.vllm_model or ((available_models or [None])[0])
    public = _public_model_name()
    if model_name == public and canonical:
        return canonical
    return model_name


def _resolve_model(requested_model: str | None, available_models: list[str]) -> str:
    requested_model = _to_canonical_model_name(requested_model, available_models)
    if requested_model:
        return requested_model
    if settings.vllm_model:
        return settings.vllm_model
    if available_models:
        return available_models[0]
    raise HTTPException(status_code=503, detail="No vLLM model is currently available.")


def _prepare_openai_payload(payload: dict, available_models: list[str]) -> dict:
    prepared = dict(payload)
    prepared["model"] = _resolve_model(prepared.get("model"), available_models)
    return prepared


def _rewrite_models_payload(payload: dict) -> dict:
    rewritten = dict(payload)
    available_models = [
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    data = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        rewritten_item = dict(item)
        model_id = rewritten_item.get("id")
        if isinstance(model_id, str):
            rewritten_item["id"] = _to_public_model_name(model_id, available_models)
        root = rewritten_item.get("root")
        if isinstance(root, str):
            rewritten_item["root"] = _to_public_model_name(root, available_models)
        data.append(rewritten_item)
    rewritten["data"] = data
    return rewritten


def _rewrite_chat_payload(payload: dict, available_models: list[str] | None = None) -> dict:
    rewritten = dict(payload)
    model_name = rewritten.get("model")
    if isinstance(model_name, str):
        rewritten["model"] = _to_public_model_name(model_name, available_models)
    return rewritten


async def _proxy_openai_request(
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    stream: bool = False,
) -> Response:
    client: httpx.AsyncClient = app.state.http_client

    if stream:
        async def event_stream() -> AsyncIterator[bytes]:
            try:
                async with client.stream(
                    method,
                    _vllm_url(path),
                    json=payload,
                    headers={"Accept": "text/event-stream"},
                    timeout=None,
                ) as upstream_response:
                    upstream_response.raise_for_status()
                    async for chunk in upstream_response.aiter_bytes():
                        if chunk:
                            yield chunk
            except httpx.HTTPStatusError as exc:
                body = await exc.response.aread()
                body_text = body.decode("utf-8", errors="replace")
                try:
                    error_payload = json.loads(body_text)
                except ValueError:
                    error_payload = {
                        "error": {
                            "message": body_text or "Upstream request failed.",
                            "type": "invalid_request_error",
                            "param": None,
                            "code": None,
                        }
                    }
                yield JSONResponse(status_code=exc.response.status_code, content=error_payload).body
            except httpx.HTTPError as exc:
                yield _openai_error_response(
                    f"Failed to reach vLLM backend: {exc}",
                    status_code=502,
                    error_type="api_connection_error",
                ).body

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    try:
        upstream_response = await client.request(method, _vllm_url(path), json=payload)
        upstream_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            error_payload = exc.response.json()
        except ValueError:
            error_payload = {
                "error": {
                    "message": exc.response.text or "Upstream request failed.",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": None,
                }
            }
        return JSONResponse(status_code=exc.response.status_code, content=error_payload)
    except httpx.HTTPError as exc:
        return _openai_error_response(
            f"Failed to reach vLLM backend: {exc}",
            status_code=502,
            error_type="api_connection_error",
        )

    if upstream_response.headers.get("content-type", "").startswith("application/json"):
        try:
            available_models = await _load_models()
            rewritten_payload = _rewrite_chat_payload(upstream_response.json(), available_models)
        except ValueError:
            rewritten_payload = None
        if rewritten_payload is not None:
            return JSONResponse(status_code=upstream_response.status_code, content=rewritten_payload)

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type", "application/json"),
    )


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


@app.get("/v1/models")
async def openai_models(authorization: str | None = Header(default=None)) -> Response:
    try:
        _require_openai_api_key(authorization)
    except HTTPException as exc:
        return _openai_error_response(
            str(exc.detail),
            status_code=exc.status_code,
            error_type="invalid_api_key",
            error_code="invalid_api_key",
        )
    proxied = await _proxy_openai_request("/v1/models")
    if not isinstance(proxied, Response) or proxied.status_code >= 400:
        return proxied
    try:
        raw_payload = json.loads(proxied.body.decode("utf-8"))
    except ValueError:
        return proxied
    return JSONResponse(status_code=proxied.status_code, content=_rewrite_models_payload(raw_payload))


@app.post("/v1/chat/completions")
async def openai_chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    try:
        _require_openai_api_key(authorization)
    except HTTPException as exc:
        return _openai_error_response(
            str(exc.detail),
            status_code=exc.status_code,
            error_type="invalid_api_key",
            error_code="invalid_api_key",
        )

    try:
        payload = await request.json()
    except ValueError:
        return _openai_error_response(
            "Request body must be valid JSON.",
            status_code=400,
            error_type="invalid_request_error",
        )

    if not isinstance(payload, dict):
        return _openai_error_response(
            "Request body must be a JSON object.",
            status_code=400,
            error_type="invalid_request_error",
        )

    available_models = await _load_models()
    try:
        prepared_payload = _prepare_openai_payload(payload, available_models)
    except HTTPException as exc:
        return _openai_error_response(
            str(exc.detail),
            status_code=exc.status_code,
            error_type="invalid_request_error",
        )

    return await _proxy_openai_request(
        "/v1/chat/completions",
        method="POST",
        payload=prepared_payload,
        stream=bool(prepared_payload.get("stream")),
    )


# Claude Code 客户端调用的接口：/v1/messages
@app.post("/v1/messages")
async def anthropic_messages(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    Claude 格式 → 自动转换为 OpenAI 格式
    完美适配 Claude Code / Claude 客户端
    """
    try:
        # 复用你现有的 API Key 校验
        _require_openai_api_key(authorization)
    except HTTPException as exc:
        return _openai_error_response(
            str(exc.detail),
            status_code=exc.status_code,
            error_type="authentication_error",
        )

    # 1. 获取 Claude 客户端的请求体
    try:
        claude_payload = await request.json()
    except ValueError:
        return _openai_error_response(
            "Request body must be valid JSON.",
            status_code=400,
            error_type="invalid_request_error",
        )

    # 2. 【核心】Claude格式 → 转换为 OpenAI格式
    openai_payload = {
        "model": claude_payload.get("model", "gemma-4-E4B"),
        "messages": claude_payload.get("messages", []),
        "stream": claude_payload.get("stream", False),
        # 兼容 Claude 的参数映射
        "max_tokens": claude_payload.get("max_tokens", 4096),
        "temperature": claude_payload.get("temperature", 0.7),
    }

    # 3. 复用你现有的载荷处理逻辑
    available_models = await _load_models()
    try:
        prepared_payload = _prepare_openai_payload(openai_payload, available_models)
    except HTTPException as exc:
        return _openai_error_response(
            str(exc.detail),
            status_code=exc.status_code,
            error_type="invalid_request_error",
        )

    # 4. 转发到你现有的 OpenAI 接口
    return await _proxy_openai_request(
        "/v1/chat/completions",
        method="POST",
        payload=prepared_payload,
        stream=bool(prepared_payload.get("stream")),
    )


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
