"""multi-label alert actions + bag_conceal category

Revision ID: b5c6d7e8f9a0
Revises: a4d5e6f7b8c9
Create Date: 2026-06-17 21:00:00.000000

Detection went multi-label (ADR 2026-06-17): a clip can contain several actions,
so `alerts.actions` (text[]) records every action the VLM detected. `category`
stays as the primary (most-severe) for level/analytics/Telegram/RAG. Also adds the
new `bag_conceal` value to the alert_category enum (item concealed in a bag).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5c6d7e8f9a0"
down_revision: str | None = "a4d5e6f7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ADD VALUE can't run inside the migration's transaction on some PG versions;
    # autocommit_block runs it standalone. IF NOT EXISTS makes it idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE alert_category ADD VALUE IF NOT EXISTS 'bag_conceal'")
    op.add_column(
        "alerts",
        sa.Column("actions", postgresql.ARRAY(sa.String(length=32)), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alerts", "actions")
    # Enum values can't be dropped without recreating the type; leaving
    # 'bag_conceal' in place is harmless.
