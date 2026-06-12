"""Alert triggered_behaviors + triggered_sequences — episode context on live alerts

Revision ID: c0d1e2f3a4b5
Revises: b9d0e1f2a3b4
Create Date: 2026-06-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c0d1e2f3a4b5"
down_revision: str | None = "b9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("triggered_behaviors", sa.ARRAY(sa.String(length=64)), nullable=True),
    )
    op.add_column(
        "alerts",
        sa.Column("triggered_sequences", sa.ARRAY(sa.String(length=64)), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("alerts", "triggered_sequences")
    op.drop_column("alerts", "triggered_behaviors")
