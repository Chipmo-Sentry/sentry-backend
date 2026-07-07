"""analytics visit — hourly visitor counts per store camera (docs/30 F3)

Additive: a new `analytics_visit` table holding per-(store, camera, hour) entry
counts derived from gate-zone crossings. No existing table is touched.

Revision ID: b2c3d4e5f6a1
Revises: a1f2b3c4d5e6
Create Date: 2026-07-06 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a1"
down_revision: str | None = "a1f2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_visit",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("hour_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entries", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("store_id", "camera_id", "hour_ts", name="uq_visit_bucket"),
    )
    op.create_index("ix_analytics_visit_organization_id", "analytics_visit", ["organization_id"])
    op.create_index("ix_analytics_visit_store_id", "analytics_visit", ["store_id"])
    op.create_index("ix_analytics_visit_hour_ts", "analytics_visit", ["hour_ts"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_visit_hour_ts", table_name="analytics_visit")
    op.drop_index("ix_analytics_visit_store_id", table_name="analytics_visit")
    op.drop_index("ix_analytics_visit_organization_id", table_name="analytics_visit")
    op.drop_table("analytics_visit")
