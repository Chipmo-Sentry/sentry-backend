"""Camera CRUD schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from sentry_backend.db.models.camera import Camera

# mediamtx_path flows into filesystem paths (clip_cutter recordings root) and
# the MediaMTX control-API URL, so it MUST be a safe slug — lowercase
# alphanumerics, underscore and hyphen only, no '/', '..', whitespace, etc.
# Shared by CameraCreate/CameraUpdate and the agent-facing AgentCameraCreate.
MEDIAMTX_PATH_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class CameraCreate(BaseModel):
    store_id: UUID
    name: str = Field(min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    enabled: bool = True
    # L5 live-pipeline registration (used by sentry-agent-pc).
    # mediamtx_path is unique per Camera and drives the live-worker camera_id;
    # if None, backend auto-generates a slug from `name`.
    mediamtx_path: str | None = Field(default=None, pattern=MEDIAMTX_PATH_PATTERN)
    risk_threshold: float = Field(default=50.0, ge=0.0, le=100.0)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool | None = None
    mediamtx_path: str | None = Field(default=None, pattern=MEDIAMTX_PATH_PATTERN)
    risk_threshold: float | None = Field(default=None, ge=0.0, le=100.0)


class StreamTokenResponse(BaseModel):
    """Short-lived per-camera WHEP/HLS read token (appended as ?jwt=…)."""

    token: str
    expires_in: int


class CameraPublic(BaseModel):
    """Public Camera — `rtsp_url` NEVER exposed; only a boolean `has_rtsp_url`."""

    id: UUID
    store_id: UUID
    name: str
    shelf_zone_json: dict[str, Any] | None
    stage2_threshold: float
    enabled: bool
    has_rtsp_url: bool
    created_at: datetime
    # L5 live-pipeline fields
    mediamtx_path: str | None = None
    risk_threshold: float = 50.0

    @classmethod
    def from_orm_camera(cls, camera: Camera) -> "CameraPublic":
        return cls(
            id=camera.id,
            store_id=camera.store_id,
            name=camera.name,
            shelf_zone_json=camera.shelf_zone_json,
            stage2_threshold=camera.stage2_threshold,
            enabled=camera.enabled,
            has_rtsp_url=camera.rtsp_url_encrypted is not None,
            created_at=camera.created_at,
            mediamtx_path=camera.mediamtx_path,
            risk_threshold=camera.risk_threshold,
        )
