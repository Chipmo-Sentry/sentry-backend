"""Camera CRUD schemas."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from sentry_backend.db.models.camera import Camera


class CameraCreate(BaseModel):
    store_id: UUID
    name: str = Field(min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    enabled: bool = True


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    rtsp_url: str | None = Field(default=None, max_length=2048)
    shelf_zone_json: dict[str, Any] | None = None
    stage2_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool | None = None


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
        )
