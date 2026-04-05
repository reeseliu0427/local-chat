from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1)
    model: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=512, ge=1, le=4096)
    top_p: float = Field(default=0.95, ge=0.0, le=1.0)


class HealthResponse(BaseModel):
    ok: bool
    app_name: str
    vllm_base_url: str
    vllm_available: bool
    models: list[str]
    detail: str | None = None


class ConfigResponse(BaseModel):
    app_name: str
    default_model: str | None
    available_models: list[str]
    vllm_base_url: str


class ChatResponse(BaseModel):
    content: str
    raw: dict[str, Any]

