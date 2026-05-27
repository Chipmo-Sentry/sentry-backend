"""Clip schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ClipPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    store_id: UUID | None
    camera_id: UUID | None
    captured_at: datetime
    received_at: datetime
    duration_sec: float
    file_size_bytes: int
    sha256: str
    created_at: datetime


class ClipUploadMeta(BaseModel):
    """Form fields sent alongside the multipart file."""

    store_id: UUID
    camera_id: UUID | None = None
    captured_at: datetime | None = None
    duration_sec: float = Field(default=0.0, ge=0.0)
