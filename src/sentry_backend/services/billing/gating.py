"""Billing gate for service surfaces (currently: live stream tokens).

Only LIVE VIEWING is gated — alert/clip evidence recording keeps running for a
suspended org (нотолгоо үргэлжилнэ), per the T14 decision.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.billing import BillingProfile
from sentry_backend.services.billing.status import BillingStatus, billing_status

SUSPENDED_DETAIL = "Үлдэгдэл хүрэлцэхгүй байна. Хэтэвчээ цэнэглэснээр live үзэх боломж сэргэнэ."


async def org_billing_status(
    db: AsyncSession, org_id: UUID, now: datetime | None = None
) -> BillingStatus:
    """Current billing status for an org. No profile row yet → status of an
    empty wallet (suspended) — orgs start gated until their first topup/promo."""
    if now is None:
        now = datetime.now(UTC)
    profile = await db.get(BillingProfile, org_id)
    if profile is None:
        return billing_status(balance_mnt=0, credit_until=None, promo_free_until=None, now=now)
    return billing_status(
        balance_mnt=profile.balance_mnt,
        credit_until=profile.credit_until,
        promo_free_until=profile.promo_free_until,
        now=now,
    )
