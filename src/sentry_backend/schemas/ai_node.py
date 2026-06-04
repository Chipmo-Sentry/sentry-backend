"""AI node schemas — pairing, heartbeat, config, admin views."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AiNodePairRequest(BaseModel):
    """Posted by the installed sentry-ai server with the 6-digit code."""

    code: str = Field(min_length=4, max_length=8)
    name: str | None = Field(default=None, max_length=255)
    hostname: str | None = Field(default=None, max_length=255)
    public_url: str | None = Field(default=None, max_length=512)
    version: str | None = Field(default=None, max_length=64)
    gpu: str | None = Field(default=None, max_length=255)


class AiNodeConfig(BaseModel):
    """Config the node polls + applies."""

    enabled: bool
    provider: str
    frame_skip: int


class AiNodePairResult(BaseModel):
    ai_node_token: str
    ai_node_id: UUID
    config: AiNodeConfig


class AiNodeHeartbeat(BaseModel):
    """Telemetry the node reports every ~60s."""

    fps_inference: float | None = None
    vram_mb: int | None = None  # legacy alias; prefer vram_used_mb below
    active_cameras: int | None = None
    version: str | None = Field(default=None, max_length=64)
    # Per-dependency health the node probes locally (e.g. {"ollama": true,
    # "ingest": false, "ai": true}). Opaque map so the node can add keys
    # without a schema change; surfaced read-only in superadmin.
    health: dict[str, bool] | None = None
    # Resource load for the observability dashboard (docs/19). GPU fields are
    # None on a CPU-only node.
    cpu_pct: float | None = None
    ram_used_mb: int | None = None
    ram_total_mb: int | None = None
    gpu_pct: int | None = None
    vram_used_mb: int | None = None
    vram_total_mb: int | None = None
    gpu_temp_c: int | None = None


class AiNodePairingCodePublic(BaseModel):
    code: str
    expires_at: datetime


class AiNodePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str | None
    hostname: str | None
    public_url: str | None
    version: str | None
    gpu: str | None
    is_active: bool
    last_seen_at: datetime | None
    telemetry: str | None
    enabled: bool
    provider: str
    frame_skip: int
    created_at: datetime


class AiNodeUpdate(BaseModel):
    """Super-admin config edit."""

    name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=64)
    frame_skip: int | None = Field(default=None, ge=0, le=30)
