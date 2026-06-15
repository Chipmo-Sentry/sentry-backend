"""event_type enum += risk_episode

Revision ID: f1a2b3c4d5e7
Revises: e7f8a9b0c1d2
Create Date: 2026-06-15 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f1a2b3c4d5e7"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the risk_episode value to the event_type enum.

    ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so use an
    autocommit block (Alembic wraps migrations in a transaction by default).
    """
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_type ADD VALUE IF NOT EXISTS 'risk_episode'")


def downgrade() -> None:
    """No-op — PostgreSQL cannot drop a single enum value without recreating the
    type, and nothing depends on its removal."""
