"""agent hls_tunnel_base — agent-direct video via cloudflared tunnel

Additive: a nullable String `hls_tunnel_base` on agents. The agent reports its
public cloudflared HLS base each heartbeat so /live proxies video straight from
the agent (no GPU-node relay). Null on every existing row (unchanged behaviour:
/live falls back to the node).

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-06-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "agents",
        sa.Column("hls_tunnel_base", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("agents", "hls_tunnel_base")
