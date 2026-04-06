from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    vllm_base_url: str = Field(default="http://127.0.0.1:9006")
    vllm_model: str | None = Field(default=None)
    app_name: str = Field(default="Local Chat")
    app_env: str = Field(default="development")
    request_timeout_seconds: float = Field(default=600.0)
    auth_username: str = Field(default="admin")
    auth_password: str = Field(default="change-me")
    auth_session_secret: str = Field(default="local-chat-session-secret")
    auth_cookie_name: str = Field(default="local_chat_session")
    auth_cookie_secure: bool = Field(default=False)
    auth_session_ttl_hours: int = Field(default=72)
    openai_api_key: str = Field(default="local-chat-openai-key")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def frontend_dist_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "frontend" / "dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
