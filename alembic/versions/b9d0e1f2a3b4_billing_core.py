"""T14 billing core — journal, profiles, promo codes/redemptions

Revision ID: b9d0e1f2a3b4
Revises: a8c9d0e1f2a3
Create Date: 2026-06-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b9d0e1f2a3b4"
down_revision: str | None = "a8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    journal_kind = sa.Enum(
        "topup", "usage_charge", "promo_credit", "adjustment", name="billing_journal_kind"
    )
    billing_account = sa.Enum(
        "cash", "org_wallet", "revenue", "promo_expense", name="billing_account"
    )
    promo_kind = sa.Enum("bonus_amount", "free_days", name="promo_kind")

    op.create_table(
        "billing_journal",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", journal_kind, nullable=False),
        sa.Column("dr_account", billing_account, nullable=False),
        sa.Column("cr_account", billing_account, nullable=False),
        sa.Column("amount_mnt", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("charge_date", sa.Date(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("amount_mnt > 0", name="ck_billing_journal_amount_positive"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_billing_journal_org_posted", "billing_journal", ["org_id", "posted_at"])
    # The daily charger's idempotency: one usage_charge per org per UB day.
    op.create_index(
        "uq_billing_journal_org_usage_day",
        "billing_journal",
        ["org_id", "charge_date"],
        unique=True,
        postgresql_where=sa.text("kind = 'usage_charge'"),
    )

    op.create_table(
        "billing_profiles",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_mnt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("credit_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credit_note", sa.String(length=500), nullable=True),
        sa.Column("promo_free_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
    )

    op.create_table(
        "promo_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("kind", promo_kind, nullable=False),
        sa.Column("amount_mnt", sa.Integer(), nullable=True),
        sa.Column("free_days", sa.Integer(), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), server_default="1", nullable=False),
        sa.Column("redeemed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_promo_codes_code"), "promo_codes", ["code"], unique=True)

    op.create_table(
        "promo_redemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("promo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("journal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["promo_id"], ["promo_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["journal_id"], ["billing_journal.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_promo_redemptions_promo_org",
        "promo_redemptions",
        ["promo_id", "org_id"],
        unique=True,
    )

    # Transition window: orgs that existed before billing shipped would land on
    # "suspended" (empty wallet) the moment this deploys, instantly cutting off
    # their live view. Seed every existing org with a 14-day promo window so
    # service continues while the founder sets up wallets/top-ups. New orgs
    # created after this migration start with no profile (= suspended until
    # funded), which is the intended prepaid behaviour.
    op.execute(
        sa.text(
            "INSERT INTO billing_profiles (org_id, balance_mnt, promo_free_until) "
            "SELECT id, 0, now() + interval '14 days' FROM organizations "
            "ON CONFLICT (org_id) DO NOTHING"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_promo_redemptions_promo_org", table_name="promo_redemptions")
    op.drop_table("promo_redemptions")
    op.drop_index(op.f("ix_promo_codes_code"), table_name="promo_codes")
    op.drop_table("promo_codes")
    op.drop_table("billing_profiles")
    op.drop_index("uq_billing_journal_org_usage_day", table_name="billing_journal")
    op.drop_index("ix_billing_journal_org_posted", table_name="billing_journal")
    op.drop_table("billing_journal")
    sa.Enum(name="promo_kind").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="billing_account").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="billing_journal_kind").drop(op.get_bind(), checkfirst=True)
