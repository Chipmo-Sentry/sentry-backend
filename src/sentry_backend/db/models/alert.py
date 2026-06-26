"""Alert — AI verdict on a clip."""

import enum
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, Enum, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AlertCategory(enum.StrEnum):
    browsing = "browsing"
    cart_pickup = "cart_pickup"
    pocket_conceal = "pocket_conceal"
    bag_conceal = "bag_conceal"
    other = "other"


class AlertLevel(enum.StrEnum):
    ignore = "ignore"
    log = "log"
    notify = "notify"
    review = "review"


class AlertTrigger(enum.StrEnum):
    manual_upload = "manual_upload"  # human uploaded mp4 via /clips/upload
    live_threshold = "live_threshold"  # live risk_pct crossed camera.risk_threshold
    edge_pc_upload = "edge_pc_upload"  # edge store PC ran Stage-1 + uploaded the clip (ADR-0029)


class Alert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "alerts"
    # T09: the dashboard's hot path lists an org's alerts newest-first; the
    # composite index serves that without a sort over the whole org partition.
    __table_args__ = (Index("ix_alerts_org_created_at", "organization_id", "created_at"),)

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
    # Multi-label: every action the VLM detected in the clip (ADR 2026-06-17).
    # `category` above is the primary (most-severe) of these, kept for the level
    # logic, analytics, Telegram, and RAG. Null on older rows / manual uploads →
    # the UI falls back to [category].
    actions: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)), nullable=True)
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
    # When triggered_by=live_threshold: episode context at breach time — fired
    # criterion keys (first-fired order) + completed sequence rule keys. Stable
    # keys; Mongolian labels come from /api/v1/behaviors (label_mn).
    triggered_behaviors: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)), nullable=True)
    triggered_sequences: Mapped[list[str] | None] = mapped_column(ARRAY(String(64)), nullable=True)
    # Per-criterion timeline for the alert detail: list of
    # {key, offset_sec, score} in first-fired order (offset = seconds from the
    # episode's first firing). Richer than triggered_behaviors (keys only);
    # nullable so older alerts / manual uploads simply have none.
    triggered_behavior_detail: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    # Embedding of `reasoning` computed by the AI node at verify time (docs/19
    # Phase 4 RAG). Lets staff feedback create a verified_case for retrieval
    # WITHOUT a second embed round-trip. Null on live-threshold alerts.
    embedding: Mapped[list[float] | None] = mapped_column(ARRAY(Float), nullable=True)
    # Edge-first traceability: the agent-pc edge clip id (`{camera_id}_{epoch}`)
    # that produced this alert. Lets the store operator match a row in the
    # agent-pc «Сэжигтэй» list to its frontend «Сэжигтэй үйлдэл» alert. Only set
    # for triggered_by=edge_pc_upload; null for every cloud/live/manual alert.
    edge_clip_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
