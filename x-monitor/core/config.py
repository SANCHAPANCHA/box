from __future__ import annotations

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: SecretStr
    # Comma-separated list of Telegram user IDs that may use the bot.
    telegram_allowed_user_ids: str = ""
    # Optional self-hosted Bot API server (https://github.com/tdlib/telegram-bot-api)
    telegram_api_base_url: str | None = None

    # X API
    x_bearer_token: SecretStr
    x_api_base_url: str = "https://api.x.com/2"
    # X renamed request params (tweet.fields -> post.fields); "auto" detects which one the API accepts.
    x_api_param_dialect: Literal["auto", "legacy", "v2"] = "auto"
    x_request_timeout_seconds: float = 20.0
    x_max_retries: int = 3
    x_max_pages_per_poll: int = Field(default=3, ge=1, le=10)
    # Expand referenced posts/authors (original of a repost, quoted post, replied-to post).
    # Gives richer notifications; X may bill the included posts as extra reads.
    x_fetch_references: bool = True

    # Storage
    database_url: str = "postgresql+asyncpg://xmonitor:xmonitor@localhost:5432/xmonitor"
    redis_url: str | None = None

    # Monitoring
    default_poll_interval_seconds: int = Field(default=60, ge=15)
    min_poll_interval_seconds: int = Field(default=15, ge=5)
    monitor_tick_seconds: int = Field(default=5, ge=1)
    monitor_concurrency: int = Field(default=5, ge=1, le=50)
    dispatcher_tick_seconds: int = Field(default=2, ge=1)
    error_notify_threshold: int = Field(default=3, ge=1)
    default_timezone: str = "UTC"

    # Service
    log_level: str = "INFO"
    log_json: bool = False
    api_enabled: bool = True
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    @field_validator("default_timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _validate_ids(cls, value: str) -> str:
        for part in value.replace(";", ",").split(","):
            part = part.strip()
            if part and not part.lstrip("-").isdigit():
                raise ValueError(f"Invalid Telegram user id: {part!r}")
        return value

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(
            int(p.strip()) for p in self.telegram_allowed_user_ids.replace(";", ",").split(",") if p.strip()
        )

    def secret_values(self) -> list[str]:
        return [s for s in (self.telegram_bot_token.get_secret_value(), self.x_bearer_token.get_secret_value()) if s]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
