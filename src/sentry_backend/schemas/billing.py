"""Billing API schemas — user-facing wallet view + super-admin management."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sentry_backend.db.models.billing import JournalKind, LedgerAccount, PromoKind
from sentry_backend.services.billing.status import BillingStatus

# --- user-facing (org-scoped) ------------------------------------------------


class StoreBillingLine(BaseModel):
    """Per-store breakdown shown on the customer billing page."""

    store_id: UUID
    name: str
    active_cameras: int
    tier: str | None  # None when the store has no enabled cameras (free)
    platform_fee_mnt: int
    camera_fee_mnt: int
    monthly_mnt: int


class BillingSummary(BaseModel):
    balance_mnt: int
    status: BillingStatus
    credit_until: datetime | None
    promo_free_until: datetime | None
    daily_rate_mnt: int
    monthly_rate_mnt: int
    stores: list[StoreBillingLine]


class JournalEntryPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    posted_at: datetime
    kind: JournalKind
    dr_account: LedgerAccount
    cr_account: LedgerAccount
    amount_mnt: int
    description: str
    charge_date: date | None = None
    meta: dict[str, Any] | None = None


class PromoRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class PromoRedeemResponse(BaseModel):
    code: str
    kind: PromoKind
    amount_mnt: int | None = None
    free_days: int | None = None
    balance_mnt: int
    promo_free_until: datetime | None = None


# --- super-admin ---------------------------------------------------------------


class OrgBillingRow(BaseModel):
    org_id: UUID
    name: str
    slug: str
    balance_mnt: int
    status: BillingStatus
    daily_rate_mnt: int
    stores_count: int
    cameras_count: int
    credit_until: datetime | None = None
    promo_free_until: datetime | None = None
    last_topup_at: datetime | None = None


class BillingOverview(BaseModel):
    orgs: list[OrgBillingRow]
    total_balance_mnt: int
    total_daily_rate_mnt: int
    active_count: int
    credit_count: int
    suspended_count: int


class TopupRequest(BaseModel):
    amount_mnt: int = Field(gt=0)
    note: str | None = Field(default=None, max_length=500)


class CreditRequest(BaseModel):
    until: datetime
    note: str | None = Field(default=None, max_length=500)


class PromoCodeCreate(BaseModel):
    """code omitted/blank → auto-generated (8 chars A-Z0-9)."""

    code: str | None = Field(default=None, max_length=32)
    kind: PromoKind
    amount_mnt: int | None = Field(default=None, gt=0)
    free_days: int | None = Field(default=None, gt=0)
    valid_until: datetime | None = None
    max_redemptions: int = Field(default=1, gt=0)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("code")
    @classmethod
    def normalize(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        return v or None


class PromoCodeUpdate(BaseModel):
    active: bool | None = None
    note: str | None = Field(default=None, max_length=500)


class PromoCodePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    kind: PromoKind
    amount_mnt: int | None
    free_days: int | None
    valid_until: datetime | None
    max_redemptions: int
    redeemed_count: int
    active: bool
    note: str | None
    created_at: datetime


class BillingDayPoint(BaseModel):
    day: str  # ISO date
    usage_mnt: int
    topup_mnt: int
    promo_mnt: int


class BillingAnalytics(BaseModel):
    by_day: list[BillingDayPoint]
    totals: BillingDayPoint
    suspended_count: int
    credit_count: int
