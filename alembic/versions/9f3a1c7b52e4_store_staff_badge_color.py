"""stores.staff_badge_color — per-store staff lanyard color.

One AI node serves many stores; the badge color used to distinguish staff from
visitors was node-global, so a multi-store node couldn't tell apart stores with
different uniforms. This per-store color is forwarded to the store's live
workers and wins over the node-global `staff_badge_color`; NULL → the store
falls back to the node color.

Revision ID: 9f3a1c7b52e4
Revises: e9f0a1b2c3d5
"""

import sqlalchemy as sa
from alembic import op

revision = "9f3a1c7b52e4"
down_revision = "e9f0a1b2c3d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("staff_badge_color", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("stores", "staff_badge_color")
