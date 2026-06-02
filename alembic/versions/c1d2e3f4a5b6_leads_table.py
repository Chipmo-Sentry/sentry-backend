"""leads table (public landing demo requests)

Revision ID: c1d2e3f4a5b6
Revises: b7a1c9e2d3f4
Create Date: 2026-06-02 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b7a1c9e2d3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    lead_status = sa.Enum(
        "new", "contacted", "qualified", "closed", name="lead_status"
    )
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("organization", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("industry", sa.String(length=128), nullable=True),
        sa.Column("camera_count", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("status", lead_status, nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_leads_email"), "leads", ["email"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_leads_email"), table_name="leads")
    op.drop_table("leads")
    sa.Enum(name="lead_status").drop(op.get_bind(), checkfirst=True)
