"""ai_nodes — per-node YOLO + scan/VLM tuning settings

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-06-18 12:00:00.000000

The whole detection pipeline becomes operator-tunable per node from superadmin
(central control, like `provider`/`breach_mode`). The node polls these from
/ai-nodes/config and hot-applies them to its running camera workers + verify
path without a restart. Before this, only `frame_skip` existed as a column — and
even that was DEAD (served to the node but never consumed); the rest lived in the
node's .env or as hardcoded literals. Existing rows get today's effective
defaults so behaviour is unchanged until an operator edits them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d7e8f9a0b1"
down_revision: str | None = "b5c6d7e8f9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_nodes",
        sa.Column("person_conf", sa.Float(), nullable=False, server_default="0.35"),
    )
    op.add_column(
        "ai_nodes",
        sa.Column("item_conf", sa.Float(), nullable=False, server_default="0.4"),
    )
    op.add_column(
        "ai_nodes",
        sa.Column("item_every_n", sa.Integer(), nullable=False, server_default="5"),
    )
    op.add_column(
        "ai_nodes",
        sa.Column("scan_interval_sec", sa.Float(), nullable=False, server_default="3.0"),
    )
    op.add_column(
        "ai_nodes",
        sa.Column("frames_per_clip", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "ai_nodes",
        sa.Column("frame_max_dim", sa.Integer(), nullable=False, server_default="320"),
    )


def downgrade() -> None:
    op.drop_column("ai_nodes", "frame_max_dim")
    op.drop_column("ai_nodes", "frames_per_clip")
    op.drop_column("ai_nodes", "scan_interval_sec")
    op.drop_column("ai_nodes", "item_every_n")
    op.drop_column("ai_nodes", "item_conf")
    op.drop_column("ai_nodes", "person_conf")
