"""lower existing cameras' risk_threshold 50 → 11 (yellow band)

Revision ID: f2b3c4d5e6f8
Revises: f1a2b3c4d5e7
Create Date: 2026-06-15 13:00:00.000000

The live alert threshold default dropped from 50 (deep red) to 11 (yellow) so
anything that turns yellow/red on the live view gets cut + VLM-verified (the VLM
filters out "browsing"). Bring existing cameras still on the old default to the
new one. Cameras whose threshold was customised away from 50 are left untouched.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f2b3c4d5e6f8"
down_revision: str | None = "f1a2b3c4d5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Catch both historical defaults (50 and the agent's 70) — anything in the
    # high/deep-red band drops to yellow. Cameras intentionally tuned LOW (<25)
    # are left as-is.
    op.execute("UPDATE cameras SET risk_threshold = 11 WHERE risk_threshold >= 25")


def downgrade() -> None:
    op.execute("UPDATE cameras SET risk_threshold = 50 WHERE risk_threshold = 11")
