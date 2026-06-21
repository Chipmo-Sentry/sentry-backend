"""AlertTrigger.edge_pc_upload (ADR-0029 edge clip path)

Adds the 'edge_pc_upload' value to the alert_trigger PG enum — edge store PCs
run Stage-1 locally and upload the suspicious clip, a third alert trigger
alongside manual_upload + live_threshold. Additive.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-21 05:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema. (PG 12+ allows ADD VALUE inside a transaction.)"""
    op.execute("ALTER TYPE alert_trigger ADD VALUE IF NOT EXISTS 'edge_pc_upload'")


def downgrade() -> None:
    """Downgrade schema.

    Postgres has no DROP VALUE for an enum, so this is a no-op — leaving the
    value in place is harmless (no row will reference it after a rollback).
    """
