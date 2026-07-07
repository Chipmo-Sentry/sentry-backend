"""analytics dwell — hourly dwell-time sums per store camera (docs/30 F3+)

Additive: a new `analytics_dwell` table holding per-(store, camera, hour) track
counts + dwell-duration sums, so avg dwell = dwell_ms_sum / track_count. No
existing table is touched.

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-07-06 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d4e5f6a1b2"
down_revision: str | None = "b2c3d4e5f6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_dwell",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("hour_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("track_count", sa.BigInteger(), nullable=False),
        sa.Column("dwell_ms_sum", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "camera_id", "hour_ts", name="uq_dwell_bucket"),
    )
    op.create_index("ix_analytics_dwell_organization_id", "analytics_dwell", ["organization_id"])
    op.create_index("ix_analytics_dwell_store_id", "analytics_dwell", ["store_id"])
    op.create_index("ix_analytics_dwell_hour_ts", "analytics_dwell", ["hour_ts"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_dwell_hour_ts", table_name="analytics_dwell")
    op.drop_index("ix_analytics_dwell_store_id", table_name="analytics_dwell")
    op.drop_index("ix_analytics_dwell_organization_id", table_name="analytics_dwell")
    op.drop_table("analytics_dwell")
