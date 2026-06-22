"""camera zones — per-camera detection polygons (docs/29)

Additive: a nullable JSONB `zones` column on cameras holding a list of normalized
polygons [{"id","type","points":[[x,y],...]}] (0-1 image space). null = no zones,
so every existing camera is unchanged. Distinct from shelf_zone_json (single bbox).

Revision ID: f7a1b2c3d4e5
Revises: a7b8c9d0e1f2
Create Date: 2026-06-22 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a1b2c3d4e5"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "cameras",
        sa.Column("zones", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("cameras", "zones")
