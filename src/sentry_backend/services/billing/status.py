"""Billing status — one pure function, the single source of truth for gating.

Rules (T14):
* active    — balance > 0  OR a free-days promo window is still open
* credit    — balance <= 0 but a super-admin granted credit_until > now
* suspended — balance <= 0, no credit, no promo (service gated, no charges)
"""

import enum
from datetime import datetime


class BillingStatus(enum.StrEnum):
    active = "active"
    credit = "credit"
    suspended = "suspended"


def billing_status(
    *,
    balance_mnt: int,
    credit_until: datetime | None,
    promo_free_until: datetime | None,
    now: datetime,
) -> BillingStatus:
    if balance_mnt > 0 or (promo_free_until is not None and promo_free_until > now):
        return BillingStatus.active
    if credit_until is not None and credit_until > now:
        return BillingStatus.credit
    return BillingStatus.suspended
