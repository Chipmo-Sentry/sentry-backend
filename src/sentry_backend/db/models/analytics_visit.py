"""Visitor counting (docs/30 F3) — hourly entries per store camera.

A "visit" = the first frame a tracked person's foot point lands inside any of
the plan's gate fixtures (type ``entrance`` / ``exit``). Deduped per track over
the union of gate zones, so one person walking in counts once. Approximate
(track ids reset, one camera may not cover the whole door), but a solid headline
"зочид" KPI built entirely from the metadata that already flows — privacy-safe.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin


class AnalyticsVisit(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_visit"
    __table_args__ = (UniqueConstraint("store_id", "camera_id", "hour_ts", name="uq_visit_bucket"),)

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
    entries: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
