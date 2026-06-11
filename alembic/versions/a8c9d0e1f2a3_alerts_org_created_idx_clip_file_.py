"""alerts (organization_id, created_at) index + clips.file_deleted_at

Two small T09 schema touches:

* ``ix_alerts_org_created_at`` — the dashboard's hot query lists an org's
  alerts newest-first; the composite index serves it directly instead of
  filtering on the single-column org index and sorting. The table is small
  today, so a plain (non-CONCURRENT) CREATE INDEX is fine.
* ``clips.file_deleted_at`` — nullable timestamp the retention sweeper stamps
  when it removed a clip's on-disk .mp4 but had to keep the row because an
  alert references it (alerts.clip_id is NOT NULL + ON DELETE CASCADE; alert
  rows are business history and must never be cascade-deleted). Additive +
  nullable, so this is a safe online migration.

Revision ID: a8c9d0e1f2a3
Revises: f1a2b3c4d5e6
Create Date: 2026-06-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c9d0e1f2a3"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_alerts_org_created_at",
        "alerts",
        ["organization_id", "created_at"],
    )
    op.add_column(
        "clips",
        sa.Column("file_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clips", "file_deleted_at")
    op.drop_index("ix_alerts_org_created_at", table_name="alerts")
