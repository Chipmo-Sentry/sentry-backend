"""pose_sequence — skeleton trajectory training data (ADR-0030, Фаз 0)

Additive: a nullable JSONB `pose_sequence` column on `alerts` (captured by the AI
node at breach time — the breaching person's skeleton trajectory, pose only, no
pixels) and on `verified_cases` (copied from the alert at verify time, paired with
the staff verdict → a labelled training row for the skeleton-anomaly model).
null = no pose, so every existing row is unchanged.

Revision ID: 9a8b7c6d5e4f
Revises: f0a1b2c3d4e5
Create Date: 2026-06-29 20:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a8b7c6d5e4f"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("pose_sequence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "verified_cases",
        sa.Column("pose_sequence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("verified_cases", "pose_sequence")
    op.drop_column("alerts", "pose_sequence")
