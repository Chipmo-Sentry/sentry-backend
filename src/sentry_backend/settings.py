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

    # Auth cookie scoping. To support split-subdomain SPAs (app./admin. talking
    # to api.) set cookie_domain=".sentry.chipmo.mn" and cookie_samesite="none"
    # (None requires HTTPS; `secure` is forced on when samesite=none). Defaults
    # keep same-origin/localhost behavior unchanged.
    cookie_domain: str | None = None
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    clip_storage_dir: str = "./storage/clips"
    max_clip_size_mb: int = 100

    sentry_ai_url: str | None = None  # e.g. http://localhost:8001 — None disables verify
    sentry_ai_timeout_sec: int = 60

    # M1-LIVE L3: simple shared-secret accepted by /api/v1/internal/live-metadata
    # (faster path than full JWT for high-volume per-frame metadata).
    # SECURITY: no hardcoded default — MUST be set via env in BOTH dev and prod
    # (dev .env sets it to match sentry-ai's SENTRY_BACKEND_SERVICE_TOKEN).
    # When None, the endpoint accepts only a valid service JWT.
    live_metadata_shared_secret: str | None = None

    # M1-LIVE L5: MediaMTX recordings directory (accessible from backend host).
    # When set, threshold-breach handler can cut clips directly without an
    # external sentry-ingest control plane. Set to None to disable L5.
    mediamtx_recordings_dir: str | None = None

    # M1.5 AG8: MediaMTX admin API URL for dynamic path config (camera register).
    # Set to None to disable — Camera CRUD still succeeds, just doesn't sync to
    # MediaMTX (an operator can `mediamtx restart` to pick up paths from yml).
    mediamtx_api_url: str | None = "http://127.0.0.1:9997"
    mediamtx_api_token: str | None = None
    # Per-person cooldown after a live-threshold breach fires — debounces
    # repeated alerts from the same suspicious moment.
    live_breach_cooldown_sec: int = 30
    # Per-person sustain time — risk must STAY above threshold for this long
    # before we fire (avoids flicker on one-frame spikes).
    live_breach_sustain_sec: float = 1.0

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
    return Settings()
