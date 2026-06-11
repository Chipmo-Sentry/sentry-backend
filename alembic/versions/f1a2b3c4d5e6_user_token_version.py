"""users.token_version (server-side token revocation)

Adds a monotonic ``token_version`` counter to ``users``. Baked into every
access/refresh JWT as the ``tv`` claim and checked at request time; bumping it
(logout / password change / "sign out everywhere") invalidates all previously
issued tokens for that user.

Additive + NOT NULL with server_default '0', so the column backfills to 0 for
all existing rows in one statement — safe online migration, no app downtime.

Revision ID: f1a2b3c4d5e6
Revises: e1a2b3c4d5e6
Create Date: 2026-06-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
