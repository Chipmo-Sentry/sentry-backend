"""camera.compute_tier (ADR-0029 edge Stage-1)

Adds the per-camera compute tier (cloud | edge_pc | edge_device). Additive:
a plain String(16) with server_default 'cloud' so every existing camera
backfills to the unchanged cloud-pull path. Not a PG enum, so a future tier
needs no type migration.

Revision ID: d5e6f7a8b9c0
Revises: c6d7e8f9a0b1
Create Date: 2026-06-20 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "cameras",
        sa.Column(
            "compute_tier",
            sa.String(length=16),
            server_default="cloud",
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("cameras", "compute_tier")
