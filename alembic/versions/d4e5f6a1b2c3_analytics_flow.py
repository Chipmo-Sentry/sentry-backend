"""analytics flow — directed movement-transition counts (docs/30 F4 flow)

Additive: a new `analytics_flow` table holding per-(store, camera, hour, edge)
transition counts on a coarse grid. No existing table is touched.

Revision ID: d4e5f6a1b2c3
Revises: c3d4e5f6a1b2
Create Date: 2026-07-06 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a1b2c3"
down_revision: str | None = "c3d4e5f6a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_flow",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("hour_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_cell", sa.Integer(), nullable=False),
        sa.Column("to_cell", sa.Integer(), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "store_id", "camera_id", "hour_ts", "from_cell", "to_cell", name="uq_flow_edge"
        ),
    )
    op.create_index("ix_analytics_flow_organization_id", "analytics_flow", ["organization_id"])
    op.create_index("ix_analytics_flow_store_id", "analytics_flow", ["store_id"])
    op.create_index("ix_analytics_flow_hour_ts", "analytics_flow", ["hour_ts"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_flow_hour_ts", table_name="analytics_flow")
    op.drop_index("ix_analytics_flow_store_id", table_name="analytics_flow")
    op.drop_index("ix_analytics_flow_organization_id", table_name="analytics_flow")
    op.drop_table("analytics_flow")
