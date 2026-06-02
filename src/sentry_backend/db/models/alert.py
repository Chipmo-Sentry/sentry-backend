"""Alert — AI verdict on a clip."""

import enum
from uuid import UUID

from sqlalchemy import Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AlertCategory(enum.StrEnum):
    browsing = "browsing"
    cart_pickup = "cart_pickup"
    pocket_conceal = "pocket_conceal"
    other = "other"


class AlertLevel(enum.StrEnum):
    ignore = "ignore"
    log = "log"
    notify = "notify"
    review = "review"


class AlertTrigger(enum.StrEnum):
    manual_upload = "manual_upload"  # human uploaded mp4 via /clips/upload
    live_threshold = "live_threshold"  # live risk_pct crossed camera.risk_threshold


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alerts"

    clip_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clips.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    store_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    camera_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("cameras.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    category: Mapped[AlertCategory] = mapped_column(
        Enum(AlertCategory, name="alert_category"), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    alert_level: Mapped[AlertLevel] = mapped_column(
        Enum(AlertLevel, name="alert_level"), index=True, nullable=False
    )
    inference_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    # M1-LIVE L5: what generated this alert + tracking context
    triggered_by: Mapped[AlertTrigger] = mapped_column(
        Enum(AlertTrigger, name="alert_trigger"),
        default=AlertTrigger.manual_upload,
        server_default=AlertTrigger.manual_upload.value,
        nullable=False,
        index=True,
    )
    # When triggered_by=live_threshold: ByteTrack tracker_id that crossed threshold
    person_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When triggered_by=live_threshold: peak risk_pct observed for this person
    peak_risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
