"""Dwell-time analytics (docs/30 F3+) — how long visitors stay.

One row = one (store, camera, hour) bucket accumulating the number of completed
tracks and the sum of their dwell durations (first-seen → last-seen), so avg
dwell = dwell_ms_sum / track_count. The hour is when each track STARTED. A track
completes when it's been absent longer than the aggregator's TTL. Privacy-safe:
only durations + counts, never tracks or identities.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin


class AnalyticsDwell(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_dwell"
    __table_args__ = (UniqueConstraint("store_id", "camera_id", "hour_ts", name="uq_dwell_bucket"),)

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
    track_count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    dwell_ms_sum: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
