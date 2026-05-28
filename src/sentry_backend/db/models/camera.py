"""Camera (per-store IP camera)."""

from typing import Any
from uuid import UUID

from sqlalchemy import Boolean, Float, ForeignKey, LargeBinary, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Camera(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cameras"

    store_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Encrypted at rest with Fernet — see sentry_backend.security
    rtsp_url_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # {"x": int, "y": int, "w": int, "h": int} — bbox for Stage 1 shelf-zone filter
    shelf_zone_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    stage2_threshold: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # M1-LIVE L5: maps this Camera row to a sentry-ingest MediaMTX path name
    # (e.g. "cam1_hik") so the live worker can resolve `camera_id` (string from
    # live_metadata) back to the org/store/camera UUID hierarchy.
    mediamtx_path: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True,
    )
    # Risk threshold (0-100); when a tracked person crosses this in live mode,
    # we cut a clip + run VLM verify. Default 70 matches REQUIREMENTS F4.7.
    risk_threshold: Mapped[float] = mapped_column(Float, default=70.0, nullable=False)
