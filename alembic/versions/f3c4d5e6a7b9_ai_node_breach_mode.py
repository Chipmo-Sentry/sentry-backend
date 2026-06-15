"""ai_nodes.breach_mode — central live-breach topology

Revision ID: f3c4d5e6a7b9
Revises: f2b3c4d5e6f8
Create Date: 2026-06-15 16:00:00.000000

The live-breach topology becomes backend-authoritative (ADR-0026 central
control, like `provider`): the node polls `breach_mode` from /ai-nodes/config
and hot-applies it, instead of deciding push locally via its own env. This
removes the two-flag split (node `live_alert_push_enabled` + backend
`live_alerts_via_node_push`) that could double-fire or silently no-op when the
two drifted. Existing nodes default to "node_push" (today's behaviour).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c4d5e6a7b9"
down_revision: str | None = "f2b3c4d5e6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_nodes",
        sa.Column(
            "breach_mode",
            sa.String(length=16),
            nullable=False,
            server_default="node_push",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_nodes", "breach_mode")
