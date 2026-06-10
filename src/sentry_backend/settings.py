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

    # First-run super-admin bootstrap (idempotent). When both are set and no
    # super-admin exists, one is created on startup — seeds a deployment with
    # no shell/DB access. No-ops once the user exists.
    bootstrap_superadmin_email: str | None = None
    bootstrap_superadmin_password: SecretStr | None = None
    bootstrap_org_name: str = "Demo Retail Group"
    bootstrap_org_slug: str = "demo-retail"

    clip_storage_dir: str = "./storage/clips"
    max_clip_size_mb: int = 100

    # Public base URL of the customer app — used to build invite links.
    app_base_url: str = "http://localhost:3000"

    # SMTP for transactional email (org user invites). All optional: when
    # smtp_host is unset, sending is skipped and the invite link is returned in
    # the API response so an admin can share it manually (so invites never block
    # on email infra). For chipmo.mn Bizmail: host=bizmail16.itools.mn, port 465
    # (SSL) or 587 (STARTTLS), user/from = a real mailbox e.g. info@chipmo.mn.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None  # e.g. "Chipmo Sentry <info@chipmo.mn>"
    smtp_ssl: bool = False  # True = implicit SSL (port 465); False = STARTTLS (587)
    invite_ttl_hours: int = 168  # 7 days

    sentry_ai_url: str | None = None  # e.g. http://localhost:8001 — None disables verify
    # Generous: the AI node's /v1/cut-verify runs a full VLM inference, which on
    # a GPU shared with the live YOLO workers measures ~100-125s. A tighter
    # timeout made the backend abandon the call before the verdict came back, so
    # the breach produced no alert/clip. (Lower this once the VLM is sped up.)
    sentry_ai_timeout_sec: int = 180
    # Shared secret sent as `Authorization: Bearer` to sentry-ai's /v1/* routes.
    # None/empty → no header (sentry-ai must then be unauthenticated/LAN-only).
    # Set the SAME value here and as sentry-ai's AI_SERVICE_TOKEN in prod.
    sentry_ai_service_token: str | None = None

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
    # Cloud MediaMTX protects its control API with an internal user (Basic
    # auth). Set these to the MTX_API_USER / MTX_API_PASS from the ingest host.
    mediamtx_api_user: str | None = None
    mediamtx_api_pass: str | None = None
    # RTSP base of MediaMTX itself — sentry-ai's live worker pulls frames from
    # here (NOT the raw camera), making MediaMTX the single fan-out point:
    # camera → MediaMTX → {WebRTC to clients, RTSP to sentry-ai}. The live
    # worker's camera_id is the camera's mediamtx_path. Set to None to disable
    # auto-starting the AI live worker on camera register.
    mediamtx_rtsp_url: str | None = "rtsp://127.0.0.1:8554"
    # Cloud topology: when set, store agents PUBLISH their camera streams to
    # this RTSP base (e.g. "rtsp://media.sentry.chipmo.mn:8554") and MediaMTX
    # paths are created in publish mode (no pull `source`, since the cloud
    # can't reach the store LAN). When None, MediaMTX pulls the camera RTSP
    # directly (local laptop / on-LAN edge). Returned to paired agents via
    # GET /api/v1/agent/stream-config.
    agent_stream_push_url: str | None = None
    mediamtx_publish_user: str | None = None
    mediamtx_publish_pass: str | None = None
    # Per-camera read token (WHEP/HLS) TTL + the MediaMTX authHTTP shared secret.
    # stream_token_ttl_sec covers a viewing session (reconnects reuse the URL).
    # mediamtx_auth_secret, when set, must be sent by MediaMTX as the
    # `Authorization: Bearer` header to the authHTTP endpoint (enforce-if-set).
    stream_token_ttl_sec: int = 3600
    mediamtx_auth_secret: str | None = None
    # Per-person cooldown after a live-threshold breach fires — debounces
    # repeated alerts from the same suspicious moment.
    live_breach_cooldown_sec: int = 30
    # Per-person sustain time — risk must STAY above threshold for this long
    # before we fire (avoids flicker on one-frame spikes).
    live_breach_sustain_sec: float = 1.0

    # LD.1: founder notification for new landing leads. When both are set, a
    # new demo request pings this Telegram chat; otherwise leads are only
    # persisted + logged. Best-effort — never blocks the public form submit.
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # BE1: alert notifications. A store's own telegram_chat_id wins; this is the
    # global fallback chat used when a store has none set. Reuses
    # telegram_bot_token. None on both → alert notifications disabled.
    telegram_alert_chat_id: str | None = None

    # LD.2: per-IP rate limit on the public POST /api/v1/leads endpoint.
    lead_rate_limit: str = "5/hour"
    # Per-IP rate limits on credential/pairing brute-force surfaces.
    login_rate_limit: str = "10/minute"
    pair_rate_limit: str = "10/minute"

    # Number of trusted reverse proxies in front of the backend. The rate-limit
    # key takes the X-Forwarded-For entry this many hops from the RIGHT — the IP
    # our own trusted proxy observed and appended, which an external client
    # cannot forge. Railway's edge is a single hop, so 1. Set to 0 when the app
    # is exposed directly with no proxy (then the socket peer is used and XFF is
    # ignored entirely). Raising this above the real proxy count re-opens the
    # forgery hole, so keep it equal to the actual topology. See ratelimit.py.
    trusted_proxy_hops: int = 1

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
