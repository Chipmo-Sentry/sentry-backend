"""analytics_paths — per-visitor walked polylines (docs/30 F4 paths)

Revision ID: b6c7d8e9f0a1
Revises: e5f6a1b2c3d4
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b6c7d8e9f0a1"
down_revision: str | None = "e5f6a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_paths",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("store_id", sa.UUID(), nullable=False),
        sa.Column("camera_id", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("points", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("n_points", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_analytics_paths_organization_id"),
        "analytics_paths",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_paths_store_id"), "analytics_paths", ["store_id"], unique=False
    )
    op.create_index(
        op.f("ix_analytics_paths_started_at"), "analytics_paths", ["started_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_analytics_paths_started_at"), table_name="analytics_paths")
    op.drop_index(op.f("ix_analytics_paths_store_id"), table_name="analytics_paths")
    op.drop_index(op.f("ix_analytics_paths_organization_id"), table_name="analytics_paths")
    op.drop_table("analytics_paths")
