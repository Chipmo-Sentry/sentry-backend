"""AI node schemas — pairing, heartbeat, config, admin views."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_serializer,
)

# A node is "online" if its last heartbeat landed within this window. The
# heartbeat interval is 60s; this tolerates a couple of missed/late beats
# (the heartbeat process can lag under GPU load) without flapping.
NODE_ONLINE_WINDOW = timedelta(minutes=5)


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


class CameraHealth(BaseModel):
    """Per-camera stream health inside the node heartbeat (audit T12 #3).

    Lets the cloud tell WHICH camera died instead of only seeing the summed
    FPS dip. status: ok = inferring, stalled = worker alive but 0 FPS,
    error = worker dead or RTSP read/open failure.
    """

    camera_id: str = Field(min_length=1, max_length=64)
    fps: float | None = Field(default=None, ge=0)
    status: Literal["ok", "stalled", "error"] = "ok"


def parse_camera_health(telemetry: str | None) -> list[CameraHealth] | None:
    """Extract the `cameras` list from a stored telemetry JSON string.

    None when telemetry is missing, malformed, or from an old node version that
    doesn't report per-camera health yet. Shared by the admin API (AiNodePublic)
    and the node watchdog so both read the same shape.
    """
    if not telemetry:
        return None
    try:
        data = json.loads(telemetry)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cams = data.get("cameras")
    if not isinstance(cams, list):
        return None
    try:
        return [CameraHealth.model_validate(c) for c in cams]
    except ValidationError:
        return None


class AiNodeHeartbeat(BaseModel):
    """Telemetry the node reports every ~60s."""

    fps_inference: float | None = None
    vram_mb: int | None = Field(default=None, ge=0)  # legacy alias; prefer vram_used_mb below
    active_cameras: int | None = None
    version: str | None = Field(default=None, max_length=64)
    # Per-dependency health the node probes locally (e.g. {"ollama": true,
    # "ingest": false, "ai": true}). Opaque map so the node can add keys
    # without a schema change; surfaced read-only in superadmin.
    health: dict[str, bool] | None = None
    # Resource load for the observability dashboard (docs/19). GPU fields are
    # None on a CPU-only node.
    cpu_pct: float | None = Field(default=None, ge=0, le=100)
    ram_used_mb: int | None = Field(default=None, ge=0)
    ram_total_mb: int | None = Field(default=None, ge=0)
    gpu_pct: int | None = Field(default=None, ge=0, le=100)
    vram_used_mb: int | None = Field(default=None, ge=0)
    vram_total_mb: int | None = Field(default=None, ge=0)
    gpu_temp_c: int | None = None
    # Sentry-project-only (process-scoped) usage — the project's own footprint,
    # vs the whole-machine fields above. No sentry_gpu_pct (NVML utilisation is
    # device-wide only; only VRAM is per-process).
    sentry_cpu_pct: float | None = Field(default=None, ge=0, le=100)
    sentry_ram_mb: int | None = Field(default=None, ge=0)
    sentry_vram_mb: int | None = Field(default=None, ge=0)
    # Per-camera stream health (audit T12 #3). Optional: old node versions don't
    # send it. Stored inside `ai_nodes.telemetry` JSON with the rest of the body
    # (no new table/migration); length-capped to keep that Text column bounded.
    cameras: list[CameraHealth] | None = Field(default=None, max_length=64)
    # Central-control feedback (ADR-0026): the VLM provider the node ACTUALLY uses
    # (effective), whether its model is reachable, and an error if not. Lets the
    # dashboard show applied-on-server vs applying vs error next to the desired
    # provider. Stored in telemetry JSON; does NOT overwrite the desired provider.
    provider_effective: str | None = Field(default=None, max_length=64)
    provider_ready: bool | None = None
    provider_error: str | None = Field(default=None, max_length=300)


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

    @field_serializer("last_seen_at", "created_at")
    def _ser_dt(self, v: datetime | None) -> str | None:
        # Always emit a tz-aware ISO string so the browser never parses a naive
        # UTC timestamp as local time (which made the node look hours stale →
        # permanently "offline" in UTC+ timezones).
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v.isoformat()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cameras(self) -> list[CameraHealth] | None:
        """Per-camera stream health parsed from the latest heartbeat telemetry
        (audit T12 #3). None for old node versions / no telemetry yet — the
        superadmin UI can distinguish "unknown" from "no cameras" ([])."""
        return parse_camera_health(self.telemetry)

    def _telemetry_obj(self) -> dict[str, object] | None:
        if not self.telemetry:
            return None
        try:
            data = json.loads(self.telemetry)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_effective(self) -> str | None:
        """VLM provider the node actually runs (from its last heartbeat). The UI
        compares this to `provider` (desired) to show applied vs applying."""
        obj = self._telemetry_obj()
        v = obj.get("provider_effective") if obj else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_ready(self) -> bool | None:
        obj = self._telemetry_obj()
        v = obj.get("provider_ready") if obj else None
        return v if isinstance(v, bool) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provider_error(self) -> str | None:
        """Why the effective provider isn't ready (e.g. model not pulled), or None."""
        obj = self._telemetry_obj()
        v = obj.get("provider_error") if obj else None
        return v if isinstance(v, str) else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_online(self) -> bool:
        """Server-computed online status — independent of the viewer's clock."""
        if self.last_seen_at is None:
            return False
        ls = self.last_seen_at
        if ls.tzinfo is None:
            ls = ls.replace(tzinfo=UTC)
        return datetime.now(UTC) - ls < NODE_ONLINE_WINDOW


class AiNodeUpdate(BaseModel):
    """Super-admin config edit."""

    name: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None
    provider: str | None = Field(default=None, max_length=64)
    frame_skip: int | None = Field(default=None, ge=0, le=30)
