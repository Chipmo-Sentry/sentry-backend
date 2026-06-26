"""alert edge_clip_id — edge-first traceability

Additive: a nullable, indexed String `edge_clip_id` on alerts. Set only for
`triggered_by=edge_pc_upload` alerts to the agent-pc edge clip id
(`{camera_id}_{epoch}`) that produced them, so the store operator can match a
row in the agent-pc «Сэжигтэй» list to its frontend «Сэжигтэй үйлдэл» alert.
Null on every existing/cloud/manual alert (unchanged behaviour).

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-06-26 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "alerts",
        sa.Column("edge_clip_id", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_alerts_edge_clip_id", "alerts", ["edge_clip_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_alerts_edge_clip_id", table_name="alerts")
    op.drop_column("alerts", "edge_clip_id")
