"""Movement-flow analytics (docs/30 F4 flow) — aggregated traffic between areas.

Rather than store per-person trajectories (heavy + identity-adjacent), we keep a
directed transition count: each time a tracked person moves from one coarse
FLOW_GRID cell to an adjacent one, that edge's count increments. Summed over a
window this is the store's movement graph — rendered as flow lines whose
thickness ∝ count. Privacy-safe: only anonymous edge counts.

Cells are indexed on a FLOW_GRID×FLOW_GRID lattice over the plan's normalized
[0,1]² space; a cell's single-int id is ``fy * FLOW_GRID + fx``.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, UUIDPrimaryKeyMixin

# Coarser than the dwell heatmap (48) — flow lines want a readable graph, not a
# dense mesh. 12×12 = 144 nodes, so at most ~144·8 directed adjacency edges.
FLOW_GRID = 12


class AnalyticsFlow(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "analytics_flow"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "camera_id",
            "hour_ts",
            "from_cell",
            "to_cell",
            name="uq_flow_edge",
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
    from_cell: Mapped[int] = mapped_column(Integer, nullable=False)
    to_cell: Mapped[int] = mapped_column(Integer, nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
