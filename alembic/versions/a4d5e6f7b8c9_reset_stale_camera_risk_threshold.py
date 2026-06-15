"""reset stale camera risk_threshold (agent's 70) → 11

Revision ID: a4d5e6f7b8c9
Revises: f3c4d5e6a7b9
Create Date: 2026-06-15 17:00:00.000000

The node-push breach path now HONORS the per-camera risk_threshold (previously
it was ignored — the node always fired at its global 11). Cameras registered by
an older agent default still sit at 70 (deep red), so making the threshold
effective without this would suddenly suppress yellow-band alerts. Re-apply the
f2b3c4d5e6f8 normalisation (anything >= 25 → 11, the yellow band) to catch rows
created after that migration ran. Cameras intentionally tuned LOW (<25) are left
as-is.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4d5e6f7b8c9"
down_revision: str | None = "f3c4d5e6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE cameras SET risk_threshold = 11 WHERE risk_threshold >= 25")


def downgrade() -> None:
    # Not reversible — the prior per-row values aren't recorded. No-op so a
    # downgrade past this point doesn't fail.
    pass
