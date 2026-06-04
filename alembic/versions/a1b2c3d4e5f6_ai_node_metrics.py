"""ai_node_metrics time-series (observability dashboard, docs/19)

Revision ID: a1b2c3d4e5f6
Revises: f7a8b9c0d1e2
Create Date: 2026-06-04 15:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ai_node_metrics",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ai_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_pct", sa.Float(), nullable=True),
        sa.Column("ram_used_mb", sa.Integer(), nullable=True),
        sa.Column("ram_total_mb", sa.Integer(), nullable=True),
        sa.Column("gpu_pct", sa.Integer(), nullable=True),
        sa.Column("vram_used_mb", sa.Integer(), nullable=True),
        sa.Column("vram_total_mb", sa.Integer(), nullable=True),
        sa.Column("gpu_temp_c", sa.Integer(), nullable=True),
        sa.Column("fps_inference", sa.Float(), nullable=True),
        sa.Column("active_cameras", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["ai_node_id"], ["ai_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_node_metrics_node_ts", "ai_node_metrics", ["ai_node_id", "ts"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_ai_node_metrics_node_ts", table_name="ai_node_metrics")
    op.drop_table("ai_node_metrics")
