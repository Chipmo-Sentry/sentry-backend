"""Promo code redemption — validation rules (pure) + the redeem transaction.

Rules: a code must exist, be active, be inside valid_until, and have
redemptions left; one org may redeem a given code exactly ONCE (DB-enforced
by the unique (promo_id, org_id) index — `validate_promo` is the friendly
pre-check, the index is the race-proof truth).

* bonus_amount → a promo_credit journal entry (Dr promo_expense/Cr org_wallet)
* free_days    → extend promo_free_until from max(now, current window)
"""

import secrets
import string
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sentry_backend.db.models.billing import (
    BillingJournal,
    JournalKind,
    PromoCode,
    PromoKind,
    PromoRedemption,
)
from sentry_backend.services.billing.ledger import get_or_create_profile, post_entry

# User-facing errors (Mongolian) — keyed so the API layer can map to HTTP codes.
ERR_NOT_FOUND = "Код олдсонгүй эсвэл хүчингүй болсон."
ERR_EXHAUSTED = "Энэ кодын ашиглалтын эрх дууссан байна."
ERR_ALREADY_REDEEMED = "Энэ кодыг та аль хэдийн ашигласан байна."

_CODE_ALPHABET = string.ascii_uppercase + string.digits


def generate_code(length: int = 8) -> str:
    """Random promo code: 8 chars A-Z0-9."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def normalize_code(code: str) -> str:
    return code.strip().upper()


def validate_promo(
    promo: PromoCode | None,
    *,
    already_redeemed: bool,
    now: datetime,
) -> str | None:
    """Return a user-facing error string, or None when the code is redeemable.

    Pure — the caller fetches the promo row and the org's redemption state.
    Inactive/expired codes are reported the same as missing ones (no oracle
    for guessing which codes exist).
    """
    if promo is None or not promo.active:
        return ERR_NOT_FOUND
    if promo.valid_until is not None and promo.valid_until <= now:
        return ERR_NOT_FOUND
    if already_redeemed:
        return ERR_ALREADY_REDEEMED
    if promo.redeemed_count >= promo.max_redemptions:
        return ERR_EXHAUSTED
    return None


def extend_promo_window(
    current: datetime | None,
    *,
    free_days: int,
    now: datetime,
) -> datetime:
    """New promo_free_until: stack onto a still-open window, else start at now."""
    base = current if (current is not None and current > now) else now
    return base + timedelta(days=free_days)


async def redeem(
    db: AsyncSession,
    *,
    org_id: UUID,
    code: str,
    user_id: UUID | None,
    now: datetime,
) -> tuple[str | None, PromoCode | None]:
    """Redeem `code` for `org_id`. Returns (error_message, promo).

    On success error_message is None. Flushes only — the caller's transaction
    commits, so journal + profile + redemption + counter land atomically.
    """
    normalized = normalize_code(code)
    result = await db.execute(
        select(PromoCode).where(PromoCode.code == normalized).with_for_update()
    )
    promo = result.scalar_one_or_none()
    already = False
    if promo is not None:
        existing = await db.execute(
            select(PromoRedemption.id).where(
                PromoRedemption.promo_id == promo.id,
                PromoRedemption.org_id == org_id,
            )
        )
        already = existing.scalar_one_or_none() is not None
    error = validate_promo(promo, already_redeemed=already, now=now)
    if error is not None or promo is None:
        return error or ERR_NOT_FOUND, None

    journal: BillingJournal | None = None
    if promo.kind is PromoKind.bonus_amount:
        if not promo.amount_mnt or promo.amount_mnt <= 0:
            return ERR_NOT_FOUND, None  # misconfigured code — treat as invalid
        journal = await post_entry(
            db,
            org_id=org_id,
            kind=JournalKind.promo_credit,
            amount_mnt=promo.amount_mnt,
            description=f"Промо код: {promo.code}",
            meta={"promo_code": promo.code, "promo_id": str(promo.id)},
            created_by_user_id=user_id,
        )
    else:  # free_days
        if not promo.free_days or promo.free_days <= 0:
            return ERR_NOT_FOUND, None
        profile = await get_or_create_profile(db, org_id)
        profile.promo_free_until = extend_promo_window(
            profile.promo_free_until, free_days=promo.free_days, now=now
        )

    promo.redeemed_count += 1
    db.add(
        PromoRedemption(
            promo_id=promo.id,
            org_id=org_id,
            journal_id=journal.id if journal is not None else None,
        )
    )
    await db.flush()
    return None, promo
