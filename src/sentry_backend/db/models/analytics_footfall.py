"""Footfall analytics (docs/30 F2) — aggregated dwell grid over the floor plan.

One row = one (store, camera, hour, grid-cell) bucket holding how many
person-frame samples landed in that cell. The grid is a fixed GRID×GRID lattice
over the plan's NORMALIZED [0,1]² space, so every camera's footfall — projected
through its homography (or a pre-calibration fallback anchored at the camera) —
accumulates onto ONE shared plan. Privacy-safe: no tracks, no identities, no
images ever leave the edge; only per-cell counts land here.

Gender columns are additive placeholders for F5 (demographics) so no later
migration is needed; F2 leaves them zero.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin

# Grid resolution over the plan's normalized space. 48×48 ≈ 2300 cells — enough
# spatial detail for a store map without exploding row counts (a busy hour tops
# out at a few hundred touched cells). Mirrored in the frontend heatmap layer.
GRID_SIZE = 48


class AnalyticsFootfall(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_footfall"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "camera_id",
            "hour_ts",
            "grid_x",
            "grid_y",
            name="uq_footfall_bucket",
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
    # The camera's mediamtx_path (string id from live metadata). Kept as a string
    # rather than an FK so a footfall bucket survives a camera row being deleted /
    # re-slugged, and so edge nodes can push without a UUID round-trip.
    camera_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hour bucket (UTC, truncated to the hour). Range queries filter on this.
    hour_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    grid_x: Mapped[int] = mapped_column(Integer, nullable=False)
    grid_y: Mapped[int] = mapped_column(Integer, nullable=False)
    # Person-frame samples that landed in this cell this hour. Proportional to
    # dwell time × people (one sample per analyzed frame per person).
    samples: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    # F5 placeholders — demographic sample split. Zero until F5 wires a classifier.
    gender_m: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    gender_f: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
