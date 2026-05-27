"""Application settings loaded from environment via pydantic-settings."""
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["dev", "staging", "production"] = "dev"
    debug: bool = False

    host: str = "0.0.0.0"  # noqa: S104
    port: int = 8000

    database_url: PostgresDsn

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 7

    service_token_secret: SecretStr = Field(min_length=32)
    service_token_ttl_days: int = 365

    rtsp_fernet_key: SecretStr

    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    clip_storage_dir: str = "./storage/clips"
    max_clip_size_mb: int = 100

    sentry_ai_url: str | None = None  # e.g. http://localhost:8001 — None disables verify
    sentry_ai_timeout_sec: int = 60

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, v: Any) -> Any:
        """Accept comma-separated string OR list; output list."""
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
