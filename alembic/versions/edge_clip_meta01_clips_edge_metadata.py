"""clips.edge_behavior_detail / edge_risk_pct — edge gate score breakdown

Revision ID: edge_clip_meta01
Revises: c6d7e8f9a0b1
Create Date: 2026-06-22 12:00:00.000000

The edge gate (sentry-agent-pc) decides a clip is worth looking at and uploads
it via POST /agent/edge/clips. These columns carry the edge's own per-movement
score breakdown (`edge_behavior_detail` = [{key, offset_sec, score}]) + its peak
risk, so the superadmin pipeline can show what the EDGE scored alongside the
cloud's re-score. Both nullable — null for manual uploads / live-push clips.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "edge_clip_meta01"
down_revision: str | None = "c6d7e8f9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clips", sa.Column("edge_behavior_detail", JSONB, nullable=True))
    op.add_column("clips", sa.Column("edge_risk_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("clips", "edge_risk_pct")
    op.drop_column("clips", "edge_behavior_detail")
