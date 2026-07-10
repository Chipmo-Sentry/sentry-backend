"""Visitor demographics (docs/30 F5) — hourly gender/age-band counts.

Populated from OPTIONAL per-track attributes on the live-metadata stream
(`LiveTrack.gender` / `LiveTrack.age_band`, sent when an AI node runs a
demographics classifier). One count per tracked person per camera within the
dedup TTL — same approximation model as visits. Rows are aggregates only
(counts per hour bucket); no per-person record is ever stored, which keeps the
feature privacy-reviewable.

Buckets are normalized server-side to a closed vocabulary so a creative
classifier can't fan out unbounded rows: gender ∈ {male, female, unknown},
age_band ∈ {child, youth, adult, senior, unknown}.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin

GENDERS = ("male", "female", "unknown")
AGE_BANDS = ("child", "youth", "adult", "senior", "unknown")


class AnalyticsDemographics(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_demographics"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "camera_id",
            "hour_ts",
            "gender",
            "age_band",
            name="uq_demographics_bucket",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    store_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stores.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hour_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    gender: Mapped[str] = mapped_column(String(16), nullable=False)
    age_band: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
