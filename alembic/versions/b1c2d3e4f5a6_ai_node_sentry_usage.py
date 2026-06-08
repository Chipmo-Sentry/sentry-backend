"""ai_node_metrics: process-scoped (Sentry-only) usage columns

Adds sentry_cpu_pct / sentry_ram_mb / sentry_vram_mb so the dashboard can show
THIS project's footprint (sentry-ai + ingest + ollama + children) distinct from
the whole-machine cpu_pct/ram_used_mb/vram_used_mb.

Revision ID: b1c2d3e4f5a6
Revises: d4e5f6a7b8c9
Create Date: 2026-06-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_node_metrics", sa.Column("sentry_cpu_pct", sa.Float(), nullable=True))
    op.add_column("ai_node_metrics", sa.Column("sentry_ram_mb", sa.Integer(), nullable=True))
    op.add_column("ai_node_metrics", sa.Column("sentry_vram_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_node_metrics", "sentry_vram_mb")
    op.drop_column("ai_node_metrics", "sentry_ram_mb")
    op.drop_column("ai_node_metrics", "sentry_cpu_pct")
