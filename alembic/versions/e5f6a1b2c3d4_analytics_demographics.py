"""analytics demographics — hourly gender/age-band visitor counts (docs/30 F5)

Additive: a new `analytics_demographics` table holding per-(store, camera,
hour, gender, age_band) counts derived from optional per-track classifier
attributes on the live-metadata stream. No existing table is touched.

Revision ID: e5f6a1b2c3d4
Revises: d4e5f6a1b2c3
Create Date: 2026-07-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f6a1b2c3d4"
down_revision: str | None = "d4e5f6a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_demographics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("store_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("camera_id", sa.String(length=64), nullable=False),
        sa.Column("hour_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gender", sa.String(length=16), nullable=False),
        sa.Column("age_band", sa.String(length=16), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "store_id", "camera_id", "hour_ts", "gender", "age_band", name="uq_demographics_bucket"
        ),
    )
    op.create_index(
        "ix_analytics_demographics_organization_id", "analytics_demographics", ["organization_id"]
    )
    op.create_index("ix_analytics_demographics_store_id", "analytics_demographics", ["store_id"])
    op.create_index("ix_analytics_demographics_hour_ts", "analytics_demographics", ["hour_ts"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analytics_demographics_hour_ts", table_name="analytics_demographics")
    op.drop_index("ix_analytics_demographics_store_id", table_name="analytics_demographics")
    op.drop_index("ix_analytics_demographics_organization_id", table_name="analytics_demographics")
    op.drop_table("analytics_demographics")
