"""alert.embedding — RAG: store the verify-time embedding (docs/19 Phase 4)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-04 17:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("embedding", postgresql.ARRAY(sa.Float()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("alerts", "embedding")
