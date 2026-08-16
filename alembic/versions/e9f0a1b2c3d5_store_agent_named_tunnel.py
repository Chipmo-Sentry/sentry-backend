"""stores.agent_tunnel_token/_hostname — named cloudflared tunnel for the agent.

The store agent's quick tunnel (*.trycloudflare.com) gets a NEW random URL on
every cloudflared restart, so agent-direct video is dark until the next
heartbeat re-reports it. With a named tunnel the hostname is permanent: the
agent runs `cloudflared tunnel run --token <token>` and the public base is
known statically. Both NULL → the agent keeps using a quick tunnel.

Revision ID: e9f0a1b2c3d5
Revises: d8e9f0a1b2c4
"""

import sqlalchemy as sa
from alembic import op

revision = "e9f0a1b2c3d5"
down_revision = "d8e9f0a1b2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stores", sa.Column("agent_tunnel_token", sa.String(length=500), nullable=True))
    op.add_column(
        "stores", sa.Column("agent_tunnel_hostname", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("stores", "agent_tunnel_hostname")
    op.drop_column("stores", "agent_tunnel_token")
