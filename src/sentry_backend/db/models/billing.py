"""Billing core (T14) — prepaid wallet, double-entry journal, promo codes.

Money model: every movement is a Dr/Cr journal row in `billing_journal`;
`billing_profiles.balance_mnt` is a derived cache of the org_wallet account,
updated in the SAME transaction as the journal insert (see
services/billing/ledger.py). The partial unique index on
(org_id, charge_date) WHERE kind='usage_charge' makes the daily charger
idempotent — a second charge for the same Ulaanbaatar day hits the constraint
and is skipped.
"""

import enum
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from sentry_backend.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class JournalKind(enum.StrEnum):
    topup = "topup"
    usage_charge = "usage_charge"
    promo_credit = "promo_credit"
    adjustment = "adjustment"


class LedgerAccount(enum.StrEnum):
    cash = "cash"
    org_wallet = "org_wallet"
    revenue = "revenue"
    promo_expense = "promo_expense"


class BillingJournal(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "billing_journal"
    __table_args__ = (
        CheckConstraint("amount_mnt > 0", name="ck_billing_journal_amount_positive"),
        Index("ix_billing_journal_org_posted", "org_id", "posted_at"),
        # One usage charge per org per (Ulaanbaatar) day — the charger's
        # idempotency guarantee, enforced by the database itself.
        Index(
            "uq_billing_journal_org_usage_day",
            "org_id",
            "charge_date",
            unique=True,
            postgresql_where=text("kind = 'usage_charge'"),
        ),
    )

    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    posted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    kind: Mapped[JournalKind] = mapped_column(
        Enum(JournalKind, name="billing_journal_kind"), nullable=False
    )
    dr_account: Mapped[LedgerAccount] = mapped_column(
        Enum(LedgerAccount, name="billing_account"), nullable=False
    )
    cr_account: Mapped[LedgerAccount] = mapped_column(
        Enum(LedgerAccount, name="billing_account"), nullable=False
    )
    amount_mnt: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Set only on kind='usage_charge': the Ulaanbaatar calendar day charged.
    charge_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class BillingProfile(Base):
    """Per-org wallet state. Lazy-created (get_or_create) on first touch.

    `balance_mnt` is the derived org_wallet balance — every journal post
    updates it in the same transaction (ledger.py). credit_until lets a
    super-admin keep a non-paying org alive temporarily ("яаралтай нээлт");
    promo_free_until is the free-days promo window.
    """

    __tablename__ = "billing_profiles"

    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    balance_mnt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    credit_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credit_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    promo_free_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PromoKind(enum.StrEnum):
    bonus_amount = "bonus_amount"
    free_days = "free_days"


class PromoCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    kind: Mapped[PromoKind] = mapped_column(Enum(PromoKind, name="promo_kind"), nullable=False)
    amount_mnt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    free_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_redemptions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    redeemed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class PromoRedemption(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (Index("uq_promo_redemptions_promo_org", "promo_id", "org_id", unique=True),)

    promo_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("promo_codes.id", ondelete="CASCADE"),
        nullable=False,
    )
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    journal_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("billing_journal.id", ondelete="SET NULL"),
        nullable=True,
    )
