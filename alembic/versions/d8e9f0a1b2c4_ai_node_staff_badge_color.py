"""ai_nodes.staff_badge_color — staff identification badge color (central control).

NULL → staff identification off on the node. Set by superadmin once the owner
picks the staff lanyard color; the node polls it via /ai-nodes/config and
hot-applies (live_worker/staff.py).

Revision ID: d8e9f0a1b2c4
Revises: c7d8e9f0a1b2
"""

import sqlalchemy as sa
from alembic import op

revision = "d8e9f0a1b2c4"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_nodes", sa.Column("staff_badge_color", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_nodes", "staff_badge_color")
