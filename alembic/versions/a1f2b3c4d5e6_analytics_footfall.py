"""analytics footfall — aggregated dwell grid over the floor plan (docs/30 F2)

Additive: a new `analytics_footfall` table holding per-(store, camera, hour,
grid-cell) sample counts. No existing table is touched. Privacy-safe: only
counts, never tracks or images.

Revision ID: a1f2b3c4d5e6
Revises: 9a8b7c6d5e4f
Create Date: 2026-07-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1f2b3c4d5e6"
down_revision: str | None = "9a8b7c6d5e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_footfall",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("hour_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grid_x", sa.Integer(), nullable=False),
        sa.Column("grid_y", sa.Integer(), nullable=False),
        sa.Column("samples", sa.BigInteger(), nullable=False),
        sa.Column("gender_m", sa.BigInteger(), nullable=False),
        sa.Column("gender_f", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "store_id",
            "camera_id",
            "hour_ts",
            "grid_x",
            "grid_y",
            name="uq_footfall_bucket",
        ),
    )
    op.create_index(
        "ix_analytics_footfall_organization_id",
        "analytics_footfall",
        ["organization_id"],
    )
    op.create_index("ix_analytics_footfall_store_id", "analytics_footfall", ["store_id"])
    op.create_index("ix_analytics_footfall_hour_ts", "analytics_footfall", ["hour_ts"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_footfall_hour_ts", table_name="analytics_footfall")
    op.drop_index("ix_analytics_footfall_store_id", table_name="analytics_footfall")
    op.drop_index("ix_analytics_footfall_organization_id", table_name="analytics_footfall")
    op.drop_table("analytics_footfall")
