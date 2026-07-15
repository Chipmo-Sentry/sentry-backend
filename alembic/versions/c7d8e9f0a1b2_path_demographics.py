"""analytics_paths: gender + age_band of the walker (docs/30 F4×F5)

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-07-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("analytics_paths", sa.Column("gender", sa.String(length=16), nullable=True))
    op.add_column("analytics_paths", sa.Column("age_band", sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("analytics_paths", "age_band")
    op.drop_column("analytics_paths", "gender")
