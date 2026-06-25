"""store agent push url — per-store cloud push target, editable from superadmin

Additive: a nullable String `agent_stream_push_url` column on stores. null → the
agent falls back to the global AGENT_STREAM_PUSH_URL env (unchanged behaviour for
every existing store). Lets a superadmin repoint a store's push base after a
vast.ai instance restart (new IP/port) without a backend redeploy.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-25 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "stores",
        sa.Column("agent_stream_push_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("stores", "agent_stream_push_url")
