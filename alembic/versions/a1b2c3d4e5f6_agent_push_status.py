"""agent push status — per-camera ffmpeg relay state in the heartbeat

Additive: a nullable JSONB `push_status` column on agents holding the latest
per-camera push-relay state ([{path, running, restarts, last_error}]) the agent
reports each heartbeat. null = never reported (older agent, or pull/on-LAN
topology). Lets the cloud pipeline view surface WHY a camera's push is down
without anyone RDP-ing into the store PC.

Revision ID: a1b2c3d4e5f6
Revises: 0f1e2d3c4b5a
Create Date: 2026-06-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "0f1e2d3c4b5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agents",
        sa.Column("push_status", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agents", "push_status")
