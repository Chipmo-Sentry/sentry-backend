"""Per-visitor walked paths (docs/30 F4 paths) — the «spaghetti» layer.

One row per finished (pruned) track: a Douglas-Peucker-simplified polyline of
the person's plan-projected foot positions, normalized to [0,1]². Anonymous —
no person id survives; the row is just «somebody walked this line then».
Rendering thousands of these as faint dotted traces shows the store's real
walking corridors the way aggregated arrows never can.

Size guard: paths are capped at _PATH_MAX_POINTS on write and stores are
queried with a row limit, so the table grows linearly with visitors (~1 KB per
visit) and reads stay bounded.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin


class AnalyticsPath(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_paths"

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
    # The camera whose track produced the path (mediamtx path string, matches
    # the other analytics tables' camera_id convention).
    camera_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    duration_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # [[x, y], ...] normalized to the plan [0,1]² — already simplified.
    points: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    n_points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
