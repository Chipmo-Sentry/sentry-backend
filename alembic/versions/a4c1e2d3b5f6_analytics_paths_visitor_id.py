"""analytics_paths.visitor_id — human-readable visitor number.

The owner reads per-visitor routes on the analytics page and wants each
visitor addressable: «20260903-012» = the 12th visit on 2026-09-03 (store-local
date). Node track ids reset per camera and per restart, so the backend assigns
the number on insert instead. Existing rows are backfilled in the same
store-local-date + arrival order so history is numbered consistently.

Revision ID: a4c1e2d3b5f6
Revises: 9f3a1c7b52e4
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c1e2d3b5f6"
down_revision = "9f3a1c7b52e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analytics_paths", sa.Column("visitor_id", sa.String(length=24), nullable=True))
    op.create_index(
        "ix_analytics_paths_store_visitor",
        "analytics_paths",
        ["store_id", "visitor_id"],
    )
    # Backfill: number every existing path per (store, store-local date) in
    # started_at order — the same rule insert_paths applies from now on.
    op.execute(
        """
        WITH numbered AS (
            SELECT p.id,
                   to_char(p.started_at AT TIME ZONE s.timezone, 'YYYYMMDD') || '-' ||
                   lpad(
                       (row_number() OVER (
                           PARTITION BY p.store_id, (p.started_at AT TIME ZONE s.timezone)::date
                           ORDER BY p.started_at, p.id
                       ))::text,
                       3, '0'
                   ) AS vid
            FROM analytics_paths p
            JOIN stores s ON s.id = p.store_id
        )
        UPDATE analytics_paths a
        SET visitor_id = numbered.vid
        FROM numbered
        WHERE a.id = numbered.id AND a.visitor_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_paths_store_visitor", table_name="analytics_paths")
    op.drop_column("analytics_paths", "visitor_id")
