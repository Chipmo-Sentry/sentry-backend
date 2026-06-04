"""verified_cases: add server_default now() to created_at/updated_at

The b2c3d4e5f6a7 table was created without a DB default on the timestamp
columns, so inserts (which don't set updated_at) hit a NOT NULL violation. This
backfills the default on already-created tables (local + prod).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04 16:45:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column("verified_cases", "created_at", server_default=sa.text("now()"))
    op.alter_column("verified_cases", "updated_at", server_default=sa.text("now()"))


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column("verified_cases", "created_at", server_default=None)
    op.alter_column("verified_cases", "updated_at", server_default=None)
