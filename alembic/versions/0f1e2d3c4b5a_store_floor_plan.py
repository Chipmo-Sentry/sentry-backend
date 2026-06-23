"""store floor plan — top-down schematic + camera placement + homography (docs/30)

Additive: a nullable JSONB `floor_plan` column on stores holding the agent-drawn
plan ({size, walls, fixtures, cameras[pos,dir_deg,homography,calib_points]}).
null = no plan yet, so every existing store is unchanged. The plan is the SOURCE
for per-camera Camera.zones (derived via each camera's homography).

Revision ID: 0f1e2d3c4b5a
Revises: f7a1b2c3d4e5
Create Date: 2026-06-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0f1e2d3c4b5a"
down_revision: str | None = "f7a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "stores",
        sa.Column("floor_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("stores", "floor_plan")
